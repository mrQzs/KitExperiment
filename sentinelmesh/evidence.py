"""Append-only immutable evidence segment storage.

Segments are sealed independently so a growing stdout/stderr stream never
changes the digest of an already referenced resource.  This module stores
bytes only; redaction and protocol parsing belong to adapters.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .core import ProtocolError, ResourceRef, raw_span, sha256_bytes, verify_resource_bytes


@dataclass(frozen=True)
class EvidenceSegment:
    """A sealed segment and its immutable content identity."""

    ref: ResourceRef
    size: int
    representation: str = "captured_raw"

    def __post_init__(self) -> None:
        if not isinstance(self.ref, ResourceRef) or type(self.size) is not int or self.size <= 0:
            raise ProtocolError("E_RESOURCE_SHAPE")
        if not isinstance(self.representation, str) or not self.representation.strip():
            raise ProtocolError("E_RESOURCE_REPRESENTATION")

    @property
    def segment_id(self) -> str:
        return self.ref.id

    def span(self, byte_start: int, byte_end: int, stream: str = "stdout") -> dict[str, object]:
        if byte_start < 0 or byte_end <= byte_start or byte_end > self.size:
            raise ProtocolError("E_EVENT_RANGE", reason="raw span is outside sealed segment")
        return raw_span(self.ref, byte_start, byte_end, stream)


class EvidenceSegmentStore:
    """Write-once evidence segment store backed by a directory or memory."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        kind: str = "evidence-segment",
        representation: str = "captured_raw",
    ) -> None:
        self.kind = kind
        self.representation = representation
        if not isinstance(kind, str) or not kind or not isinstance(representation, str) or not representation:
            raise ProtocolError("E_RESOURCE_SHAPE")
        self._root = Path(root) if root is not None else None
        if self._root is not None:
            self._root.mkdir(parents=True, exist_ok=True)
        self._segments: dict[str, tuple[EvidenceSegment, bytes]] = {}

    @property
    def root(self) -> Path | None:
        return self._root

    def append(
        self,
        data: bytes,
        *,
        segment_id: str | None = None,
        representation: str | None = None,
        path: str | None = None,
    ) -> EvidenceSegment:
        """Seal one non-empty byte segment and return its ResourceRef."""
        if type(data) is not bytes:
            raise ProtocolError("E_RESOURCE_BYTES", reason="bytes input required")
        if not data:
            raise ProtocolError("E_RESOURCE_BYTES", reason="empty evidence segment")
        sid = segment_id or ("seg-" + uuid.uuid4().hex)
        rep = representation or self.representation
        if not isinstance(sid, str) or not sid or not isinstance(rep, str) or not rep:
            raise ProtocolError("E_RESOURCE_SHAPE")
        relative = path or f"segments/{sid}.bin"
        ref = ResourceRef(self.kind, sid, sha256_bytes(data), relative)
        if sid in self._segments:
            old, _ = self._segments[sid]
            if old.ref.sha256 == ref.sha256:
                return old
            raise ProtocolError("E_RESOURCE_IMMUTABLE", reason="segment id already contains other bytes")

        if self._root is not None:
            target = self._root / relative.replace("/", os.sep)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive create prevents a stale path from being replaced.
                with target.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError as exc:
                # A crash can leave an object that must be verified, never overwritten.
                try:
                    existing = target.read_bytes()
                except OSError:
                    raise ProtocolError("E_RESOURCE_WRITE", reason="existing segment cannot be read") from exc
                if existing != data:
                    raise ProtocolError("E_RESOURCE_IMMUTABLE", reason="segment path contains other bytes") from exc
            except OSError as exc:
                raise ProtocolError("E_RESOURCE_WRITE", reason="evidence segment write failed") from exc

        segment = EvidenceSegment(ref, len(data), rep)
        self._segments[sid] = (segment, data)
        return segment

    append_segment = append

    def get(self, ref_or_id: ResourceRef | str) -> EvidenceSegment:
        sid = ref_or_id.id if isinstance(ref_or_id, ResourceRef) else ref_or_id
        if not isinstance(sid, str) or not sid:
            raise ProtocolError("E_RESOURCE_NOT_FOUND")
        try:
            return self._segments[sid][0]
        except KeyError as exc:
            raise ProtocolError("E_RESOURCE_NOT_FOUND", reason="evidence segment is not indexed") from exc

    def read(self, ref_or_id: ResourceRef | str) -> bytes:
        segment = self.get(ref_or_id)
        if self._root is not None:
            target = self._root / segment.ref.path.replace("/", os.sep)  # type: ignore[union-attr]
            try:
                data = target.read_bytes()
            except OSError as exc:
                raise ProtocolError("E_RESOURCE_READ", reason="evidence segment cannot be read") from exc
        else:
            data = self._segments[segment.ref.id][1]
        verify_resource_bytes(segment.ref, data)
        return data

    def span(
        self,
        ref_or_id: ResourceRef | str,
        byte_start: int,
        byte_end: int,
        stream: str = "stdout",
    ) -> dict[str, object]:
        segment = self.get(ref_or_id)
        return segment.span(byte_start, byte_end, stream)

    def iter_segments(self) -> Iterator[EvidenceSegment]:
        for segment, _ in self._segments.values():
            yield segment

    def __len__(self) -> int:
        return len(self._segments)


# Names used by different callers in the design documents.
EvidenceStore = EvidenceSegmentStore
AppendOnlyEvidenceStore = EvidenceSegmentStore


__all__ = [
    "EvidenceSegment",
    "EvidenceSegmentStore",
    "EvidenceStore",
    "AppendOnlyEvidenceStore",
]
