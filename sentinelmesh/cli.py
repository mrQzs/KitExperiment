"""Small CLI for exercising the SentinelMesh reference implementation."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from .config import ConfigResolver, load_profiles
from .observability import DebugBundleBuilder
from .plugins import PluginManifest, resolve_plugins
from .baseline import capture_baseline
from .planner import ContractIR, render_contract_ir
from .workflows import builtin_workflow, lock_for_workflow


def cmd_validate_config(args: argparse.Namespace) -> int:
    profiles = load_profiles(Path(args.file).read_bytes())
    resolver = ConfigResolver(profiles)
    result = resolver.resolve(args.profile, role=args.role)
    print(json.dumps({"profile_id": result.profile.profile_id, "semantic_hash": result.semantic_hash, "source_refs": result.source_refs}, ensure_ascii=False, indent=2))
    return 0


def cmd_plugin_lock(args: argparse.Namespace) -> int:
    raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
    manifests = {item["id"]: PluginManifest.from_dict(item) for item in raw.get("plugins", [])}
    lock = resolve_plugins(manifests, raw.get("requested", list(manifests)), granted_capabilities=set(raw.get("granted_capabilities", [])))
    print(json.dumps({"lock_id": lock.lock_id, "plugins": lock.ordered_plugins, "digest": lock.digest}, ensure_ascii=False, indent=2))
    return 0


def cmd_debug_bundle(args: argparse.Namespace) -> int:
    scope = json.loads(Path(args.scope).read_text(encoding="utf-8"))
    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    bundle = DebugBundleBuilder().build(incident_id=args.incident, scope=scope, evidence=evidence, reproduction=args.reproduction)
    out = DebugBundleBuilder().write(bundle, args.output)
    print(json.dumps({"bundle_id": bundle.bundle_id, "sha256": bundle.sha256, "path": str(out)}, indent=2))
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    snapshot = capture_baseline(args.repository)
    print(json.dumps({"baseline_id": snapshot.baseline_id, "sha256": snapshot.digest,
                      "head": snapshot.head, "complete": snapshot.complete,
                      "issues": list(snapshot.issues), "status_entries": len(snapshot.status),
                      "tracked_entries": len(snapshot.tracked), "untracked_entries": len(snapshot.untracked)},
                     ensure_ascii=False, indent=2))
    return 0 if snapshot.complete else 1


def cmd_workflow(args: argparse.Namespace) -> int:
    definition = builtin_workflow(args.workflow)
    capabilities = {item for item in args.capabilities.split(",") if item}
    lock = lock_for_workflow(args.workflow, granted_capabilities=capabilities)
    print(json.dumps({"workflow": definition.to_mapping(), "workflow_sha256": definition.digest,
                      "plugin_lock_id": lock.lock_id, "plugins": list(lock.ordered_plugins),
                      "plugin_lock_sha256": lock.digest}, ensure_ascii=False, indent=2))
    return 0


def cmd_contract_render(args: argparse.Namespace) -> int:
    ir = ContractIR.from_mapping(json.loads(Path(args.input).read_text(encoding="utf-8")))
    markdown, machine = render_contract_ir(ir)
    Path(args.markdown).write_bytes(markdown)
    Path(args.json).write_bytes(machine)
    print(json.dumps({"task_id": ir.task_id, "plan_version": ir.plan_version,
                      "markdown_sha256": hashlib.sha256(markdown).hexdigest(),
                      "json_sha256": hashlib.sha256(machine).hexdigest()}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smctl", description="SentinelMesh reference control-plane CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("validate-config")
    p.add_argument("file")
    p.add_argument("profile")
    p.add_argument("--role")
    p.set_defaults(func=cmd_validate_config)
    p = sub.add_parser("plugin-lock")
    p.add_argument("file")
    p.set_defaults(func=cmd_plugin_lock)
    p = sub.add_parser("debug-bundle")
    p.add_argument("--incident", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--evidence", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--reproduction", default="")
    p.set_defaults(func=cmd_debug_bundle)
    p = sub.add_parser("baseline", help="capture a read-only repository baseline")
    p.add_argument("repository", nargs="?", default=".")
    p.set_defaults(func=cmd_baseline)
    p = sub.add_parser("workflow", help="validate a built-in workflow and resolve its plugin lock")
    p.add_argument("workflow", choices=("bugfix", "documentation", "regression"))
    p.add_argument("--capabilities", default="read_repo,write_scope,run_checks")
    p.set_defaults(func=cmd_workflow)
    p = sub.add_parser("contract-render", help="render one Contract IR mapping as JSON and Markdown")
    p.add_argument("input")
    p.add_argument("--markdown", required=True)
    p.add_argument("--json", required=True)
    p.set_defaults(func=cmd_contract_render)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
