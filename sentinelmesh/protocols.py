#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codekit protocols —— 01-T2 公共协议的纯函数实现（01 文档 1.3、4.1–4.4，附录 A.2、B、C）。

定位
- “codekit-public” 公共协议家族 v1 的结构校验与部分语义检查，只依赖标准库（01-L01）。
- 起点是附录 B.2 参考实现：错误码语义保持不变并扩展。接口说明、错误码表以及“哪些语义检查
  已实现、哪些明确未实现并归属哪个模块”见 docs/code-kit-v2/01-baseline/protocols-api.md。
- 结构校验已实现 ≠ 协议已完全安全。授权、生产级安全文件访问、真实事件类型、预算与唯一记账、
  合同发布、项目集成等不在本模块（02/03/04/07/09/10 负责）。
- 业务路径的物理检查（resolve_existing_path_for_review、_check_link_hops 逐跳核对）是静态审阅 + 纵深防御，
  不是安全访问保证（裁决 R-P9，01-D06）：无法确定语义的链接写法失败关闭；已知不检测、不保证的情形见
  PATH_REVIEW_LIMITATIONS。

副作用约束（01-A14）
- 导入本模块不读文件、不启动进程、不联网、不读环境变量、不写任何东西。
- 纯函数为主，例外只有：resolve_existing_path_for_review()（被调用时只读地解析调用方给出的路径并
  列出沿途目录的名称）；append_event()（按规格就地更新调用方传入的 state）；new_request_id()/
  new_run_id() 使用随机数。本模块不执行 git，也不调用任何模型。
- ProtocolError.retryable 只是提示，本模块从不据此重试或触发任何动作。

适用范围（附录 D.2、01-D09）：本模块的格式与上限只用于新写入的公共记录和新入口，不改写 v1 合同、
历史证据或旧入口语义。
"""
from __future__ import annotations

import copy
import datetime as _dt
import decimal
import hashlib
import hmac
import json
import math
import os
import re
import stat
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path, PurePath, PureWindowsPath
from types import MappingProxyType
from typing import Any, NamedTuple

# ---------------------------------------------------------------------------
# 常量（01-D01、01-D02、01-D04）
# ---------------------------------------------------------------------------

PROTOCOL_FAMILY = "codekit-public"
PUBLIC_SCHEMA_VERSION = 1
SCHEMA_ID = "urn:codekit:public-protocol:1"
SNAPSHOT_PROFILE = "codekit-config-json-py-v1"
MAX_SAFE_INTEGER = 2**53 - 1
DEFAULT_MAX_BYTES = 1_048_576
DEFAULT_MAX_DEPTH = 64
ROLES = ("planner", "executor", "observer")
PLATFORMS = ("linux", "macos", "windows")
PUBLIC_RECORD_TYPES = ("run-context", "run-event", "usage-observation")
REPRESENTATIONS = ("captured_raw", "redacted_raw", "normalized_event", "synthetic_fixture")
ORIGIN_CLASSES = ("legacy", "captured", "synthetic", "negative")
# 抄自 scripts/pi_dispatch.py:75 的 VERDICTS（基线 a4fa199）。旧枚举原样保留（01 文档 4.1.4），
# 不在此定义新状态转换；测试断言它与 pi_dispatch.VERDICTS 相等以防漂移。
LEGACY_VERDICTS = ("PASS", "ACCEPTED_WITH_EXCEPTION", "REVISE", "BLOCKED", "ESCALATE")

EVENT_SOURCE_KINDS = ("core", "vendor")
EVENT_INTEGRITY_VALUES = ("complete", "incomplete", "pending")
RAW_STREAMS = ("stdout", "stderr", "file")
OBSERVATION_KINDS = ("delta", "cumulative", "final", "unknown")
BILLING_SOURCES = ("vendor_reported", "provider_invoice", "adapter_estimated", "unavailable")
#: 只有这些来源可以带 reported_cost；adapter_estimated/unavailable 的费用只能写在 estimated_cost。
REPORTED_BILLING_SOURCES = ("vendor_reported", "provider_invoice")
STREAM_CLASSIFICATIONS = ("known_required", "known_optional", "ignorable_optional")
DERIVE_ACTIONS = ("reconnect", "replay", "executor_retry", "observer_call",
                  "rework_new_contract", "new_goal")
GIT_OBJECT_FORMATS = ("sha1", "sha256")
USAGE_COUNTERS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens",
                  "reasoning_tokens")
#: 可以为 null（observation_kind 为 "unknown"）且必须在 unknown_reasons 中说明原因的用量字段（4.3.7）。
UNKNOWABLE_USAGE_FIELDS = ("provider", "model", "observation_kind", "input_tokens",
                           "cache_read_tokens", "cache_write_tokens", "output_tokens",
                           "reasoning_tokens", "input_includes_cache", "output_includes_reasoning",
                           "reported_cost", "estimated_cost")
#: 合成样本不能声称证明的“真实兼容”类结论（01-A12）；另按前缀 real_/vendor_/legacy_ 拒绝。
REAL_COMPATIBILITY_CLAIMS = ("real_cli_compatibility", "legacy_dispatch_behavior", "vendor_protocol")
REAL_CLAIM_PREFIXES = ("real_", "vendor_", "legacy_")
#: 真实 CLI/厂商兼容声明（P4 来源规则）：只允许 origin.class=captured 且 representation 为
#: captured_raw/redacted_raw 的条目声明；另按前缀 real_/vendor_ 判定。
VENDOR_COMPATIBILITY_CLAIMS = ("real_cli_compatibility", "vendor_protocol")
VENDOR_CLAIM_PREFIXES = ("real_", "vendor_")
#: 旧调度器行为声明：只允许 origin.class=legacy 且 expected.outcome=legacy-judgment 的条目声明。
LEGACY_DISPATCH_CLAIM = "legacy_dispatch_behavior"
UNKNOWN_CURRENCY = "unknown_currency"
#: v2（P4）：cancelled 改为三态（未提供取消来源且未核对时为 None），见 build_run_dimensions。
DIMENSIONS_RULE_REVISION = "codekit01-run-dimensions-v2"

# ---------------------------------------------------------------------------
# 结构化错误（4.3.6、附录 A error）
# ---------------------------------------------------------------------------


class ErrorSpec(NamedTuple):
    """错误码的固定属性：安全文案（不含任何输入值）、是否“某种重试可能恢复”、类别。"""
    message: str
    retryable: bool
    category: str


_CAT_INPUT = "input_format"
_CAT_CONTEXT = "context"
_CAT_PATH = "path"
_CAT_RESOURCE = "resource"
_CAT_GIT = "git"
_CAT_EVENT = "event_integrity"
_CAT_USAGE = "usage"
_CAT_PROVENANCE = "provenance"
_CAT_BASELINE = "baseline_compat"

_ERROR_TABLE = (
    # 输入格式（4.2.7、4.3.6）：通常需修正输入，不重复执行同样载荷。
    ("E_JSON_INPUT_TYPE", "输入必须是原始字节（bytes），不接受文本或其他类型。", False, _CAT_INPUT),
    ("E_JSON_LIMIT", "输入为空，或超出字节数、嵌套深度等资源上限。", False, _CAT_INPUT),
    ("E_JSON_BOM", "新协议 JSON 不接受 UTF-8 BOM。", False, _CAT_INPUT),
    ("E_JSON_ENCODING", "输入不是有效的 UTF-8 编码。", False, _CAT_INPUT),
    ("E_JSON_SYNTAX", "输入不是单个完整的 JSON 文档。", False, _CAT_INPUT),
    ("E_JSON_DUPLICATE_KEY", "输入对象包含重复字段，不能形成审核输入。", False, _CAT_INPUT),
    ("E_JSON_NON_FINITE", "输入包含非有限数值（NaN、无穷大或数值溢出）。", False, _CAT_INPUT),
    ("E_JSON_UNICODE", "字符串包含孤立代理项，不是合法的 Unicode 标量序列。", False, _CAT_INPUT),
    ("E_JSON_VALUE_TYPE", "值不是 JSON 原生类型。", False, _CAT_INPUT),
    ("E_JSON_KEY_TYPE", "对象键必须是字符串。", False, _CAT_INPUT),
    ("E_JSON_CYCLE", "值中存在循环引用的容器。", False, _CAT_INPUT),
    ("E_INTEGER_RANGE", "整数超出协议允许的范围。", False, _CAT_INPUT),
    ("E_SCHEMA_TYPE", "字段或记录的类型不符合协议结构。", False, _CAT_INPUT),
    ("E_SCHEMA_FIELDS", "字段缺失、存在未知字段，或取值不在允许的集合或长度范围内。", False, _CAT_INPUT),
    ("E_SCHEMA_VERSION_TYPE", "schema_version 必须是正整数，不接受布尔值、字符串或小数形式。", False,
     _CAT_INPUT),
    ("E_SCHEMA_VERSION_UNSUPPORTED", "不支持该 schema_version；不会删除字段或回退到旧版本解释。", False,
     _CAT_INPUT),
    ("E_SCHEMA_RECORD_TYPE", "记录类型与受信入口要求的类型不一致。", False, _CAT_INPUT),
    ("E_ID_SYNTAX", "标识格式不符合协议要求。", False, _CAT_INPUT),
    ("E_TIME_FORMAT", "时间必须是带时区的有效时刻，并使用固定的 UTC 表示。", False, _CAT_INPUT),
    # 上下文绑定（4.1.3、4.3.5）。
    ("E_CONTEXT_INCOMPLETE", "运行上下文缺少该操作必需的字段。", False, _CAT_CONTEXT),
    ("E_CONTEXT_BINDING", "记录与受信运行上下文的绑定不一致；不受信数据不能改写绑定。", False,
     _CAT_CONTEXT),
    # 路径/访问（4.1.5–4.1.7）：不能靠放宽边界“修复”。
    ("E_PATH_SYNTAX", "业务路径不符合可移植的仓库相对路径格式。", False, _CAT_PATH),
    ("E_PATH_RESERVED", "业务路径包含 Windows 保留设备名。", False, _CAT_PATH),
    ("E_PATH_ESCAPE", "路径解析后位于受信根目录之外。", False, _CAT_PATH),
    ("E_PATH_RESOLUTION_FAILED", "路径无法完成静态解析。", False, _CAT_PATH),
    ("E_PATH_NOT_FOUND", "路径指向的对象不存在。", False, _CAT_PATH),
    ("E_PATH_ROOT", "受信根目录不存在或不是目录。", False, _CAT_PATH),
    ("E_PATH_COLLISION", "路径在大小写或 Unicode 归一化后与其他路径同名。", False, _CAT_PATH),
    # 资源校验（4.2）：不重算期望摘要来强行通过。
    ("E_RESOURCE_FIELD", "资源引用的 kind、id 或 sha256 格式无效。", False, _CAT_RESOURCE),
    ("E_RESOURCE_KIND", "资源类型未登记，或不适用于该操作。", False, _CAT_RESOURCE),
    ("E_RESOURCE_PATH_REQUIRED", "文件型资源引用必须包含 path。", False, _CAT_RESOURCE),
    ("E_RESOURCE_BYTES", "资源内容必须以原始字节（bytes）提供。", False, _CAT_RESOURCE),
    ("E_RESOURCE_HASH_MISMATCH", "资源字节的 SHA-256 与引用不一致。", False, _CAT_RESOURCE),
    ("E_RESOURCE_CYCLE", "资源引用之间存在依赖环。", False, _CAT_RESOURCE),
    ("E_RESOURCE_CONFLICT", "同一不可变位置已有不同内容，不会覆盖。", False, _CAT_RESOURCE),
    ("E_RESOURCE_INDEX_FAILED", "资源已封存但索引提交失败；可用相同字节幂等补录。", True, _CAT_RESOURCE),
    ("E_RESOURCE_MISSING", "引用的资源不存在或不是普通文件。", False, _CAT_RESOURCE),
    # Git 对象（4.2.3）：先确认基线/环境，不自动拉取或改仓库。
    ("E_GIT_OID_SYNTAX", "Git 对象标识必须是该对象格式下完整的小写 OID。", False, _CAT_GIT),
    ("E_GIT_OBJECT_MISSING", "Git 对象不存在或无法查询。", False, _CAT_GIT),
    ("E_GIT_OBJECT_TYPE", "Git 对象类型与字段要求不符。", False, _CAT_GIT),
    ("E_GIT_FORMAT_UNSUPPORTED", "不支持该 Git 对象格式；不会按长度猜测。", False, _CAT_GIT),
    # 事件完整性（4.3.1–4.3.5）。裁决 R-P1：只有“可恢复读取”的码为 retryable（4.3.6 表所列的
    # GAP、REPLAY_CONFLICT、PROTOCOL_INCOMPLETE，以及分段可能尚未封存的 RAW_REF_UNKNOWN），仍不自动
    # 重新调用模型；RAW_RANGE、TYPE_UNKNOWN、SOURCE、EVIDENCE_MISSING 是输入缺陷，同一记录重读仍失败。
    ("E_EVENT_GAP", "事件序号不连续，日志不完整。", True, _CAT_EVENT),
    ("E_EVENT_REPLAY_CONFLICT", "同一事件序号出现不同内容，冲突已隔离。", True, _CAT_EVENT),
    ("E_PROTOCOL_INCOMPLETE", "协议证据不完整，不能形成成功结论。", True, _CAT_EVENT),
    ("E_EVENT_RAW_RANGE", "原始证据字节范围无效或超出被引用分段。", False, _CAT_EVENT),
    ("E_EVENT_RAW_REF_UNKNOWN", "原始证据引用的分段版本未封存或未知。", True, _CAT_EVENT),
    ("E_EVENT_TYPE_UNKNOWN", "事件类型未登记。", False, _CAT_EVENT),
    ("E_EVENT_SOURCE", "事件来源不允许产生该事件类型。", False, _CAT_EVENT),
    ("E_EVENT_EVIDENCE_MISSING", "厂商事件缺少解析修订或可引用的原始证据。", False, _CAT_EVENT),
    # 用量（4.3.7–4.3.9）：进入核对/拒绝聚合，不补零。
    ("E_USAGE_CONFLICT", "用量观测之间存在冲突，不能合并。", False, _CAT_USAGE),
    ("E_USAGE_SCOPE_UNKNOWN", "用量观测的累计/增量范围不明确，不猜测口径。", False, _CAT_USAGE),
    ("E_USAGE_CURRENCY_MISMATCH", "修订链或同一累计序列中的费用币种不一致。", False, _CAT_USAGE),
    ("E_USAGE_UNKNOWN_REASON", "未知用量字段与 unknown_reasons 不一致。", False, _CAT_USAGE),
    ("E_USAGE_VALUE", "金额或币种格式无效，或实际费用与费用来源不一致。", False, _CAT_USAGE),
    # 来源（4.3.3、附录 C.1、01-A12）。
    ("E_PROVENANCE_CLAIM", "来源、表示与能力声明不一致。", False, _CAT_PROVENANCE),
    # 基线/兼容（2.2、8.1）：重新取证或明确不支持。
    ("E_BASELINE_DRIFT", "基线在测试前后发生漂移。", False, _CAT_BASELINE),
    ("E_BASELINE_INPUT_MISSING", "基线输入缺失。", False, _CAT_BASELINE),
    ("E_COMPAT_UNSUPPORTED", "不支持的记录类型、平台或能力。", False, _CAT_BASELINE),
)

#: 错误码 -> ErrorSpec。错误码是机器接口；message 可以改进措辞而不改变 code（4.3.6）。
ERROR_CODES: dict[str, ErrorSpec] = {
    code: ErrorSpec(message, retryable, category)
    for code, message, retryable, category in _ERROR_TABLE
}

_ERROR_TEXT_LIMITS = {"message": 512, "field": 512, "phase": 64, "reason": 512, "hint": 512}
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
#: field 被改写（含孤立代理项或超过上限）时追加到 reason 的固定标记。
FIELD_TRUNCATED_MARK = ";field_truncated_to_ancestor"


def _scrub_text(text: str, limit: int | None) -> str:
    """把孤立代理项换成 U+FFFD 并按 Schema 上限（以码点计）截断，保证错误记录本身可序列化。"""
    cleaned = _SURROGATE_RE.sub("�", text)
    return cleaned if limit is None or len(cleaned) <= limit else cleaned[:limit]


def _safe_pointer(field: str, limit: int | None) -> tuple[str, bool]:
    """返回 (可安全输出的指针, 是否被改写)。

    指针含孤立代理项或超过 limit 时，截到最长的、由完整引用 token 组成的**祖先**指针（最短是根 ""）：
    不在 "~" 与其后的 0/1 之间截断，也不把代理项替换成 U+FFFD 而误指向另一个真实存在的键。
    """
    if (limit is None or len(field) <= limit) and not _has_surrogate(field):
        return field, False
    result = ""
    if field.startswith("/"):
        for token in field[1:].split("/"):
            candidate = result + "/" + token
            if _has_surrogate(token) or (limit is not None and len(candidate) > limit):
                break
            result = candidate
    return result, True


class ProtocolError(ValueError):
    """公共协议错误。只携带固定错误码、JSON Pointer 与固定原因文字，从不携带输入值。

    field：JSON Pointer（RFC 6901）或 None；phase：检查阶段；reason：固定的机器可读原因；
    hint：可选处理提示；source_ref：可信来源的 ResourceRef 或 None；retryable：默认取错误码表，
    只是提示，不触发任何动作。
    """

    def __init__(self, code: str, *, field: str | None = None, phase: str | None = None,
                 reason: str | None = None, hint: str | None = None,
                 source_ref: dict[str, Any] | None = None, retryable: bool | None = None):
        if type(code) is not str or code not in ERROR_CODES:
            raise ValueError("unknown protocol error code")
        for name, item in (("field", field), ("phase", phase), ("reason", reason), ("hint", hint)):
            if item is not None and type(item) is not str:
                raise ValueError(f"ProtocolError.{name} must be str or None")
        if source_ref is not None and not _is_resource_ref_shape(source_ref):
            raise ValueError("ProtocolError.source_ref must be a structurally valid ResourceRef")
        spec = ERROR_CODES[code]
        if retryable is None:
            retryable = spec.retryable
        elif type(retryable) is not bool:
            raise ValueError("ProtocolError.retryable must be bool or None")
        super().__init__(code)
        self.code = code
        self.field = field
        self.phase = phase if phase is not None else spec.category
        self.reason = reason if reason is not None else "invalid_value"
        self.hint = hint
        self.source_ref = copy.deepcopy(source_ref)
        self.retryable = retryable

    @property
    def message(self) -> str:
        return ERROR_CODES[self.code].message

    def _detail_dict(self, limits: Mapping[str, int] | None) -> dict[str, Any]:
        def cap(name: str) -> int | None:
            return None if limits is None else limits[name]

        field, altered = (None, False) if self.field is None else _safe_pointer(self.field,
                                                                                  cap("field"))
        reason = self.reason + FIELD_TRUNCATED_MARK if altered else self.reason
        reason_limit = cap("reason")
        if altered and reason_limit is not None and len(reason) > reason_limit:
            # 保证标记本身不被截掉：先截原因正文，再追加标记。
            reason = self.reason[:reason_limit - len(FIELD_TRUNCATED_MARK)] + FIELD_TRUNCATED_MARK
        return {"field": field, "source_ref": copy.deepcopy(self.source_ref),
                "phase": _scrub_text(self.phase, cap("phase")),
                "reason": _scrub_text(reason, reason_limit),
                "hint": None if self.hint is None else _scrub_text(self.hint, cap("hint"))}

    @property
    def details(self) -> dict[str, Any]:
        """与附录 A errorDetail 同形的字典（不按长度截断）；附录 B 参考测试通过它读取 field。

        文字中的孤立代理项已清除（可按 UTF-8 编码）；field 含孤立代理项时改为最长的祖先指针，
        并在 reason 末尾追加 FIELD_TRUNCATED_MARK。原始指针仍在属性 field 上（可能含孤立代理项，
        不得原样外发）。
        """
        return self._detail_dict(None)

    def to_record(self) -> dict[str, Any]:
        """返回附录 A 的 error 结构；文字按 Schema 上限截断，message 取错误码表的安全文案。

        field 超过上限或含孤立代理项时，截到最长的祖先指针（仍是合法的 RFC 6901 指针，指向真实
        存在的上层成员），并在 reason 末尾追加 FIELD_TRUNCATED_MARK。
        """
        return {
            "code": self.code,
            "message": _scrub_text(ERROR_CODES[self.code].message, _ERROR_TEXT_LIMITS["message"]),
            "details": self._detail_dict(_ERROR_TEXT_LIMITS),
            "retryable": self.retryable,
        }


def _escape_token(key: str | int) -> str:
    """RFC 6901：~ 转义为 ~0，/ 转义为 ~1。"""
    return str(key).replace("~", "~0").replace("/", "~1")


def _pointer(parent: str | None, key: str | int) -> str:
    return (parent or "") + "/" + _escape_token(key)


def _render_pointer(prefix: str, chain: Any) -> str:
    """把惰性路径链 (父链, 键) 渲染为 JSON Pointer。只在构造错误时调用，避免遍历时逐层拼接字符串
    （否则父路径很长时总开销为 O(元素数 × 路径长)）。"""
    tokens = []
    while chain is not None:
        chain, key = chain
        tokens.append(_escape_token(key))
    return prefix + "".join("/" + token for token in reversed(tokens))


def _has_surrogate(value: str) -> bool:
    return _SURROGATE_RE.search(value) is not None


def _check_unicode(value: str, field: str | None) -> None:
    if _has_surrogate(value):
        raise ProtocolError("E_JSON_UNICODE", field=field, reason="unpaired_surrogate")


# ---------------------------------------------------------------------------
# 严格 JSON（4.2.6、4.2.7、附录 A.2 第一行）
# ---------------------------------------------------------------------------

def _check_positive_param(name: str, value: Any) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive int")


class _Pairs(list):
    """保留对象成员对，等解析完成后再按 JSON Pointer 定位重复键。"""


class _BadNumber:
    """严格解析中遇到的非有限数或越界整数（P4 第 3 轮，复审 json 视角）。

    json 的解析钩子里还不知道 JSON Pointer；钩子因此不直接抛错，而是返回本哨兵，由 _materialize 按文档
    顺序遍历时带指针报告。这样 E_JSON_NON_FINITE / E_INTEGER_RANGE 与重复键、孤立代理项一样带 field，
    与 check_json_value 对同一个值给出的 field 相同。
    """

    __slots__ = ("code", "reason")

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason


_NON_FINITE_LITERAL = _BadNumber("E_JSON_NON_FINITE", "non_finite_literal")
_NUMERIC_OVERFLOW = _BadNumber("E_JSON_NON_FINITE", "numeric_overflow")
_INTEGER_OUTSIDE_PROFILE = _BadNumber("E_INTEGER_RANGE", "integer_outside_profile")


def _reject_constant(_: str) -> Any:
    return _NON_FINITE_LITERAL


def _parse_float(text: str) -> Any:
    value = float(text)
    if not math.isfinite(value):
        return _NUMERIC_OVERFLOW
    return value


def _parse_int(text: str) -> Any:
    digits = text[1:] if text.startswith("-") else text
    if len(digits) > 16:  # 先按位数判定，不做超长整数转换（避免开销与解释器位数上限）
        return _INTEGER_OUTSIDE_PROFILE
    value = int(text)
    if abs(value) > MAX_SAFE_INTEGER:
        return _INTEGER_OUTSIDE_PROFILE
    return value


def _materialize(value: Any, chain: Any, level: int, max_depth: int) -> Any:
    """把 json 的解析结果转成 dict/list，同时按文档顺序检查嵌套深度、重复键、孤立代理项、非有限数与
    整数范围（后两者来自解析钩子返回的 _BadNumber 哨兵）。

    chain 是惰性路径链（见 _render_pointer）；指针只在出错时渲染。
    """
    kind = type(value)
    if kind is _BadNumber:
        raise ProtocolError(value.code, field=_render_pointer("", chain) or None, phase="decode",
                            reason=value.reason)
    if kind is _Pairs or kind is list:
        if level + 1 > max_depth:
            raise ProtocolError("E_JSON_LIMIT", field=_render_pointer("", chain) or None,
                                phase="decode", reason="nesting_limit_exceeded")
        if kind is list:
            return [_materialize(item, (chain, index), level + 1, max_depth)
                    for index, item in enumerate(value)]
        result: dict[str, Any] = {}
        for key, item in value:
            if _has_surrogate(key):
                raise ProtocolError("E_JSON_UNICODE", field=_render_pointer("", (chain, key)),
                                    phase="decode", reason="unpaired_surrogate_in_key")
            if key in result:
                raise ProtocolError("E_JSON_DUPLICATE_KEY", field=_render_pointer("", (chain, key)),
                                    phase="decode", reason="duplicate_object_member",
                                    hint="修正重复字段后重新提交；不要保留最后一个值后继续审核。")
            result[key] = _materialize(item, (chain, key), level + 1, max_depth)
        return result
    if kind is str and _has_surrogate(value):
        raise ProtocolError("E_JSON_UNICODE", field=_render_pointer("", chain) or None,
                            phase="decode", reason="unpaired_surrogate")
    return value


def strict_json_loads(raw: bytes, *, max_bytes: int = DEFAULT_MAX_BYTES,
                      max_depth: int = DEFAULT_MAX_DEPTH) -> Any:
    """严格解析新协议 JSON 字节（4.2.7）。

    只接受 type 精确为 bytes 的输入；拒绝空输入、超限、BOM、非 UTF-8、尾随第二文档、重复键
    （包括转义后相同的键，嵌套也检出）、NaN/Infinity 与溢出、|整数| > 2^53-1、孤立代理项、
    嵌套超过 max_depth（解码器递归超限同样报 E_JSON_LIMIT）。

    检查时机：类型、长度、BOM、UTF-8 严格按此顺序；之后由 json 从左到右解析，文法错误（含尾随第二文档）
    在解析失败处报（reason 固定为 syntax_error，行/列位置放在 hint 中）；解析成功后再按文档顺序遍历，
    检查嵌套深度、重复键、非有限数、整数范围与孤立代理项，报告遍历中遇到的第一处，field 指向该成员
    （P4 第 3 轮：非有限数与整数范围也带 field）。因此多重损坏输入不承诺返回同一个“第一处错误”，只对
    单一故障输入承诺稳定错误码（4.2.7、4.3.6）。嵌套深度在解析后按真实的字符串边界计算，字符串内的
    括号永远不计入。
    """
    _check_positive_param("max_bytes", max_bytes)
    _check_positive_param("max_depth", max_depth)
    if type(raw) is not bytes:
        raise ProtocolError("E_JSON_INPUT_TYPE", phase="decode", reason="bytes_input_required")
    if not 1 <= len(raw) <= max_bytes:
        raise ProtocolError("E_JSON_LIMIT", phase="decode", reason="empty_or_byte_limit_exceeded")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ProtocolError("E_JSON_BOM", phase="decode", reason="utf8_bom_not_accepted")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ProtocolError("E_JSON_ENCODING", phase="decode", reason="utf8_decoding_failed") from None
    try:
        parsed = json.loads(text, object_pairs_hook=_Pairs, parse_constant=_reject_constant,
                            parse_float=_parse_float, parse_int=_parse_int)
        return _materialize(parsed, None, 0, max_depth)
    except json.JSONDecodeError as exc:
        # reason 是固定的机器可读原因；位置随输入与标准库 json 的实现变化，只放在可选的 hint 中（P4 第 3 轮）。
        raise ProtocolError("E_JSON_SYNTAX", phase="decode", reason="syntax_error",
                            hint=f"line={exc.lineno} column={exc.colno}") from None
    except RecursionError:
        raise ProtocolError("E_JSON_LIMIT", phase="decode",
                            reason="decoder_recursion_limit_exceeded") from None


# 固定快照（紧凑）序列化后各类值至少占用的字节数；用于在遍历时累计输出大小的**下界**。
_MIN_BYTES_NONE, _MIN_BYTES_TRUE, _MIN_BYTES_FALSE, _MIN_BYTES_INT, _MIN_BYTES_FLOAT = 4, 4, 5, 1, 3


def _charge(budget: list[int] | None, cost: int, prefix: str, chain: Any) -> None:
    if budget is None:
        return
    budget[0] -= cost
    if budget[0] < 0:
        raise ProtocolError("E_JSON_LIMIT", field=_render_pointer(prefix, chain) or None,
                            phase="value", reason="serialized_size_lower_bound_exceeded")


def _reaches_cycle(start: Any, ancestors: set[int]) -> bool:
    """从容器 start 出发（只沿 type 精确为 dict/list 的容器），能否回到当前路径上的祖先，或在下方成环。

    只在嵌套深度超限时调用，用来区分“环”（E_JSON_CYCLE）与“过深但无环”（E_JSON_LIMIT）。迭代实现，
    不受递归上限影响；每个容器只访问一次，开销与调用方内存中的容器与成员数成线性。
    """
    active = set(ancestors)  # 当前路径上的祖先 + 本次深度优先搜索栈上的容器
    finished: set[int] = set()
    active.add(id(start))
    stack = [(start, iter(start.values() if type(start) is dict else start))]
    while stack:
        node, children = stack[-1]
        for child in children:
            kind = type(child)
            if kind is not dict and kind is not list:
                continue
            child_id = id(child)
            if child_id in active:
                return True
            if child_id in finished:
                continue
            active.add(child_id)
            stack.append((child, iter(child.values() if kind is dict else child)))
            break
        else:
            stack.pop()
            active.discard(id(node))
            finished.add(id(node))
    return False


def _check_value(value: Any, prefix: str, chain: Any, level: int, max_depth: int,
                 ancestors: set[int], budget: list[int] | None) -> None:
    kind = type(value)
    if value is None:
        _charge(budget, _MIN_BYTES_NONE, prefix, chain)
        return
    if kind is bool:
        _charge(budget, _MIN_BYTES_TRUE if value else _MIN_BYTES_FALSE, prefix, chain)
        return
    if kind is str:
        if _has_surrogate(value):
            raise ProtocolError("E_JSON_UNICODE", field=_render_pointer(prefix, chain) or None,
                                phase="value", reason="unpaired_surrogate")
        _charge(budget, 2 + len(value), prefix, chain)
        return
    if kind is int:
        if abs(value) > MAX_SAFE_INTEGER:
            raise ProtocolError("E_INTEGER_RANGE", field=_render_pointer(prefix, chain) or None,
                                phase="value", reason="integer_outside_profile")
        _charge(budget, _MIN_BYTES_INT, prefix, chain)
        return
    if kind is float:
        if not math.isfinite(value):
            raise ProtocolError("E_JSON_NON_FINITE", field=_render_pointer(prefix, chain) or None,
                                phase="value", reason="non_finite_float")
        _charge(budget, _MIN_BYTES_FLOAT, prefix, chain)
        return
    if kind is not dict and kind is not list:
        raise ProtocolError("E_JSON_VALUE_TYPE", field=_render_pointer(prefix, chain) or None,
                            phase="value", reason="only_json_native_values_accepted")
    identity = id(value)
    # 环先于深度判断，且深度超限时再确认下方是否有环（P4 第 3 轮，复审 json 视角）：同一种单一故障（一条
    # 回边）无论环多长都报 E_JSON_CYCLE，不因环长超过剩余深度而变成 E_JSON_LIMIT。
    if identity in ancestors:
        raise ProtocolError("E_JSON_CYCLE", field=_render_pointer(prefix, chain) or None,
                            phase="value", reason="cyclic_container")
    if level + 1 > max_depth:
        if _reaches_cycle(value, ancestors):
            raise ProtocolError("E_JSON_CYCLE", field=_render_pointer(prefix, chain) or None,
                                phase="value", reason="cyclic_container")
        raise ProtocolError("E_JSON_LIMIT", field=_render_pointer(prefix, chain) or None,
                            phase="value", reason="nesting_limit_exceeded")
    # 括号 2 字节，成员之间各 1 个逗号。
    _charge(budget, 2 + max(len(value) - 1, 0), prefix, chain)
    ancestors.add(identity)
    try:
        if kind is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise ProtocolError("E_JSON_KEY_TYPE", field=_render_pointer(prefix, chain) or None,
                                        phase="value", reason="object_keys_must_be_strings")
                child = (chain, key)
                if _has_surrogate(key):
                    raise ProtocolError("E_JSON_UNICODE", field=_render_pointer(prefix, child),
                                        phase="value", reason="unpaired_surrogate")
                _charge(budget, 3 + len(key), prefix, child)  # 两个引号与冒号
                _check_value(item, prefix, child, level + 1, max_depth, ancestors, budget)
        else:
            for index, item in enumerate(value):
                _check_value(item, prefix, (chain, index), level + 1, max_depth, ancestors, budget)
    finally:
        ancestors.discard(identity)


def check_json_value(value: Any, *, max_depth: int = DEFAULT_MAX_DEPTH, field: str = "",
                     max_bytes: int | None = None) -> None:
    """检查 Python 值是否为 JSON 原生值（type 精确为 dict/list/str/int/float/bool/None）。

    子类（包括 OrderedDict、IntEnum、str 子类）一律 E_JSON_VALUE_TYPE；dict 键必须是 str；
    环 E_JSON_CYCLE（同一容器被复用但不成环可以接受）；深度按容器嵌套层数计，与 strict_json_loads
    一致；整数范围、非有限浮点、孤立代理项同 strict_json_loads。field 是错误定位用的指针前缀。
    环先于深度判断：环长超过剩余深度时，在深度超限处另做一次不限深度的迭代环检测，仍报 E_JSON_CYCLE
    （field 指向深度超限处）；只有过深而无环才报 E_JSON_LIMIT（P4 第 3 轮）。

    max_bytes：给出时，边遍历边累计该值按固定快照（紧凑）序列化后字节数的下界，超过即
    E_JSON_LIMIT。共享容器每被引用一次就计一次，因此“内存里只有几十个对象、展开后指数级”的
    共享结构会在预算用完时停止，不会指数级遍历。默认 None 不限；处理调用方直接传入的不受信 Python
    值时应给出上限（stable_snapshot_bytes 与各记录校验函数都已给出）。
    """
    _check_positive_param("max_depth", max_depth)
    if max_bytes is not None:
        _check_positive_param("max_bytes", max_bytes)
    if type(field) is not str:
        raise ValueError("field must be a str JSON Pointer prefix")
    budget = None if max_bytes is None else [max_bytes]
    try:
        _check_value(value, field, None, 0, max_depth, set(), budget)
    except RecursionError:
        raise ProtocolError("E_JSON_LIMIT", field=field or None, phase="value",
                            reason="recursion_limit_exceeded") from None


def stable_snapshot_bytes(value: dict[str, Any]) -> bytes:
    """固定序列化 profile codekit-config-json-py-v1（4.2.6）。

    参数必须已经是 02 验证并完成语义投影的对象；本函数不删除秘密、不补默认值、不判断授权，
    不排序数组、不做 Unicode 归一化、不把 1.0 变成 1。输出为 UTF-8（无 BOM）加一个 LF。
    检查顺序与附录 B.2 参考实现相同（P4 第 3 轮，复审 fidelity 视角）：先要求根的 type 精确为 dict
    （否则 E_SCHEMA_TYPE，不遍历），再按输出字节数下界做预算检查（不含结尾 LF 不超过 1 MiB - 1），
    超限的值（包括共享容器展开后超限的值）在生成输出之前就报 E_JSON_LIMIT。
    """
    if type(value) is not dict:
        raise ProtocolError("E_SCHEMA_TYPE", phase="snapshot", reason="snapshot_root_must_be_object")
    check_json_value(value, max_bytes=DEFAULT_MAX_BYTES - 1)
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > DEFAULT_MAX_BYTES:
        raise ProtocolError("E_JSON_LIMIT", phase="snapshot", reason="snapshot_byte_limit_exceeded")
    return encoded


def sha256_hex(raw: bytes) -> str:
    """完整字节的 SHA-256，64 位小写十六进制。只接受 type 精确为 bytes 的输入。"""
    if type(raw) is not bytes:
        raise ProtocolError("E_RESOURCE_BYTES", phase="digest", reason="bytes_input_required")
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# 标识、整数与枚举（4.1.1）
# ---------------------------------------------------------------------------

_REQUEST_ID_RE = re.compile(r"req-[0-9a-f]{32}")
_RUN_ID_RE = re.compile(r"run-[0-9a-f]{32}")
_TASK_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_LOGICAL_ID_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
_KIND_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def new_request_id() -> str:
    """新的 request 标识（01-D01）。持久化唯一性由上层（07）检查。"""
    return "req-" + uuid.uuid4().hex


def new_run_id() -> str:
    """新的 run 标识（01-D01）。持久化唯一性由上层（07）检查。"""
    return "run-" + uuid.uuid4().hex


def _match_id(regex: re.Pattern[str], value: Any, field: str, *, code: str = "E_ID_SYNTAX",
              max_len: int | None = None) -> str:
    # re.fullmatch 要求整串匹配：尾随换行不会像 "$" 那样被放过。
    if (type(value) is not str or (max_len is not None and len(value) > max_len)
            or regex.fullmatch(value) is None):
        raise ProtocolError(code, field=field, phase="identity", reason="identifier_syntax")
    return value


def validate_request_id(value: Any, *, field: str = "/request_id") -> str:
    return _match_id(_REQUEST_ID_RE, value, field)


def validate_run_id(value: Any, *, field: str = "/run_id") -> str:
    return _match_id(_RUN_ID_RE, value, field)


def validate_task_id(value: Any, *, field: str = "/task_id") -> str:
    return _match_id(_TASK_ID_RE, value, field, max_len=64)


def validate_logical_id(value: Any, *, field: str = "/id") -> str:
    return _match_id(_LOGICAL_ID_RE, value, field)


def validate_resource_kind(value: Any, *, field: str = "/kind") -> str:
    """kind 语法（不查登记）。在 ResourceRef 语境下 validate_resource_ref 报 E_RESOURCE_FIELD。"""
    return _match_id(_KIND_RE, value, field)


def validate_sha256(value: Any, *, field: str = "/sha256") -> str:
    """64 位小写十六进制摘要；拒绝大写、短摘要、sha256: 前缀与尾随换行。"""
    return _match_id(_SHA256_RE, value, field)


def require_int(value: Any, *, field: str, minimum: int, maximum: int = MAX_SAFE_INTEGER) -> int:
    """精确整数：type(value) is int（拒绝 bool、1.0 等 float、str）→ 否则 E_SCHEMA_TYPE；越界 E_INTEGER_RANGE。"""
    if type(value) is not int:
        raise ProtocolError("E_SCHEMA_TYPE", field=field, phase="schema", reason="exact_integer_required")
    if value < minimum or value > maximum:
        raise ProtocolError("E_INTEGER_RANGE", field=field, phase="schema", reason="integer_out_of_range")
    return value


def validate_plan_version(value: Any, *, allow_null: bool = False,
                          field: str = "/plan_version") -> int | None:
    """正整数；allow_null=True 时允许 None（尚无合同）。"""
    if value is None:
        if allow_null:
            return None
        raise ProtocolError("E_SCHEMA_TYPE", field=field, phase="schema", reason="value_required")
    return require_int(value, field=field, minimum=1)


def validate_attempt(value: Any, *, field: str = "/attempt") -> int | None:
    """None 或非负整数。

    新格式允许 0 只表示格式允许；旧实现首个编号为 1（number = max(existing, default=0) + 1，
    scripts/pi_dispatch.py:2619-2623，见 01-D03），不为新 Schema 平移历史编号。
    """
    if value is None:
        return None
    return require_int(value, field=field, minimum=0)


def _enum(value: Any, allowed: Sequence[str], field: str, *, allow_null: bool = False,
          phase: str = "schema") -> Any:
    """封闭枚举：值不在集合内（包括类型不对）一律 E_SCHEMA_FIELDS。"""
    if value is None and allow_null:
        return None
    if type(value) is not str or value not in allowed:
        raise ProtocolError("E_SCHEMA_FIELDS", field=field, phase=phase, reason="value_not_in_enum")
    return value


def validate_role(value: Any, *, allow_null: bool = False, field: str = "/role") -> str | None:
    return _enum(value, ROLES, field, allow_null=allow_null)


def validate_platform(value: Any, *, field: str = "/platform") -> str:
    return _enum(value, PLATFORMS, field)


_SYSTEM_TO_PLATFORM = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}


def platform_from_system(system: Any) -> str:
    """把受信环境事实（platform.system() 的返回值）精确映射到 platform；其他一律不支持，不猜 linux。"""
    if type(system) is not str or system not in _SYSTEM_TO_PLATFORM:
        raise ProtocolError("E_COMPAT_UNSUPPORTED", field="/platform", phase="context",
                            reason="unsupported_system")
    return _SYSTEM_TO_PLATFORM[system]


# ---------------------------------------------------------------------------
# 通用结构辅助
# ---------------------------------------------------------------------------

def _expect_dict(value: Any, field: str | None, *, phase: str = "schema") -> dict[str, Any]:
    if type(value) is not dict:
        raise ProtocolError("E_SCHEMA_TYPE", field=field or None, phase=phase,
                            reason="object_required")
    return value


def _expect_list(value: Any, field: str, *, max_items: int | None = None) -> list[Any]:
    if type(value) is not list:
        raise ProtocolError("E_SCHEMA_TYPE", field=field, phase="schema", reason="array_required")
    if max_items is not None and len(value) > max_items:
        raise ProtocolError("E_SCHEMA_FIELDS", field=field, phase="schema", reason="too_many_items")
    return value


def _require_closed(obj: dict[str, Any], required: Sequence[str], *, field: str = "",
                    optional: Sequence[str] = ()) -> None:
    """字段封闭：缺必需字段或出现未知字段都是 E_SCHEMA_FIELDS（先报缺失，再报未知）。"""
    for name in required:
        if name not in obj:
            raise ProtocolError("E_SCHEMA_FIELDS", field=_pointer(field, name), phase="schema",
                                reason="missing_field")
    allowed = set(required) | set(optional)
    names = []
    for key in obj:
        if type(key) is not str:
            raise ProtocolError("E_SCHEMA_FIELDS", field=field or None, phase="schema",
                                reason="non_string_field_name")
        if key not in allowed:
            names.append(key)
    if names:
        raise ProtocolError("E_SCHEMA_FIELDS", field=_pointer(field, sorted(names)[0]),
                            phase="schema", reason="unknown_field")


def _require_str(value: Any, field: str, *, min_len: int = 1, max_len: int,
                 allow_null: bool = False) -> str | None:
    if value is None and allow_null:
        return None
    if type(value) is not str:
        raise ProtocolError("E_SCHEMA_TYPE", field=field, phase="schema", reason="string_required")
    _check_unicode(value, field)
    if not min_len <= len(value) <= max_len:
        raise ProtocolError("E_SCHEMA_FIELDS", field=field, phase="schema",
                            reason="string_length_out_of_range")
    return value


def _require_bool(value: Any, field: str, *, allow_null: bool = False) -> bool | None:
    if value is None and allow_null:
        return None
    if type(value) is not bool:
        raise ProtocolError("E_SCHEMA_TYPE", field=field, phase="schema", reason="boolean_required")
    return value


def _check_record_header(obj: dict[str, Any], record_type: str, field: str = "") -> int:
    """schema_version 精确正整数、record_type 一致、版本受支持；顺序与 route_public_record 相同。"""
    version = obj.get("schema_version")
    if type(version) is not int or version < 1:
        raise ProtocolError("E_SCHEMA_VERSION_TYPE", field=_pointer(field, "schema_version"),
                            phase="route", reason="schema_version_not_positive_integer")
    declared = obj.get("record_type")
    if type(declared) is not str or declared != record_type:  # str 子类也不接受
        raise ProtocolError("E_SCHEMA_RECORD_TYPE", field=_pointer(field, "record_type"),
                            phase="route", reason="record_type_mismatch")
    if version != PUBLIC_SCHEMA_VERSION:
        raise ProtocolError("E_SCHEMA_VERSION_UNSUPPORTED", field=_pointer(field, "schema_version"),
                            phase="route", reason="schema_version_unsupported")
    return version


def _check_record_json(obj: dict[str, Any], field: str = "") -> None:
    """整条记录先按 JSON 原生值检查（类型、深度、整数范围、非有限数、孤立代理项、字节下界）。

    记录与其中的 payload 共用同一个深度上限（整条记录不超过 64 层），类型与深度的判断与
    strict_json_loads、stable_snapshot_bytes 一致，不会出现“记录校验通过、快照或严格解析却按深度或
    类型拒绝”的情况。字节预算只是下界（转义会让实际输出更长），实际大小仍以快照时的检查为准。
    """
    check_json_value(obj, field=field, max_bytes=DEFAULT_MAX_BYTES)


def _report(record_type: str, *, payload_validated: bool, done: Iterable[str],
            skipped: Iterable[str]) -> dict[str, Any]:
    done_list = list(dict.fromkeys(done))
    return {
        "record_type": record_type,
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "structural": True,
        "payload_validated": payload_validated,
        "semantic_checks": done_list,
        "semantic_checks_skipped": [name for name in dict.fromkeys(skipped) if name not in done_list],
    }


# ---------------------------------------------------------------------------
# 业务路径（4.1.5–4.1.7）
# ---------------------------------------------------------------------------

_PATH_FORBIDDEN = frozenset('\\:<>"|?*')
_RESERVED_DEVICE_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    prefix + digit for prefix in ("COM", "LPT") for digit in tuple("0123456789") + ("¹", "²", "³")
})
MAX_PATH_BYTES = 4096
MAX_SEGMENT_BYTES = 255
#: “控制字符”按 Unicode 类别 Cc 理解：C0（U+0000–U+001F）、DEL（U+007F）与 C1（U+0080–U+009F）。
_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f]")
#: 裁决 R-P4：行/段分隔符 U+2028、U+2029 与双向控制字符 U+061C、U+200E、U+200F、U+202A–U+202E、
#: U+2066–U+2069 同样拒绝（日志注入与 Trojan Source 风险）；其他格式字符（例如 ZWNJ U+200C）不拒绝。
_LINE_SEPARATOR_OR_BIDI_RE = re.compile("[؜‎‏  ‪-‮⁦-⁩]")
#: 裁决 R-P3：Windows 8.3 短名形态的分段。基名是 1–6 个非 "." 字符的前缀 + "~" + 数字，基名总长不超过 8
#: （8.3 格式的上限），可带 1–3 个字符的扩展名；大小写不敏感（字符类不区分大小写）。
#: 在 Windows 上这样的名称可能是另一文件的别名（例如 AGENTS~1.MD），会绕过按字符串比较的保护路径。
#: P4 第 3 轮（复审 paths 视角，major）：前缀**允许含 "~"**。长名前 6 个有效字符含 "~" 时，Windows 自动生成的
#: 短名前缀也含 "~"（例如 ~$document.docx → ~$DOCU~1.DOC、.~agentconfig → ~AGENT~1、哈希形态 ~~1BC3~1.MD）；
#: 原来的“前缀不含 ~”漏掉了这一类。代价是 a~b~1、x~y~2.txt 这类基名不超过 8 的名称也被拒绝（保守方向）。
_SHORT_NAME_RE = re.compile(r"([^.]{1,6}~[0-9]+)(?:\.[^.]{1,3})?")


def _is_short_name_form(segment: str) -> bool:
    match = _SHORT_NAME_RE.fullmatch(segment)
    return match is not None and len(match.group(1)) <= 8
#: 设备名比较只做 ASCII 大小写折叠（Windows 的设备名匹配不按 Unicode 大小写映射，
#: 例如 "CON\u0131N$"（U+0131）在本机是普通文件名，不能被 str.upper() 误映射成 CONIN$）。
_ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def validate_business_path(value: Any, *, field: str = "/path") -> str:
    """可移植词法 profile（4.1.5）。只做词法检查，不是授权或文件系统安全检查；原样返回，不归一化。

    拒绝：非 str 或空串、孤立代理项、超长、绝对路径与 \\ : < > " | ? *、Unicode Cc 控制字符、行/段分隔符
    与双向控制字符（裁决 R-P4）、空段/./..、以点或空格结尾的段、超长段（以上 E_PATH_SYNTAX）；Windows
    保留设备名与 8.3 短名形态的分段（裁决 R-P3，E_PATH_RESERVED）。整串检查（类型到双向控制字符）之后
    按分段依次检查：每段先做语法检查，再检查设备名与 8.3 短名形态，所以前一段的保留名先于后一段的语法
    错误报告；多缺陷输入不承诺首错（第 3.2 节）。

    词法合法仍不代表对象身份唯一：大小写不同的拼写在词法层都合法，却可能指向同一个磁盘对象（用 fsutil
    手工设置、不含 "~" 的短名同理）。物理层的 resolve_existing_path_for_review 与 sealing 会核对磁盘上
    登记的真实名称；只按字符串比较业务路径的下游（例如保护范围）必须知道这一点。
    全角或兼容字符不拒绝（裁决 R-P5）：把业务路径交给 ANSI 代码页 API 的消费者必须自行复核 best-fit 映射。
    """
    def reject(code: str, reason: str) -> ProtocolError:
        return ProtocolError(code, field=field, phase="path", reason=reason)

    if type(value) is not str or not value:
        raise reject("E_PATH_SYNTAX", "nonempty_string_required")
    if _has_surrogate(value):
        raise reject("E_PATH_SYNTAX", "unpaired_surrogate")
    if len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise reject("E_PATH_SYNTAX", "path_byte_limit_exceeded")
    if value.startswith("/") or any(char in _PATH_FORBIDDEN for char in value):
        raise reject("E_PATH_SYNTAX", "nonportable_path_syntax")
    if _CONTROL_RE.search(value) is not None:
        raise reject("E_PATH_SYNTAX", "control_character")
    if _LINE_SEPARATOR_OR_BIDI_RE.search(value) is not None:
        raise reject("E_PATH_SYNTAX", "line_separator_or_bidi_control")
    for segment in value.split("/"):
        if segment in ("", ".", ".."):
            raise reject("E_PATH_SYNTAX", "invalid_path_component")
        if segment.endswith((".", " ")):
            raise reject("E_PATH_SYNTAX", "segment_ends_with_dot_or_space")
        if len(segment.encode("utf-8")) > MAX_SEGMENT_BYTES:
            raise reject("E_PATH_SYNTAX", "segment_byte_limit_exceeded")
        # 带扩展名（NUL.txt、nul.tar.gz）与基名末尾空格（"NUL .txt"）同样视为设备名。
        base = segment.split(".", 1)[0].rstrip(" ").translate(_ASCII_UPPER)
        if base in _RESERVED_DEVICE_NAMES:
            raise reject("E_PATH_RESERVED", "reserved_device_name")
        if _is_short_name_form(segment):
            raise reject("E_PATH_RESERVED", "windows_short_name_form")
    return value


def _collision_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _collect_paths(paths: Iterable[str]) -> list[str]:
    if isinstance(paths, (str, bytes)):
        raise ProtocolError("E_SCHEMA_TYPE", phase="path", reason="iterable_of_paths_required")
    items = list(paths)
    for item in items:
        if type(item) is not str:
            raise ProtocolError("E_SCHEMA_TYPE", phase="path", reason="path_must_be_string")
    return items


def find_name_collisions(paths: Iterable[str]) -> list[list[str]]:
    """按 unicodedata.normalize("NFC", p).casefold() 对**整条路径**分组，返回含多个不同原始拼写的组
    （排序后）。

    这是跨平台保守近似，不等于任何一个文件系统的精确比较规则；完全相同的拼写不算碰撞。
    目录前缀碰撞与“文件/目录同名”见 check_no_name_collisions。
    """
    groups: dict[str, set[str]] = {}
    for item in _collect_paths(paths):
        groups.setdefault(_collision_key(item), set()).add(item)
    return sorted(sorted(group) for group in groups.values() if len(group) > 1)


def check_no_name_collisions(paths: Iterable[str]) -> None:
    """发现跨平台名称碰撞时抛 E_PATH_COLLISION（不回显路径），按以下顺序检查：

    1. 整条路径在 NFC + casefold 后同名而拼写不同（reason=case_or_normalization_collision）；
    2. 某一级目录前缀在 NFC + casefold 后同名而拼写不同，例如 Fixtures/a.json 与 fixtures/b.json
       （在 Windows/macOS 上落进同一目录，在 Linux 上是两个目录；reason=directory_prefix_collision）；
    3. 同一名称（NFC + casefold 后）既是文件又是目录前缀，例如 data 与 DATA/x.json 或 data/x.json
       （reason=file_directory_conflict）。
    """
    items = _collect_paths(paths)
    if find_name_collisions(items):
        raise ProtocolError("E_PATH_COLLISION", phase="path", reason="case_or_normalization_collision")
    full_keys = {_collision_key(item) for item in items}
    prefixes: dict[str, set[str]] = {}
    for item in items:
        segments = item.split("/")
        for end in range(1, len(segments)):
            prefix = "/".join(segments[:end])
            prefixes.setdefault(_collision_key(prefix), set()).add(prefix)
    if any(len(spellings) > 1 for spellings in prefixes.values()):
        raise ProtocolError("E_PATH_COLLISION", phase="path", reason="directory_prefix_collision")
    if full_keys & set(prefixes):
        raise ProtocolError("E_PATH_COLLISION", phase="path", reason="file_directory_conflict")


_WINDOWS_VERBATIM_UNC_PREFIX = "\\\\?\\UNC\\"
_WINDOWS_VERBATIM_PREFIX = "\\\\?\\"

#: 业务路径物理检查（resolve_existing_path_for_review、_check_link_hops 逐跳核对，以及 sealing 的路径步进）
#: 的“已知不检测/不保证”清单（裁决 R-P9）。物理检查是静态审阅 + 纵深防御，不是安全访问保证（01-D06；
#: 设计文档 4.1.6–4.1.7）。已检测的情形见 protocols-api.md 第 7 节“物理检查的定位（R-P9）”。
PATH_REVIEW_LIMITATIONS = (
    "不消除 TOCTOU，不防并发替换：返回的路径不是访问凭证，检查与使用之间路径可能被替换",
    "检查之后父目录或目标被替换（包括被换成指向根外的链接）时，已完成的检查不再成立",
    "硬链接不检测：与根外文件共享数据的硬链接在路径层面与普通文件无法区分，会被接受",
    "挂载点不保证：Windows 上以卷 GUID 为目标（\\\\?\\Volume{GUID}\\ 形式）的 junction 按不受支持的写法失败关闭"
    "（本机用 FSCTL 造出的这种 junction 实测），经挂载管理器登记的真实卷挂载点未实测；POSIX 的 bind mount 等挂载"
    "不是链接，不检测",
    "NT 层与 Win32 层名称语义差异：只覆盖已枚举的写法（分段末尾的点与空格、目标文本的各种写法、名称代理类"
    "重解析点）；尚未枚举的写法不保证被识别",
    "不带名称代理位的重解析点（去重、云文件、WOF 压缩、AppExecLink 等）按普通名称处理：处理它们的过滤驱动若在"
    "打开时重定向，不在检测范围内",
    "POSIX 平台（Linux/macOS）未实测（NOT_RUN）：按物理语义处理 \"..\" 的分支只有白盒注入",
    "不防同一账户下其他进程的篡改",
    "根目录本身（含其祖先与根路径上的链接）经 resolve 后作为可信起点，不做逐跳核对；根的可信性由调用方负责",
)


def _exact_parts(path: Any) -> tuple[str, ...]:
    """路径组件（逐字）；Windows 路径去掉 resolve 可能保留的 \\\\?\\ 前缀（超长路径，或含以点、空格结尾的目录名）。"""
    if isinstance(path, PureWindowsPath):
        text = str(path)
        if text.startswith(_WINDOWS_VERBATIM_UNC_PREFIX):
            path = PureWindowsPath("\\\\" + text[len(_WINDOWS_VERBATIM_UNC_PREFIX):])
        elif text.startswith(_WINDOWS_VERBATIM_PREFIX):
            path = PureWindowsPath(text[len(_WINDOWS_VERBATIM_PREFIX):])
    return tuple(path.parts)


def _is_within(root: Any, target: Any) -> bool:
    """target 是否位于 root 之内（含 root 本身）：root 的组件必须是 target 组件的**逐字**前缀。

    两个参数都应来自 resolve(strict=True)：Windows 上由 GetFinalPathNameByHandle 给出磁盘上的真实拼写，
    POSIX 上是 realpath 结果（NOT_RUN），所以按组件逐字比较。它只比较两个路径字符串，不核对对象身份：
    硬链接、挂载点与检查之后的替换都不在它的判断范围内（见 PATH_REVIEW_LIMITATIONS，裁决 R-P9）。
    **不用 PurePath.relative_to**：PureWindowsPath 用 str.lower() 比较，而 NTFS 的名称等价规则不同——
    K 与 U+212A（开尔文符号）、å 与 U+212B、ω 与 U+2126、ß 与 U+1E9E 等在 lower() 后相同，在 NTFS
    上却是两个名称；按目录启用了大小写敏感的 NTFS 目录中任何字母都是如此。relative_to 会把这类根外的
    兄弟目录当成根内（P2 返工，复审 paths 视角 blocker）。

    拼写不同一律视为越界（保守方向，可能误报 E_PATH_ESCAPE，例如经卷挂载点解析到另一卷）。protocols 与
    sealing 共用本函数。
    """
    root_parts, target_parts = _exact_parts(root), _exact_parts(target)
    return len(target_parts) >= len(root_parts) and target_parts[:len(root_parts)] == root_parts


def _check_listed_name(directory: Path, name: str, *, field: str | None = "/path",
                       phase: str = "path") -> None:
    """确认 name 是 directory 中**登记的真实名称**（逐字相同）。

    Windows/macOS 的大小写不敏感查找、Windows 8.3 短名都能让另一种拼写解析到同一对象；这里据
    os.listdir 的结果拒绝这类别名：NFC + casefold 后与某个已有名称相同 → E_PATH_COLLISION；
    其他未列出的别名（例如 8.3 短名）→ E_PATH_RESOLUTION_FAILED。只列出目录名称，不读取文件内容。
    它只核对名称拼写，不核对对象身份（硬链接不检测，见 PATH_REVIEW_LIMITATIONS）。
    """
    try:
        names = os.listdir(directory)
    except OSError:
        raise ProtocolError("E_PATH_RESOLUTION_FAILED", field=field, phase=phase,
                            reason="directory_listing_failed") from None
    if name in names:
        return
    key = _collision_key(name)
    if any(_collision_key(listed) == key for listed in names):
        raise ProtocolError("E_PATH_COLLISION", field=field, phase=phase,
                            reason="spelling_differs_from_existing_name")
    raise ProtocolError("E_PATH_RESOLUTION_FAILED", field=field, phase=phase,
                        reason="noncanonical_filesystem_alias")


#: 逐跳展开链接链时的跳数上限（Windows 的重解析点上限为 63，POSIX 通常为 40）。
_MAX_LINK_HOPS = 64
_WINDOWS_DEVICE_PREFIX = "\\\\.\\"


def _os_path(path: Path) -> Any:
    """逐跳展开时交给 os.lstat、os.readlink 与 resolve 的路径形式（P4 第 4 轮，复审 paths 视角 major）。

    Windows 上把绝对路径转成逐字形式（\\\\?\\、\\\\?\\UNC\\）。Win32 路径规范化会去掉分段末尾的点与空格
    （"hop." 变成 "hop"），而内核解析重解析点的目标文本时在 NT 层逐字使用名称。经过 Win32 规范化的逐跳
    核对看到的是另一个对象（例如 "hop." 是指向根外的链接，"hop" 不存在），会漏掉链上的根外链接。前提：
    path 是绝对路径，且不含 "."、".." 分段（它来自 resolve 的结果，或来自已按词法折叠的分段）。
    POSIX 原样返回。
    """
    if not isinstance(path, PureWindowsPath):
        return path
    text = str(path)
    if text.startswith(_WINDOWS_VERBATIM_PREFIX):
        return text
    if text.startswith("\\\\"):
        return _WINDOWS_VERBATIM_UNC_PREFIX + text[2:]
    return _WINDOWS_VERBATIM_PREFIX + text


#: Windows 重解析点标签的“名称代理”位（IsReparseTagNameSurrogate）：带这一位的重解析点代表另一个命名实体，
#: OS（或文件系统过滤驱动）解析路径时可能跟随它。
_REPARSE_TAG_NAME_SURROGATE = 0x20000000


class _UnsupportedLinkKind(Exception):
    """_link_target_text 遇到无法确定语义的链接类型（R-P9 失败关闭）；调用方映射为 E_PATH_RESOLUTION_FAILED。"""


def _link_target_text(path: Path) -> str | None:
    """path 是 symlink 或 Windows junction/挂载点（IO_REPARSE_TAG_MOUNT_POINT）时返回 os.readlink 的目标文本；
    不是链接（包括不存在）时返回 None。其他 OSError 原样抛出，由调用方映射。Windows 上按逐字路径访问
    （_os_path），名称末尾的点与空格不会被去掉。

    R-P9（失败关闭）：lstat 报告的重解析点标签带名称代理位、却既不是 symlink 也不是 junction/挂载点（例如
    LX_SYMLINK、WCI_LINK、GLOBAL_REPARSE 或第三方标签）时抛 _UnsupportedLinkKind。这类对象由过滤驱动决定
    指向哪里，逐跳核对无法得知目标，不能当作普通名称放行（P4 之前返回 None，逐跳核对把它当作普通名称跳过）。
    不带名称代理位的重解析点（去重、云文件、WOF 压缩、AppExecLink 等）不改变名称指向，仍按普通名称处理。
    """
    os_path = _os_path(path)
    try:
        info = os.lstat(os_path)
    except FileNotFoundError:
        return None
    tag = getattr(info, "st_reparse_tag", 0) or 0
    if stat.S_ISLNK(info.st_mode) or tag == stat.IO_REPARSE_TAG_MOUNT_POINT:
        return os.readlink(os_path)
    if tag & _REPARSE_TAG_NAME_SURROGATE:
        raise _UnsupportedLinkKind(tag)
    return None


def _split_link_target(text: str, where: Path) -> tuple[Path, list[str]] | None:
    """把链接目标文本拆成（起点目录, 待处理分段）。写法不受支持或语义无法确定时返回 None，由调用方按解析
    失败处理（失败关闭，裁决 R-P9）。where 是链接所在的真实目录（resolve 的结果，其中没有链接）。

    POSIX（设计意图，NOT_RUN）：相对目标从 where 起算；绝对目标从 "/" 起算。分段中的 ".." 原样留给调用方，
    按物理语义处理：先跟随它前面的链接，再取真实目录的上一级。本机没有 POSIX 环境，这一分支只有白盒注入。

    Windows 已支持的写法（本机实测内核的处理方式，逐条对应测试）：
    - 相对目标（不以 "\\" 开头、不带盘符）：I/O 管理器把链接所在目录与目标文本拼接，再按**词法**折叠 "."、
      ".." 与空分段：`x\\..\\y` 等同于 `y`，不访问 x（P4 第 4 轮，复审 fidelity 与 paths 视角 major）。这里同样
      先按词法折叠：目标文本内部的 "<名称>\\.." 成对消去，空分段与 "." 跳过；折叠后剩下的开头 ".." 作用于
      where。where 中没有链接，所以它的词法上一级就是物理上一级；".." 越过盘符根返回 None。
    - 逐字绝对目标（\\\\?\\C:\\… 与 \\\\?\\UNC\\…，即 os.symlink 与 junction 写下的 \\??\\ 形式）：从其根起算，
      末尾的一个分隔符（目录链接常见）忽略。
    以下写法返回 None（R-P9 起，此前部分写法被猜测为可解析）：
    - 逐字绝对目标中含 "."、".." 或空分段：本机实测内核拒绝解析（ERROR_INVALID_NAME、ERROR_PATH_NOT_FOUND），
      而不是按词法折叠；
    - 不带 \\\\?\\ 的盘符绝对路径（"C:\\x"）：本机实测相对与绝对两种标志位下内核都拒绝（ERROR_INVALID_NAME）；
      只有盘符的半相对路径（"C:x"）；不带 \\\\?\\ 的 UNC 文本；
    - 只有根、没有盘符的目标（"\\x"）：os.readlink 不给出相对/绝对标志位，这种文本在相对标志下从链接所在
      盘的根起算（本机实测按词法折叠），在绝对标志下却是 NT 命名空间路径（本机实测 "\\Device\\HarddiskVolumeN\\…"
      形式的绝对链接能被内核跟随），语义无法确定（P4 第 4 轮曾按前者支持）；
    - 卷 GUID 等其他逐字设备路径（含卷挂载点的目标）与 \\\\.\\ 设备路径；
    - 含 "/" 的目标（NT 层不把 "/" 当分隔符，本机实测这类链接无法解析）。
    """
    if not isinstance(where, PureWindowsPath):
        target = PurePath(text)
        if target.is_absolute():
            return Path(target.anchor), list(target.parts[1:])
        return where, list(target.parts)
    if "/" in text:
        return None
    verbatim = True
    if text.startswith(_WINDOWS_VERBATIM_UNC_PREFIX):
        text = "\\\\" + text[len(_WINDOWS_VERBATIM_UNC_PREFIX):]
    elif (text.startswith(_WINDOWS_VERBATIM_PREFIX) and len(text) >= 6 and text[5] == ":"
          and text[4].isascii() and text[4].isalpha()):
        text = text[len(_WINDOWS_VERBATIM_PREFIX):]
    else:
        verbatim = False
    if text.startswith((_WINDOWS_VERBATIM_PREFIX, _WINDOWS_DEVICE_PREFIX)):
        return None
    target = PureWindowsPath(text)
    if target.anchor:
        # 只接受逐字绝对目标（\\?\C:\…、\\?\UNC\…），且必须有根；其余带锚点的写法语义无法确定或内核拒绝。
        if not verbatim or not target.root or not text.startswith(target.anchor):
            return None
        absolute = text[len(target.anchor):].split("\\")
        if absolute and absolute[-1] == "":
            absolute.pop()  # 末尾的一个分隔符（本机实测目录 symlink 与 junction 都照常解析）
        if any(part in ("", ".", "..") for part in absolute):
            return None
        return Path(target.anchor), absolute
    start: Path = where
    parts: list[str] = []
    ups = 0
    for part in text.split("\\"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            else:
                ups += 1
            continue
        parts.append(part)
    for _ in range(ups):
        if start.parent == start:
            return None
        start = start.parent
    return start, parts


def _check_link_hops(root: Path, directory: Path, name: str, *, field: str | None = "/path",
                     phase: str = "path", resolved: Path | None = None) -> None:
    """逐跳核对 directory/name 的链接链（P4 第 3 轮，复审 fidelity 视角 major；P4 第 4 轮修正；R-P9 定位）。

    定位（裁决 R-P9）：这是静态审阅 + 纵深防御，不是安全访问保证（01-D06）。下面只列已检测的情形；已知
    不检测、不保证的情形见 PATH_REVIEW_LIMITATIONS（TOCTOU 与并发替换、检查后父目录或目标被替换、硬链接与
    挂载点、尚未枚举的 NT/Win32 写法、POSIX 未实测、同账户进程篡改等）。

    directory 必须是已确认位于根内的真实目录（resolve 的结果）。resolve(strict=True) 只给出链接链的最终
    目标：根内链接 → 根外链接 → 根内目标这样的链，最终目标在根内，结果却依赖根外对象。这里用
    os.lstat/os.readlink 按下述规则逐跳展开。已检测的情形：
    - 展开时遇到的链接对象（symlink、junction，包括目标文本中经过的中间链接）所在目录解析后的真实路径不在
      根内（按 _is_within 逐字比较）→ E_PATH_ESCAPE（reason=link_hop_outside_trusted_root）；目标文本中经过的
      非链接目录（例如根的祖先目录、8.3 形式的用户目录）只决定名称如何拼接，不单独判定；
    - Windows 相对目标中的 "."、".." 与空分段按词法折叠（本机实测内核同样如此，P4 第 4 轮）；名称按逐字路径
      访问（_os_path），分段末尾的点与空格不被 Win32 规范化去掉（P4 第 4 轮）；
    - 以下情形失败关闭，报 E_PATH_RESOLUTION_FAILED：目标写法不受支持或语义无法确定（link_target_form_unsupported，
      写法清单见 _split_link_target）；带名称代理位、却不是 symlink/junction 的重解析点（link_kind_unsupported，
      R-P9）；跳数超过 _MAX_LINK_HOPS（link_hop_limit_exceeded）；中间分段无法读取或解析（link_hop_unreadable、
      link_hop_unresolvable）；
    - resolved 给出时（各入口都传入这一级 resolve(strict=True) 的结果），链上至少有一个链接的情况下，逐跳
      展开的终点必须与它是同一路径（按 _exact_parts 逐字比较），否则说明逐跳展开与 OS 的解析走了不同的链，
      报 E_PATH_RESOLUTION_FAILED（reason=link_walk_diverged）。这只是兜底：终点相同不能证明途经的链接相同。
    POSIX 按物理语义处理 ".."（设计意图，NOT_RUN）。只读元数据（lstat、readlink、resolve），不读文件内容。
    protocols 与 sealing 共用本函数。
    """
    def failed(reason: str) -> ProtocolError:
        return ProtocolError("E_PATH_RESOLUTION_FAILED", field=field, phase=phase, reason=reason)

    where = directory
    final = directory
    pending = [name]
    hops = 0
    while pending:
        component = pending.pop(0)
        if component in ("", "."):
            final = where
            continue
        if component == "..":  # 只在 POSIX 上出现（Windows 的分段已按词法折叠）：物理上一级
            where = where.parent
            final = where
            continue
        candidate = where / component
        final = candidate
        try:
            target_text = _link_target_text(candidate)
        except _UnsupportedLinkKind:
            raise failed("link_kind_unsupported") from None
        except OSError:
            raise failed("link_hop_unreadable") from None
        if target_text is None:
            if pending:  # 中间的非链接分段：取真实拼写，后续分段在其下继续展开
                try:
                    where = Path(_os_path(candidate)).resolve(strict=True)
                except (OSError, RuntimeError):
                    raise failed("link_hop_unresolvable") from None
            continue
        if not _is_within(root, where):
            raise ProtocolError("E_PATH_ESCAPE", field=field, phase=phase,
                                reason="link_hop_outside_trusted_root")
        hops += 1
        if hops > _MAX_LINK_HOPS:
            raise failed("link_hop_limit_exceeded")
        split = _split_link_target(target_text, where)
        if split is None:
            raise failed("link_target_form_unsupported")
        where, parts = split
        final = where
        pending[:0] = parts
    if hops and resolved is not None:
        try:
            walked = Path(_os_path(final)).resolve(strict=True)
        except (OSError, RuntimeError):
            raise failed("link_walk_diverged") from None
        if _exact_parts(walked) != _exact_parts(resolved):
            raise failed("link_walk_diverged")


def resolve_existing_path_for_review(root: Path, business_path: str) -> Path:
    """静态审阅用：把业务路径解析到受信根下已存在的对象（跟随 symlink 与 Windows junction）。

    定位（裁决 R-P9）：静态审阅 + 纵深防御，不是安全访问保证（01-D06；4.1.6–4.1.7）。已知不检测、不保证的
    情形见 PATH_REVIEW_LIMITATIONS：TOCTOU 与并发替换、检查后父目录或目标被替换、硬链接与挂载点、尚未枚举
    的 NT/Win32 写法、POSIX 未实测、同账户进程篡改、根路径本身不逐跳核对。

    已检测的情形（按检查顺序）：
    - 每一级 resolve(strict=True) 的最终目标不在 resolve 后的根内 → E_PATH_ESCAPE（resolved_outside_trusted_root）；
      多分段路径的中间一级落到根外时在那一级报告，只在已确认位于根内的目录中列目录名称；
    - 这一级的链接链经 _check_link_hops 逐跳核对：展开时遇到位于根外的链接对象 → E_PATH_ESCAPE
      （link_hop_outside_trusted_root；P4 第 3 轮，此前同一分段内“根内链接 → 根外链接 → 根内目标”的链被接受）；
      Windows 相对目标按词法折叠 "." 与 ".."，名称按逐字路径访问（P4 第 4 轮）；目标写法不受支持或语义无法
      确定、未识别的名称代理类重解析点、展开终点与 resolve 结果不同时失败关闭（E_PATH_RESOLUTION_FAILED）；
    - 这一级的拼写不是上一级目录中登记的真实名称：大小写或 Unicode 形式别名 E_PATH_COLLISION，其他未登记的
      别名 E_PATH_RESOLUTION_FAILED（自动生成的 8.3 短名形态已在词法层以 E_PATH_RESERVED 拒绝，裁决 R-P3，这里
      兜底的是手工设置的短名，例如 fsutil 设置的 SHORTX）；
    - 目标不存在（含中间段是文件、悬空的链接）E_PATH_NOT_FOUND；链接环等解析失败 E_PATH_RESOLUTION_FAILED。
    “在根内”按路径组件逐字比较（_is_within），不按 str.lower()：与根的某级祖先只在 NTFS 名称等价规则之外
    “看起来同名”的兄弟目录（例如名称含 U+212A 开尔文符号，或位于大小写敏感目录中）算根外。

    返回的 Path 是相对 resolve 后的根计算的真实路径。它**不是访问凭证**，不能用于随后按路径
    open()，也**不消除 TOCTOU**（检查与使用之间路径可能被替换，4.1.6、01-D06）。
    """
    validate_business_path(business_path)
    try:
        trusted_root = Path(root).resolve(strict=True)
        root_is_dir = trusted_root.is_dir()
    except (OSError, RuntimeError):
        raise ProtocolError("E_PATH_ROOT", field=None, phase="path",
                            reason="trusted_root_unresolvable") from None
    if not root_is_dir:
        raise ProtocolError("E_PATH_ROOT", phase="path", reason="trusted_root_not_directory")
    current = trusted_root
    for segment in business_path.split("/"):
        try:
            resolved = (current / segment).resolve(strict=True)
        except (FileNotFoundError, NotADirectoryError):
            raise ProtocolError("E_PATH_NOT_FOUND", field="/path", phase="path",
                                reason="target_not_found") from None
        except (OSError, RuntimeError):
            raise ProtocolError("E_PATH_RESOLUTION_FAILED", field="/path", phase="path",
                                reason="static_resolution_failed") from None
        if not _is_within(trusted_root, resolved):
            raise ProtocolError("E_PATH_ESCAPE", field="/path", phase="path",
                                reason="resolved_outside_trusted_root")
        _check_link_hops(trusted_root, current, segment, field="/path", phase="path",
                         resolved=resolved)
        _check_listed_name(current, segment)
        current = resolved
    return current


# ---------------------------------------------------------------------------
# ResourceRef 与资源类型注册表（4.2.1–4.2.3、4.2.8）
# ---------------------------------------------------------------------------

class _FrozenRecord:
    """构造后不可变的小型登记项基类。

    不用 dataclasses：本模块可能被测试按文件加载而不登记到 sys.modules，此时带字符串注解的
    dataclass 在类创建时会失败。
    """

    __slots__ = ()
    _fields: tuple[str, ...] = ()

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def _key(self) -> tuple[Any, ...]:
        return tuple(getattr(self, name) for name in self._fields)

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return self._key() == other._key()  # type: ignore[attr-defined]

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:
        inner = ", ".join(f"{name}={getattr(self, name)!r}" for name in self._fields)
        return f"{type(self).__name__}({inner})"


class _Undeclared:
    """ResourceKind 中“未声明”的登记项字段（区别于显式声明的 None）。这样的登记项不能进入注册表。"""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<undeclared>"

    def __copy__(self) -> "_Undeclared":
        return self

    def __deepcopy__(self, memo: Any) -> "_Undeclared":
        return self


_UNDECLARED = _Undeclared()
#: 资源载荷的表示（裁决 R-P10，文档 4.2.1“允许的载荷表示”）。
PAYLOAD_REPRESENTATIONS = ("json", "text", "markdown", "binary")
#: 登记类型时必须声明的三项（裁决 R-P10）；未声明的登记项不能进入 ResourceKindRegistry（E_SCHEMA_FIELDS）。
RESOURCE_KIND_DECLARED_FIELDS = ("schema_ref", "payload_representations", "payload_validator")
#: schema_ref：本地受信 Schema 标识（可打印 ASCII，不含空白，1–256 个字符）或 None。01 不加载 Schema 文件。
_SCHEMA_REF_RE = re.compile(r"[\x21-\x7e]{1,256}")


class ResourceKind(_FrozenRecord):
    """资源类型登记项（构造后不可变）。is_file=True 时引用必须带 path。

    裁决 R-P10（文档 4.2.1“kind 注册表应包含 schema、是否文件资源、允许的载荷表示、语义校验器及所属模块”）
    增加三项，均为关键字参数：
    - schema_ref：本地受信 Schema 标识，或 None（没有独立 Schema 文件）。01 只登记，不加载 Schema 文件；
    - payload_representations：允许的载荷表示，PAYLOAD_REPRESENTATIONS（json、text、markdown、binary）的非空
      子集，保存为 frozenset；
    - payload_validator：可调用对象 payload_validator(raw: bytes, registry) 或 None；载荷不合法时抛 ProtocolError。
    三项都可以不传（属性值为内部的“未声明”标记，见 undeclared_fields()），但未声明的登记项不能进入
    ResourceKindRegistry：构造或 extended() 时报 E_SCHEMA_FIELDS（reason=resource_kind_registration_incomplete）。
    显式传入的值不合法时，与其他字段一样抛 ValueError。
    """

    __slots__ = ("name", "is_file", "owner_module", "description", "schema_ref", "payload_representations",
                 "payload_validator")
    _fields = ("name", "is_file", "owner_module", "description", "schema_ref", "payload_representations",
               "payload_validator")

    def __init__(self, name: str, is_file: bool, owner_module: str, description: str, *,
                 schema_ref: Any = _UNDECLARED, payload_representations: Any = _UNDECLARED,
                 payload_validator: Any = _UNDECLARED) -> None:
        if type(name) is not str or _KIND_RE.fullmatch(name) is None:
            raise ValueError("ResourceKind.name must match ^[a-z][a-z0-9-]{0,63}$")
        if type(is_file) is not bool:
            raise ValueError("ResourceKind.is_file must be bool")
        if type(owner_module) is not str or not owner_module:
            raise ValueError("ResourceKind.owner_module must be a non-empty str")
        if type(description) is not str:
            raise ValueError("ResourceKind.description must be str")
        if (schema_ref is not _UNDECLARED and schema_ref is not None
                and (type(schema_ref) is not str or _SCHEMA_REF_RE.fullmatch(schema_ref) is None)):
            raise ValueError("ResourceKind.schema_ref must be None or a printable ASCII identifier "
                             "of 1..256 characters")
        if payload_representations is not _UNDECLARED:
            if isinstance(payload_representations, (str, bytes)) or not isinstance(payload_representations,
                                                                                   Iterable):
                raise ValueError("ResourceKind.payload_representations must be a collection of representations")
            items = list(payload_representations)
            if (not items or any(type(item) is not str or item not in PAYLOAD_REPRESENTATIONS for item in items)):
                raise ValueError("ResourceKind.payload_representations must be a non-empty subset of "
                                 "json/text/markdown/binary")
            payload_representations = frozenset(items)
        if (payload_validator is not _UNDECLARED and payload_validator is not None
                and not callable(payload_validator)):
            raise ValueError("ResourceKind.payload_validator must be callable or None")
        for field_name, value in zip(self._fields, (name, is_file, owner_module, description, schema_ref,
                                                    payload_representations, payload_validator)):
            object.__setattr__(self, field_name, value)

    def undeclared_fields(self) -> tuple[str, ...]:
        """RESOURCE_KIND_DECLARED_FIELDS 中尚未声明的项（按固定顺序）；为空才能登记。"""
        return tuple(name for name in RESOURCE_KIND_DECLARED_FIELDS if getattr(self, name) is _UNDECLARED)


class _FrozenRegistry:
    """构造后不可变的名称 -> 登记项映射；extended() 返回新对象，不改原对象。"""

    __slots__ = ("_items",)
    _item_type: type = object

    def __init__(self, items: Iterable[Any] = ()) -> None:
        table: dict[str, Any] = {}
        for item in items:
            if not isinstance(item, self._item_type):
                raise TypeError(f"{type(self).__name__} accepts only {self._item_type.__name__}")
            if item.name in table:
                raise ValueError("duplicate registry entry")
            table[item.name] = item
        object.__setattr__(self, "_items", MappingProxyType(table))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def get(self, name: str) -> Any:
        return self._items.get(name) if type(name) is str else None

    def __contains__(self, name: object) -> bool:
        return type(name) is str and name in self._items

    def __getitem__(self, name: str) -> Any:
        return self._items[name]

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def names(self) -> tuple[str, ...]:
        return tuple(self._items)

    def items(self) -> tuple[tuple[str, Any], ...]:
        return tuple(self._items.items())

    def extended(self, items: Iterable[Any]) -> Any:
        """返回包含原登记项与新登记项的新注册表；同名项不覆盖，抛 ValueError。"""
        return type(self)(list(self._items.values()) + list(items))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({list(self._items)!r})"


class ResourceKindRegistry(_FrozenRegistry):
    """受信核心的资源类型注册表（4.2.1）。不由模型或插件在运行时改写。

    裁决 R-P10：每个登记项都必须已声明 schema_ref、payload_representations、payload_validator 三项；构造与
    extended() 都检查，缺任一项报 E_SCHEMA_FIELDS（field 为 "/<首个缺失项>"，reason=
    resource_kind_registration_incomplete）。检查顺序：登记项类型（TypeError）→ 三项已声明 → 同名项（ValueError，
    不覆盖）。
    """
    __slots__ = ()
    _item_type = ResourceKind

    def __init__(self, items: Iterable[Any] = ()) -> None:
        items = list(items)
        for item in items:
            if not isinstance(item, ResourceKind):
                continue  # 由基类报 TypeError
            missing = item.undeclared_fields()
            if missing:
                raise ProtocolError("E_SCHEMA_FIELDS", field=_pointer("", missing[0]), phase="resource",
                                    reason="resource_kind_registration_incomplete")
        super().__init__(items)

    def is_file_kind(self, name: str) -> bool:
        kind = self.get(name)
        return bool(kind is not None and kind.is_file)


def _validate_fixture_manifest_payload(raw: bytes, registry: ResourceKindRegistry) -> None:
    """fixture-manifest 的载荷校验器（R-P10）：严格解析后按 validate_fixture_manifest_set 校验整份清单。"""
    validate_fixture_manifest_set(strict_json_loads(raw), registry)


def _validate_public_schema_payload(raw: bytes, registry: ResourceKindRegistry) -> None:
    """public-schema 的载荷校验器（R-P10）：严格解析；根必须是对象，且 "$id" 恰为 SCHEMA_ID。

    只核对身份，不做 JSON Schema 元校验（本机无 jsonschema，01-L07）。
    """
    document = strict_json_loads(raw)
    if type(document) is not dict:
        raise ProtocolError("E_SCHEMA_TYPE", phase="resource", reason="object_required")
    declared = document.get("$id")
    if type(declared) is not str or declared != SCHEMA_ID:
        raise ProtocolError("E_SCHEMA_FIELDS", field="/$id", phase="resource", reason="public_schema_id_mismatch")


def build_default_registry() -> ResourceKindRegistry:
    """只登记 01 自己拥有的类型，以及由 01 预登记封装、语义归 04 的 raw-segment。

    裁决 R-P10 的三项（schema_ref 都为 None：01 没有为这些类型单独登记 Schema 文件，结构由校验器代码表达）：
    - fixture：json/text/markdown/binary，无校验器；
    - fixture-manifest：json，校验器为严格解析加 validate_fixture_manifest_set；
    - public-schema：json，校验器为严格解析加 "$id" 核对；
    - baseline-evidence：json/text/binary，无校验器；
    - raw-segment：binary，无校验器（分段语义归 04）。
    """
    return ResourceKindRegistry([
        ResourceKind("fixture", True, "01", "01 兼容/协议测试样本文件（附录 C、5.3）",
                     schema_ref=None, payload_representations=("json", "text", "markdown", "binary"),
                     payload_validator=None),
        ResourceKind("fixture-manifest", True, "01", "fixture 来源清单文件（附录 C.1）",
                     schema_ref=None, payload_representations=("json",),
                     payload_validator=_validate_fixture_manifest_payload),
        ResourceKind("public-schema", True, "01", "本地公共协议 Schema 文件（附录 A、01-L02）",
                     schema_ref=None, payload_representations=("json",),
                     payload_validator=_validate_public_schema_payload),
        ResourceKind("baseline-evidence", True, "01", "基线取证记录与归档文件（01-T1）",
                     schema_ref=None, payload_representations=("json", "text", "binary"),
                     payload_validator=None),
        ResourceKind("raw-segment", True, "04",
                     "已封存的原始流分段（4.3.3）；01 只预登记引用封装，分段语义、阈值与脱敏由 04 负责",
                     schema_ref=None, payload_representations=("binary",), payload_validator=None),
    ])


def _require_registry(registry: Any) -> ResourceKindRegistry:
    if not isinstance(registry, ResourceKindRegistry):
        raise TypeError("registry must be a ResourceKindRegistry")
    return registry


_REF_REQUIRED = ("kind", "id", "sha256")


def _validate_ref_shape(ref: Any, field: str) -> None:
    _expect_dict(ref, field, phase="resource")
    _require_closed(ref, _REF_REQUIRED, field=field, optional=("path",))
    for name, regex in (("kind", _KIND_RE), ("id", _LOGICAL_ID_RE), ("sha256", _SHA256_RE)):
        value = ref[name]
        if type(value) is not str or regex.fullmatch(value) is None:
            raise ProtocolError("E_RESOURCE_FIELD", field=_pointer(field, name), phase="resource",
                                reason="resource_field_syntax")


def _is_resource_ref_shape(ref: Any) -> bool:
    try:
        _validate_ref_shape(ref, "")
        if "path" in ref:
            validate_business_path(ref["path"])
    except ProtocolError:
        return False
    return True


def _validate_ref(ref: Any, registry: ResourceKindRegistry | None, field: str) -> None:
    """registry 为 None 时只做形状与路径词法检查（不查登记），调用方必须如实声明。"""
    _validate_ref_shape(ref, field)
    if registry is not None:
        kind = registry.get(ref["kind"])
        if kind is None:
            raise ProtocolError("E_RESOURCE_KIND", field=_pointer(field, "kind"), phase="resource",
                                reason="resource_kind_not_registered")
        if kind.is_file and "path" not in ref:
            raise ProtocolError("E_RESOURCE_PATH_REQUIRED", field=_pointer(field, "path"),
                                phase="resource", reason="file_resource_requires_path")
    if "path" in ref:
        validate_business_path(ref["path"], field=_pointer(field, "path"))


def validate_resource_ref(ref: Any, registry: ResourceKindRegistry, *, field: str = "") -> None:
    """ResourceRef 结构与登记检查（4.2.1）：封闭字段、kind/id/sha256 语法、kind 已登记、文件型带 path。

    不读取资源、不核对字节；字节核验用 verify_resource_bytes（或 sealing.read_sealed）。
    """
    _validate_ref(ref, _require_registry(registry), field)


def verify_resource_bytes(ref: Any, raw: bytes, registry: ResourceKindRegistry) -> None:
    """在调用方已固定的同一份字节上核对完整 SHA-256（hmac.compare_digest）。"""
    validate_resource_ref(ref, registry)
    if type(raw) is not bytes:
        raise ProtocolError("E_RESOURCE_BYTES", phase="resource", reason="bytes_input_required")
    actual = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual, ref["sha256"]):
        raise ProtocolError("E_RESOURCE_HASH_MISMATCH", field="/sha256", phase="resource",
                            reason="digest_mismatch")


def _check_payload_representation(raw: bytes, allowed: frozenset[str]) -> None:
    """字节是否符合登记的载荷表示之一（R-P10）。

    binary：任何字节；json：strict_json_loads 通过（默认 1 MiB、64 层）；text 与 markdown：严格 UTF-8 解码成功
    （两者的字节检查相同，区别只是登记用的标签）。都不符合时：允许 json 就报严格解析的原错误码，否则报
    E_JSON_ENCODING（reason=payload_not_utf8_text）。
    """
    if "binary" in allowed:
        return
    json_error: ProtocolError | None = None
    if "json" in allowed:
        try:
            strict_json_loads(raw)
            return
        except ProtocolError as exc:
            json_error = exc
    if "text" in allowed or "markdown" in allowed:
        try:
            raw.decode("utf-8")
            return
        except UnicodeDecodeError:
            pass
    if json_error is not None:
        raise json_error
    raise ProtocolError("E_JSON_ENCODING", phase="resource", reason="payload_not_utf8_text")


def validate_resource_payload(ref: Any, raw: bytes, registry: ResourceKindRegistry) -> dict[str, Any]:
    """按资源类型登记的三项核验载荷（裁决 R-P10，文档 4.2.1）。

    顺序：先按 verify_resource_bytes 核验引用与字节（结构、kind 已登记、文件型带 path、raw 为 bytes、完整
    SHA-256）；再检查字节符合该类型的 payload_representations 之一（_check_payload_representation）；最后执行
    该类型的 payload_validator(raw, registry)。校验器抛出的 ProtocolError 原样传出；抛出其他异常时包装为
    E_SCHEMA_FIELDS（reason=payload_validator_unexpected_exception，phase=resource），不带原异常的文字，也不
    链接原异常（__cause__ 与 __context__ 都为 None），不回显输入。
    schema_ref 只登记不加载：非 None 时列入 semantic_checks_skipped（resource_schema）。
    返回 {"kind", "payload_validated", "semantic_checks", "semantic_checks_skipped"}；payload_validated 只有
    校验器实际运行过才是 True。通过不代表来源可信、已授权或已审核。
    """
    registry = _require_registry(registry)
    verify_resource_bytes(ref, raw, registry)
    kind = registry[ref["kind"]]
    _check_payload_representation(raw, kind.payload_representations)
    done = ["resource_ref_shape", "resource_kind_registered"]
    if "path" in ref:
        done.append("business_path_lexical")
    done += ["resource_bytes_digest", "payload_representation"]
    skipped = []
    if kind.schema_ref is not None:
        skipped.append("resource_schema")
    validated = False
    if kind.payload_validator is not None:
        unexpected = False
        try:
            kind.payload_validator(raw, registry)
        except ProtocolError:
            raise
        except Exception:  # noqa: BLE001 —— 包装为稳定错误码；在 except 块之外抛出，不链接原异常
            unexpected = True
        if unexpected:
            raise ProtocolError("E_SCHEMA_FIELDS", phase="resource",
                                reason="payload_validator_unexpected_exception")
        validated = True
        done.append("payload_validator")
    else:
        skipped.append("payload_validator")
    return {"kind": kind.name, "payload_validated": validated, "semantic_checks": done,
            "semantic_checks_skipped": skipped}


def check_resource_dag(edges: Mapping[str, Iterable[str]]) -> list[str]:
    """资源引用依赖图的确定性拓扑序（被引用者在前）；自环或多节点环 E_RESOURCE_CYCLE。

    edges[a] 是 a 引用的节点。错误的 field 是指向环上一个节点的 JSON Pointer（"/<节点>"）。
    """
    if not isinstance(edges, Mapping):
        raise ProtocolError("E_SCHEMA_TYPE", phase="resource", reason="edges_mapping_required")
    graph: dict[str, list[str]] = {}
    for node, targets in edges.items():
        if type(node) is not str or isinstance(targets, (str, bytes)):
            raise ProtocolError("E_SCHEMA_TYPE", phase="resource", reason="invalid_edge_entry")
        target_list = list(targets)
        if any(type(target) is not str for target in target_list):
            raise ProtocolError("E_SCHEMA_TYPE", phase="resource", reason="invalid_edge_target")
        graph[node] = sorted(set(target_list))
    for target_list in list(graph.values()):
        for target in target_list:
            graph.setdefault(target, [])
    order: list[str] = []
    state: dict[str, int] = {}  # 1 = 正在访问，2 = 已完成
    for start in sorted(graph):
        if start in state:
            continue
        state[start] = 1
        stack = [(start, iter(graph[start]))]
        while stack:
            node, children = stack[-1]
            child = next(children, None)
            if child is None:
                stack.pop()
                state[node] = 2
                order.append(node)
                continue
            status = state.get(child)
            if status == 1:
                raise ProtocolError("E_RESOURCE_CYCLE", field=_pointer("", child), phase="resource",
                                    reason="resource_dependency_cycle")
            if status is None:
                state[child] = 1
                stack.append((child, iter(graph[child])))
    return order


# ---------------------------------------------------------------------------
# Git OID（4.2.3）
# ---------------------------------------------------------------------------

_GIT_OID_RE = {"sha1": re.compile(r"[0-9a-f]{40}"), "sha256": re.compile(r"[0-9a-f]{64}")}
_ANY_GIT_OID_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_GIT_OBJECT_TYPES = ("commit", "tree")


def validate_git_oid(value: Any, object_format: str, *, field: str = "/oid") -> str:
    """完整小写 OID：sha1 为 40 位、sha256 为 64 位。其他格式 E_GIT_FORMAT_UNSUPPORTED，不按长度猜。"""
    if type(object_format) is not str or object_format not in _GIT_OID_RE:
        raise ProtocolError("E_GIT_FORMAT_UNSUPPORTED", phase="git", reason="object_format_unsupported")
    if type(value) is not str or _GIT_OID_RE[object_format].fullmatch(value) is None:
        raise ProtocolError("E_GIT_OID_SYNTAX", field=field, phase="git", reason="full_lowercase_oid_required")
    return value


def git_cat_file_type_argv(oid: str, object_format: str) -> list[str]:
    """受信 argv（不经 shell）：["git", "cat-file", "-t", oid]。本模块不执行它。"""
    validate_git_oid(oid, object_format)
    return ["git", "cat-file", "-t", oid]


def interpret_git_object_type(expected_type: str, returncode: int | None, stdout: bytes) -> None:
    """解释 `git cat-file -t` 的结果：非 0/None 退出码 → E_GIT_OBJECT_MISSING；类型不符 → E_GIT_OBJECT_TYPE。"""
    if type(expected_type) is not str or expected_type not in _GIT_OBJECT_TYPES:
        raise ProtocolError("E_COMPAT_UNSUPPORTED", phase="git", reason="expected_type_unsupported")
    if type(returncode) is not int or returncode != 0:
        raise ProtocolError("E_GIT_OBJECT_MISSING", phase="git", reason="object_lookup_failed")
    if type(stdout) is not bytes:
        raise ProtocolError("E_GIT_OBJECT_TYPE", phase="git", reason="unrecognized_output")
    text = stdout
    if text.endswith(b"\n"):
        text = text[:-1]
        if text.endswith(b"\r"):
            text = text[:-1]
    if text != expected_type.encode("ascii"):
        raise ProtocolError("E_GIT_OBJECT_TYPE", phase="git", reason="object_type_mismatch")


# ---------------------------------------------------------------------------
# 时间（4.3.1、附录 A.2“时间”）
# ---------------------------------------------------------------------------

_OBSERVED_AT_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})\.([0-9]{6})Z")


def format_observed_at(dt: _dt.datetime) -> str:
    """带时区的 datetime → UTC 的 YYYY-MM-DDTHH:MM:SS.ffffffZ；naive 时间 E_TIME_FORMAT。

    表示精度不等于时钟真实精度。
    """
    if not isinstance(dt, _dt.datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise ProtocolError("E_TIME_FORMAT", phase="time", reason="aware_datetime_required")
    try:
        utc = dt.astimezone(_dt.timezone.utc)
    except (OverflowError, ValueError):
        raise ProtocolError("E_TIME_FORMAT", phase="time", reason="utc_conversion_failed") from None
    return (f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T{utc.hour:02d}:{utc.minute:02d}:"
            f"{utc.second:02d}.{utc.microsecond:06d}Z")


def parse_observed_at(value: Any, *, field: str = "/observed_at") -> _dt.datetime:
    """严格解析固定 UTC 格式并检查日历有效（2026-02-30、24 时、60 秒均拒绝），返回带 UTC 时区的 datetime。"""
    if type(value) is not str:
        raise ProtocolError("E_TIME_FORMAT", field=field, phase="time", reason="string_required")
    match = _OBSERVED_AT_RE.fullmatch(value)
    if match is None:
        raise ProtocolError("E_TIME_FORMAT", field=field, phase="time", reason="fixed_utc_format_required")
    try:
        return _dt.datetime(*(int(part) for part in match.groups()), tzinfo=_dt.timezone.utc)
    except ValueError:
        raise ProtocolError("E_TIME_FORMAT", field=field, phase="time", reason="invalid_calendar_value") from None


# ---------------------------------------------------------------------------
# RunContext、派生与 Schema 路由（1.3、4.1.2–4.1.3、4.4.2）
# ---------------------------------------------------------------------------

_RUN_CONTEXT_FIELDS = ("schema_version", "record_type", "request_id", "task_id", "plan_version",
                       "run_id", "attempt", "role", "platform", "contract_ref")
_EXECUTOR_DISPATCH_FIELDS = ("task_id", "plan_version", "attempt", "contract_ref")


def _validate_run_context(ctx: Any, *, for_executor_dispatch: bool,
                          registry: ResourceKindRegistry | None) -> dict[str, Any]:
    _expect_dict(ctx, None)
    _check_record_json(ctx)
    _check_record_header(ctx, "run-context")
    _require_closed(ctx, _RUN_CONTEXT_FIELDS)
    validate_request_id(ctx["request_id"])
    if ctx["task_id"] is not None:
        validate_task_id(ctx["task_id"])
    validate_plan_version(ctx["plan_version"], allow_null=True)
    validate_run_id(ctx["run_id"])
    validate_attempt(ctx["attempt"])
    validate_role(ctx["role"])
    validate_platform(ctx["platform"])
    done = ["json_native_record", "identifier_syntax"]
    skipped = ["identifiers_allocated_by_trusted_core", "contract_ref_matches_trusted_record",
               "resource_bytes_digest"]
    if ctx["contract_ref"] is not None:
        _validate_ref(ctx["contract_ref"], registry, "/contract_ref")
        done += ["resource_ref_shape", "business_path_lexical"]
        (done if registry is not None else skipped).append("resource_kind_registered")
    if for_executor_dispatch:
        for name in _EXECUTOR_DISPATCH_FIELDS:
            if ctx[name] is None:
                raise ProtocolError("E_CONTEXT_INCOMPLETE", field=_pointer("", name), phase="context",
                                    reason="executor_dispatch_requires_field")
        # 4.1.3“进入 executor 派遣前”：planner/observer 上下文不能通过 executor 派遣检查（P4，与 C-04 一致）。
        if ctx["role"] != "executor":
            raise ProtocolError("E_SCHEMA_FIELDS", field="/role", phase="context",
                                reason="executor_dispatch_requires_executor_role")
        done.append("executor_dispatch_fields")
    else:
        skipped.append("executor_dispatch_fields")
    return _report("run-context", payload_validated=True, done=done, skipped=skipped)


def validate_run_context(ctx: Any, *, for_executor_dispatch: bool = False,
                         registry: ResourceKindRegistry | None = None) -> dict[str, Any]:
    """附录 A runContext 结构（字段封闭）。for_executor_dispatch=True 时 task_id/plan_version/attempt/
    contract_ref 必须非空（E_CONTEXT_INCOMPLETE），且 role 必须是 executor（E_SCHEMA_FIELDS，/role）。
    registry 给出时 contract_ref 的 kind 须已登记，否则只做形状检查并在返回值中声明。与 07 受信记录是否
    一致不在此检查。
    """
    if type(for_executor_dispatch) is not bool:
        raise TypeError("for_executor_dispatch must be bool")
    if registry is not None:
        _require_registry(registry)
    return _validate_run_context(ctx, for_executor_dispatch=for_executor_dispatch, registry=registry)


def _require_trusted_context(context: Any) -> dict[str, Any]:
    """校验调用方传入的受信 RunContext。

    context 本身不合法是**调用方**的错误，不是被校验记录的错误：错误码保持 validate_run_context 的
    原码，但 field 加前缀 "/context"（指向参数 context 内部，例如 "/context/platform"），phase 为
    "context"，从而与记录自身字段的错误区分开。
    """
    try:
        validate_run_context(context)
    except ProtocolError as exc:
        raise ProtocolError(exc.code, field="/context" + (exc.field or ""), phase="context",
                            reason=exc.reason,
                            hint="受信运行上下文参数本身不合法；这是调用方错误，不是被校验记录的字段错误。"
                            ) from None
    return context


_DERIVE_PARAMETERS = {
    "reconnect": frozenset(),
    "replay": frozenset(),
    "executor_retry": frozenset({"attempt"}),
    "observer_call": frozenset({"attempt", "role"}),
    "rework_new_contract": frozenset({"attempt", "new_task_id", "new_plan_version", "contract_ref",
                                      "role"}),
    "new_goal": frozenset({"attempt", "new_task_id", "new_plan_version", "contract_ref", "role"}),
}


def derive_run_context(parent: dict[str, Any], action: str, *, attempt: int | None = None,
                       new_task_id: str | None = None, new_plan_version: int | None = None,
                       contract_ref: dict[str, Any] | None = None,
                       role: str | None = None) -> dict[str, Any]:
    """按 4.1.2 表派生新的 RunContext（不修改 parent，返回新字典）。

    reconnect/replay：同 request、同 run、同 attempt（不产生新调用）。
    executor_retry（裁决 R-P2）：同 request、新 run；attempt **必须由调用方传入**，取受信分配器（旧实现
    scripts/pi_dispatch.py 的 allocate_attempt，将来由 07 负责）给出的编号，本函数不自行 +1。它必须
    ≥ parent.attempt：相等表示同一 prepared attempt 的重新运行（旧规则“预检失败后再次 run 沿用原编号”，
    01-D03）。未传 → E_CONTEXT_INCOMPLETE（/attempt）；小于 parent.attempt → E_CONTEXT_BINDING（/attempt）；
    parent.attempt 为 None → E_CONTEXT_INCOMPLETE；parent.role 不是 executor → E_SCHEMA_FIELDS（/role）。
    是否允许创建新尝试由 07 决定。
    observer_call：同 request、新 run、role=observer、attempt 取参数（可为 None）。
    rework_new_contract：同 request、新 run、plan_version（必填）/contract_ref/attempt 取参数。
    new_goal：新 request、新 run，task_id/plan_version/contract_ref/attempt 取参数。
    不适用于该动作的参数给了非 None 值 → E_SCHEMA_FIELDS；未知动作 → E_SCHEMA_FIELDS。
    """
    if type(action) is not str or action not in _DERIVE_PARAMETERS:
        raise ProtocolError("E_SCHEMA_FIELDS", field="/action", phase="context", reason="unknown_action")
    validate_run_context(parent)
    supplied = {name for name, value in (("attempt", attempt), ("new_task_id", new_task_id),
                                         ("new_plan_version", new_plan_version),
                                         ("contract_ref", contract_ref), ("role", role))
                if value is not None}
    if supplied - _DERIVE_PARAMETERS[action]:
        raise ProtocolError("E_SCHEMA_FIELDS", phase="context", reason="parameter_not_applicable")
    child = copy.deepcopy(parent)
    if action in ("reconnect", "replay"):
        return child
    child["run_id"] = new_run_id()
    if action == "executor_retry":
        # 4.1.2“原执行失败后再次启动 executor”：父上下文必须是 executor 运行（规格澄清 C-04）。
        if parent["role"] != "executor":
            raise ProtocolError("E_SCHEMA_FIELDS", field="/role", phase="context",
                                reason="executor_retry_requires_executor_parent")
        if parent["attempt"] is None:
            raise ProtocolError("E_CONTEXT_INCOMPLETE", field="/attempt", phase="context",
                                reason="retry_requires_attempt")
        # 裁决 R-P2：编号来自受信分配器，不在纯函数中自行 +1。
        if attempt is None:
            raise ProtocolError("E_CONTEXT_INCOMPLETE", field="/attempt", phase="context",
                                reason="executor_retry_requires_allocated_attempt")
        validate_attempt(attempt)
        if attempt < parent["attempt"]:
            raise ProtocolError("E_CONTEXT_BINDING", field="/attempt", phase="context",
                                reason="retry_attempt_below_parent")
        child["attempt"] = attempt
    elif action == "observer_call":
        if role is not None and role != "observer":
            raise ProtocolError("E_SCHEMA_FIELDS", field="/role", phase="context",
                                reason="observer_call_role_must_be_observer")
        child["role"] = "observer"
        child["attempt"] = attempt
    elif action == "rework_new_contract":
        if new_plan_version is None:
            raise ProtocolError("E_CONTEXT_INCOMPLETE", field="/plan_version", phase="context",
                                reason="rework_requires_plan_version")
        if new_task_id is not None:
            child["task_id"] = new_task_id
        child["plan_version"] = new_plan_version
        child["contract_ref"] = contract_ref  # 先校验原对象，通过后再拷贝（见下）
        child["attempt"] = attempt
        if role is not None:
            child["role"] = role
    else:  # new_goal
        child["request_id"] = new_request_id()
        child["task_id"] = new_task_id
        child["plan_version"] = new_plan_version
        child["contract_ref"] = contract_ref  # 先校验原对象，通过后再拷贝（见下）
        child["attempt"] = attempt
        if role is not None:
            child["role"] = role
    # 先校验、再拷贝（P4 第 3 轮，复审 json 视角）：调用方传入的 contract_ref 过深、含不可拷贝对象或 dict 子类
    # 时，与直接调用 validate_run_context 得到同一个码与 field（E_JSON_LIMIT / E_JSON_VALUE_TYPE），不漏出
    # RecursionError/TypeError，也不在校验前执行调用方对象的 __deepcopy__/__reduce_ex__。
    validate_run_context(child)
    if "contract_ref" in supplied:
        child["contract_ref"] = copy.deepcopy(contract_ref)  # 已确认是 JSON 原生值，拷贝不会失败
    return child


def require_schema_version(value: Any, supported: Iterable[int]) -> int:
    """参考实现行为：schema_version 必须是精确正整数（E_SCHEMA_VERSION_TYPE），且在 supported 中
    （E_SCHEMA_VERSION_UNSUPPORTED）。不检查 record_type；公共记录请用 route_public_record。"""
    if type(value) is not dict:
        raise ProtocolError("E_SCHEMA_TYPE", phase="route", reason="object_required")
    version = value.get("schema_version")
    if type(version) is not int or version < 1:
        raise ProtocolError("E_SCHEMA_VERSION_TYPE", field="/schema_version", phase="route",
                            reason="schema_version_not_positive_integer")
    if version not in set(supported):
        raise ProtocolError("E_SCHEMA_VERSION_UNSUPPORTED", field="/schema_version", phase="route",
                            reason="schema_version_unsupported")
    return version


def route_public_record(value: Any, *, expected_record_type: str) -> int:
    """按“受信入口声明的记录类型 + 家族 + 版本”路由（1.3、4.4.2）。绝不回退到 v1、不删除字段。"""
    if type(expected_record_type) is not str or expected_record_type not in PUBLIC_RECORD_TYPES:
        raise ProtocolError("E_COMPAT_UNSUPPORTED", phase="route", reason="record_type_unsupported")
    if type(value) is not dict:
        raise ProtocolError("E_SCHEMA_TYPE", phase="route", reason="object_required")
    return _check_record_header(value, expected_record_type)


_PUBLIC_RECORD_PARAMETERS = {
    "run-context": frozenset(),
    "run-event": frozenset({"context", "event_types", "segment_sizes"}),
    "usage-observation": frozenset({"context"}),
}


def validate_public_record(value: Any, *, expected_record_type: str, registry: ResourceKindRegistry,
                           context: dict[str, Any] | None = None,
                           event_types: EventTypeRegistry | None = None,
                           segment_sizes: Mapping[str, int] | None = None) -> dict[str, Any]:
    """先做整条记录的 JSON 原生值检查，再 route_public_record，再分派到对应记录校验；返回诚实的检查报告。

    检查顺序与直接调用 validate_run_context/validate_run_event/validate_usage_observation 相同（P4，
    复审 fidelity/json 视角）：value 是 dict 且 expected_record_type 是公共记录类型时，先对整条记录执行
    check_json_value(max_bytes=1 MiB)，所以记录头字段的整数越界、非有限数、孤立代理项、str/int 子类
    在两个入口得到同一个错误码（E_INTEGER_RANGE、E_JSON_NON_FINITE、E_JSON_UNICODE、E_JSON_VALUE_TYPE）。
    route_public_record 单独调用时的契约不变（它不做 JSON 原生值检查）。

    返回 {"record_type", "schema_version", "structural": True, "payload_validated",
    "semantic_checks": [已执行], "semantic_checks_skipped": [未执行]}。registry 会传给三类记录中的
    全部 ResourceRef 检查。

    不适用于该记录类型的参数给了非 None 值时报 E_SCHEMA_FIELDS（reason=parameter_not_applicable），
    不静默忽略：run-context 不接受 context、event_types、segment_sizes；usage-observation 不接受
    event_types、segment_sizes（与 derive_run_context 拒绝不适用参数的做法一致）。
    """
    if (type(value) is dict and type(expected_record_type) is str
            and expected_record_type in PUBLIC_RECORD_TYPES):
        _check_record_json(value)  # 与直接调用记录校验函数的顺序一致（C-07）
    route_public_record(value, expected_record_type=expected_record_type)
    _require_registry(registry)
    applicable = _PUBLIC_RECORD_PARAMETERS[expected_record_type]
    for name, given in (("context", context), ("event_types", event_types),
                        ("segment_sizes", segment_sizes)):
        if given is not None and name not in applicable:
            raise ProtocolError("E_SCHEMA_FIELDS", phase="route", reason="parameter_not_applicable",
                                hint=f"{expected_record_type} 不使用参数 {name}")
    if expected_record_type == "run-context":
        return validate_run_context(value, registry=registry)
    if expected_record_type == "run-event":
        return validate_run_event(value, registry=registry, context=context,
                                  event_types=event_types, segment_sizes=segment_sizes)
    return validate_usage_observation(value, context=context, registry=registry)


# ---------------------------------------------------------------------------
# 事件（4.3.1–4.3.5、附录 A.2）
# ---------------------------------------------------------------------------

class EventType(_FrozenRecord):
    """事件类型登记项（构造后不可变）。payload_validator(payload) 在载荷不合法时抛 ProtocolError。"""

    __slots__ = ("name", "allowed_sources", "payload_validator", "category")
    _fields = ("name", "allowed_sources", "payload_validator", "category")

    def __init__(self, name: str, allowed_sources: Iterable[str],
                 payload_validator: Callable[[dict[str, Any]], None] | None, category: str) -> None:
        if type(name) is not str or not 1 <= len(name) <= 96 or _has_surrogate(name):
            raise ValueError("EventType.name must be a str of 1..96 characters")
        if isinstance(allowed_sources, (str, bytes)):
            raise ValueError("EventType.allowed_sources must be a collection of source kinds")
        sources = frozenset(allowed_sources)
        if not sources or not sources <= set(EVENT_SOURCE_KINDS):
            raise ValueError("EventType.allowed_sources must be a non-empty subset of core/vendor")
        if payload_validator is not None and not callable(payload_validator):
            raise ValueError("EventType.payload_validator must be callable or None")
        if type(category) is not str or not category:
            raise ValueError("EventType.category must be a non-empty str")
        for field_name, value in zip(self._fields, (name, sources, payload_validator, category)):
            object.__setattr__(self, field_name, value)


class EventTypeRegistry(_FrozenRegistry):
    """受信事件类型注册表。01 只登记示例类型 run.prepared；真实 Pi 类型由 04 登记（01-D07）。"""
    __slots__ = ()
    _item_type = EventType


def _validate_run_prepared_payload(payload: Any) -> None:
    if type(payload) is not dict:
        raise ProtocolError("E_SCHEMA_TYPE", field="/payload", phase="event", reason="object_required")
    # 被 04 或其他调用方单独调用时同样按 1 MiB 字节下界止损（共享容器不会指数级遍历；P4）。
    check_json_value(payload, field="/payload", max_bytes=DEFAULT_MAX_BYTES)


def build_default_event_types() -> EventTypeRegistry:
    """只登记 01 的示例核心事件 run.prepared（载荷为对象、值为 JSON 原生值）。"""
    return EventTypeRegistry([
        EventType("run.prepared", frozenset({"core"}), _validate_run_prepared_payload, "core"),
    ])


def _validate_raw_span(span: Any, registry: ResourceKindRegistry,
                       segment_sizes: Mapping[str, int] | None, field: str) -> None:
    _expect_dict(span, field or None, phase="event")
    _require_closed(span, ("ref", "byte_start", "byte_end", "stream"), field=field)
    _enum(span["stream"], RAW_STREAMS, _pointer(field, "stream"), phase="event")
    _validate_ref(span["ref"], registry, _pointer(field, "ref"))
    # 裁决 R-P7：raw_refs 只能指向已封存的原始证据分段（raw-segment；分段语义归 04）。
    if span["ref"]["kind"] != "raw-segment":
        raise ProtocolError("E_RESOURCE_KIND", field=_pointer(_pointer(field, "ref"), "kind"), phase="event",
                            reason="raw_ref_requires_raw_segment")
    start, end = span["byte_start"], span["byte_end"]
    if type(start) is not int or not 0 <= start <= MAX_SAFE_INTEGER:
        raise ProtocolError("E_EVENT_RAW_RANGE", field=_pointer(field, "byte_start"), phase="event",
                            reason="invalid_byte_start")
    if type(end) is not int or not start < end <= MAX_SAFE_INTEGER:
        raise ProtocolError("E_EVENT_RAW_RANGE", field=_pointer(field, "byte_end"), phase="event",
                            reason="invalid_byte_end")
    if segment_sizes is not None:
        digest = span["ref"]["sha256"]
        if digest not in segment_sizes:
            raise ProtocolError("E_EVENT_RAW_REF_UNKNOWN", field=_pointer(_pointer(field, "ref"), "sha256"),
                                phase="event", reason="segment_version_unknown")
        size = segment_sizes[digest]
        # segment_sizes 来自受信封存索引；值不合法是调用方错误（只检查实际用到的条目，O(1)）。
        if type(size) is not int:
            raise TypeError("segment_sizes values must be exact int byte counts")
        if not 0 <= size <= MAX_SAFE_INTEGER:
            raise ValueError("segment_sizes values must be within 0..2**53-1")
        # 偏移按被引用版本的字节计：脱敏改变长度后，旧偏移不能套到新版本（01-F042）。
        if end > size:
            raise ProtocolError("E_EVENT_RAW_RANGE", field=_pointer(field, "byte_end"), phase="event",
                                reason="range_exceeds_segment")


def _require_segment_sizes(segment_sizes: Any) -> None:
    if segment_sizes is not None and not isinstance(segment_sizes, Mapping):
        raise TypeError("segment_sizes must be a Mapping[str, int] or None")


def validate_raw_span(span: Any, *, registry: ResourceKindRegistry,
                      segment_sizes: Mapping[str, int] | None = None) -> None:
    """原始证据半开区间 [byte_start, byte_end)（4.3.3）。

    segment_sizes：键为已封存分段版本的 sha256，值为该版本字节数（精确 int，0..2^53-1）；给出时引用
    必须在其中（E_EVENT_RAW_REF_UNKNOWN），且 byte_end 不超过该版本大小（E_EVENT_RAW_RANGE）。
    被用到的条目值不是精确 int 时抛 TypeError，越界时抛 ValueError（调用方错误）。
    ref 必须是已登记的 raw-segment（裁决 R-P7；其他 kind → E_RESOURCE_KIND，/ref/kind）。
    单独调用时先对整个区间对象做 JSON 原生值检查（与它嵌在事件中经 validate_run_event 校验时的顺序一致，
    例如 byte_end=2**60 在两处都是 E_INTEGER_RANGE；P4）。
    """
    _require_registry(registry)
    _require_segment_sizes(segment_sizes)
    if type(span) is dict:
        _check_record_json(span)
    _validate_raw_span(span, registry, segment_sizes, "")


_EVENT_FIELDS = ("schema_version", "record_type", "request_id", "run_id", "seq", "observed_at", "role",
                 "type", "source_kind", "payload", "raw_refs", "integrity", "adapter_revision")


def validate_run_event(event: Any, *, registry: ResourceKindRegistry,
                       context: dict[str, Any] | None = None,
                       event_types: EventTypeRegistry | None = None,
                       segment_sizes: Mapping[str, int] | None = None) -> dict[str, Any]:
    """附录 A event 封装 + 可选语义检查。

    整条记录先按 JSON 原生值检查（与 payload 共用 64 层上限）。role 只有核心事件可以为 null：vendor 事件
    role 为 null → E_SCHEMA_FIELDS（/role，reason=vendor_event_requires_role），不论是否给出 context。
    context（受信 RunContext）给出时 request_id/run_id 必须一致，事件 role 非空时必须等于
    context.role（E_CONTEXT_BINDING）；context 本身不合法时错误的 field 以 /context 开头、phase 为
    context（调用方错误）。event_types 给出时类型须已登记（E_EVENT_TYPE_UNKNOWN）、
    来源须被允许（E_EVENT_SOURCE）、载荷经该类型校验器检查；vendor 事件必须有 adapter_revision，
    声称 integrity=complete 时 raw_refs 必须非空（E_EVENT_EVIDENCE_MISSING）。
    seq 连续性不在单条记录上判断，见 assess_event_log/append_event/recover_next_seq。
    """
    _require_registry(registry)
    _require_segment_sizes(segment_sizes)
    if event_types is not None and not isinstance(event_types, EventTypeRegistry):
        raise TypeError("event_types must be an EventTypeRegistry or None")
    _expect_dict(event, None)
    _check_record_json(event)  # 整条记录（含 payload）共用 64 层上限，str 子类等非原生值也在此拒绝
    _check_record_header(event, "run-event")
    _require_closed(event, _EVENT_FIELDS)
    validate_request_id(event["request_id"])
    validate_run_id(event["run_id"])
    require_int(event["seq"], field="/seq", minimum=1)
    parse_observed_at(event["observed_at"])
    validate_role(event["role"], allow_null=True)
    _require_str(event["type"], "/type", max_len=96)
    _enum(event["source_kind"], EVENT_SOURCE_KINDS, "/source_kind")
    # 4.3.1：role 只有“无角色核心事件”可以为空。厂商事件把 role 置空就能脱离角色绑定（context 只在 role
    # 非空时核对），所以 vendor 事件的 role 必须非空，不论是否给出 context（P4 第 3 轮，复审 fidelity 与
    # events-usage 视角；JSON Schema 允许 null，这是 Schema 之外的领域检查）。
    if event["source_kind"] == "vendor" and event["role"] is None:
        raise ProtocolError("E_SCHEMA_FIELDS", field="/role", phase="event",
                            reason="vendor_event_requires_role")
    payload = event["payload"]
    if type(payload) is not dict:
        raise ProtocolError("E_SCHEMA_TYPE", field="/payload", phase="schema", reason="object_required")
    raw_refs = _expect_list(event["raw_refs"], "/raw_refs", max_items=256)
    for index, span in enumerate(raw_refs):
        _validate_raw_span(span, registry, segment_sizes, _pointer("/raw_refs", index))
    _enum(event["integrity"], EVENT_INTEGRITY_VALUES, "/integrity")
    _require_str(event["adapter_revision"], "/adapter_revision", max_len=128, allow_null=True)

    done = ["json_native_record", "identifier_syntax", "observed_at_calendar", "json_native_payload"]
    skipped = ["seq_continuity", "raw_ref_bytes_digest", "core_event_claims"]
    if raw_refs:
        done += ["raw_span_range", "resource_ref_shape", "resource_kind_registered",
                 "business_path_lexical"]
        (done if segment_sizes is not None else skipped).append("raw_segment_size")
    if context is not None:
        _require_trusted_context(context)
        if event["request_id"] != context["request_id"] or event["run_id"] != context["run_id"]:
            raise ProtocolError("E_CONTEXT_BINDING", field="/run_id" if event["request_id"] ==
                                context["request_id"] else "/request_id", phase="context",
                                reason="identifier_binding_mismatch")
        if event["role"] is not None and event["role"] != context["role"]:
            raise ProtocolError("E_CONTEXT_BINDING", field="/role", phase="context",
                                reason="role_binding_mismatch")
        done.append("context_binding")
    else:
        skipped.append("context_binding")
    payload_validated = False
    if event_types is not None:
        registered = event_types.get(event["type"])
        if registered is None:
            raise ProtocolError("E_EVENT_TYPE_UNKNOWN", field="/type", phase="event",
                                reason="event_type_not_registered")
        if event["source_kind"] not in registered.allowed_sources:
            raise ProtocolError("E_EVENT_SOURCE", field="/source_kind", phase="event",
                                reason="source_not_allowed_for_type")
        done += ["event_type_registered", "event_source_allowed"]
        if registered.payload_validator is not None:
            registered.payload_validator(payload)
            payload_validated = True
            done.append("payload_validator")
        else:
            skipped.append("payload_validator")
    else:
        skipped += ["event_type_registered", "event_source_allowed", "payload_validator"]
    if event["source_kind"] == "vendor":
        if event["adapter_revision"] is None:
            raise ProtocolError("E_EVENT_EVIDENCE_MISSING", field="/adapter_revision", phase="event",
                                reason="vendor_event_requires_adapter_revision")
        if event["integrity"] == "complete" and not raw_refs:
            raise ProtocolError("E_EVENT_EVIDENCE_MISSING", field="/raw_refs", phase="event",
                                reason="complete_vendor_event_requires_raw_refs")
        done.append("vendor_evidence_present")
    return _report("run-event", payload_validated=payload_validated, done=done, skipped=skipped)


def assess_stream_integrity(observed_kinds: Iterable[str], classification: Mapping[str, str],
                            required_kinds: Iterable[str]) -> dict[str, Any]:
    """给 04 的完整性判定接口（4.3.4、01-D07）。

    classification 值 ∈ known_required/known_optional/ignorable_optional；未登记的内容是 unknown，
    不能被忽略。必须出现的类型 = required_kinds ∪ classification 中标为 known_required 的类型
    （规格澄清 C-06：known_required 表示“已知且必须出现”；可能不出现的关键内容应登记为
    known_optional，它仍是已知内容），缺少其中任一类型即不完整。required_kinds 中的类型必须出现在
    classification 中，否则是调用方的配置错误 → E_SCHEMA_FIELDS（/required_kinds/<i>，
    reason=required_kind_not_classified；P4 第 3 轮）。01 不登记真实 Pi 类型。
    在本接口中 known_optional 与 ignorable_optional **判定结果完全相同**：接口只接收类型名，不接收“这条
    内容是否被理解/解析”，无法表达 4.3.4 中两者的区别（已知但未被理解的可选内容是否影响完整性）。
    这一区别由 04 在自己的解析层定义（01-D07）；两个取值在 01 中只是登记用的标签。
    """
    if not isinstance(classification, Mapping):
        raise ProtocolError("E_SCHEMA_TYPE", phase="event", reason="classification_mapping_required")
    for key, value in classification.items():
        if type(key) is not str:
            raise ProtocolError("E_SCHEMA_TYPE", phase="event", reason="classification_key_must_be_string")
        _enum(value, STREAM_CLASSIFICATIONS, _pointer("/classification", key), phase="event")

    def as_list(items: Any, name: str) -> list[str]:
        if isinstance(items, (str, bytes)):
            raise ProtocolError("E_SCHEMA_TYPE", phase="event", reason=f"{name}_iterable_required")
        result = list(items)
        if any(type(item) is not str for item in result):
            raise ProtocolError("E_SCHEMA_TYPE", phase="event", reason=f"{name}_must_be_strings")
        return result

    observed = as_list(observed_kinds, "observed_kinds")
    required = as_list(required_kinds, "required_kinds")
    # 必需类型必须已登记（P4 第 3 轮，复审 events-usage 视角）：未登记的必需类型出现时算 unknown、缺失时算
    # missing，流永远不会 complete；这是调用方（04）的配置错误，明确报出，不静默表现为“流永远不完整”。
    for index, kind in enumerate(required):
        if kind not in classification:
            raise ProtocolError("E_SCHEMA_FIELDS", field=_pointer("/required_kinds", index), phase="event",
                                reason="required_kind_not_classified")
    unknown = sorted({kind for kind in observed if kind not in classification})
    seen = set(observed)
    must_appear = set(required) | {kind for kind, value in classification.items()
                                   if value == "known_required"}
    missing = sorted(kind for kind in must_appear if kind not in seen)
    return {"integrity": "complete" if not unknown and not missing else "incomplete",
            "unknown": unknown, "missing_required": missing}


_ABSENT = object()


def _binding_fields_readable(record: dict[str, Any]) -> bool:
    """M-01：request_id 必须是 str；role 字段必须存在且为 str 或 None（无角色核心事件）。"""
    request = record.get("request_id", _ABSENT)
    role = record.get("role", _ABSENT)
    return type(request) is str and (role is None or type(role) is str)


def _assess_event_log(records: Sequence[dict[str, Any]], run_id: str | None) -> tuple[dict[str, Any], set[str]]:
    """assess_event_log 的实现；另返回绑定错误的类别集合（"run"、"request"、"role"），供恢复函数选择 reason。"""
    kinds: set[str] = set()
    result: dict[str, Any] = {"events": [], "event_count": 0, "duplicates": [], "conflicts": [],
                              "gaps": [], "out_of_order": [], "binding_errors": [],
                              "record_type_errors": [], "invalid": [],
                              "last_contiguous_seq": 0, "complete": False}
    try:
        items = list(records)
    except Exception:  # noqa: BLE001 —— 纯函数约定：输入不可迭代也只报告，不抛异常
        result["invalid"].append(None)
        return result, kinds
    canonical: dict[int, tuple[dict[str, Any], bytes]] = {}
    conflicts: set[int] = set()
    highest = 0
    reference = run_id
    # 参照值给出但不是合法 run id（非 str，或 str 但不符合 run- 加 32 位小写十六进制）：每条可读记录都记为
    # binding_errors，不因记录恰好带同一个非法字符串而判为一致（P4 第 3 轮，复审 fidelity 视角）。
    reference_invalid = run_id is not None and (type(run_id) is not str
                                                or _RUN_ID_RE.fullmatch(run_id) is None)
    # M-01（收尾轮）：同一 run 内 request_id 一致、非空 role 一致。参照值取第一条通过 run 绑定的记录的
    # request_id，以及第一条通过 run 与 request 绑定、role 非空的记录的 role。
    reference_request: str | None = None
    reference_role: str | None = None
    for index, record in enumerate(items):
        try:
            seq = record["seq"]
            record_run = record["run_id"]
            if (type(record) is not dict or type(seq) is not int or not 1 <= seq <= MAX_SAFE_INTEGER
                    or type(record_run) is not str):
                raise ValueError
        except Exception:  # noqa: BLE001
            result["invalid"].append(index)
            continue
        declared = record.get("record_type")
        if type(declared) is not str or declared != "run-event":  # 与 append_event 的判断相同
            result["record_type_errors"].append(index)
            continue
        if not _binding_fields_readable(record):  # M-01：绑定字段不可读，与 seq/run_id 不可读同样处理
            result["invalid"].append(index)
            continue
        try:
            encoded = stable_snapshot_bytes(record)
        except Exception:  # noqa: BLE001
            result["invalid"].append(index)
            continue
        if reference is None:
            reference = record_run
        if reference_invalid or record_run != reference:
            result["binding_errors"].append(seq)
            kinds.add("run")
            continue
        record_request, record_role = record["request_id"], record["role"]
        if reference_request is None:
            reference_request = record_request
        if record_request != reference_request:
            result["binding_errors"].append(seq)
            kinds.add("request")
            continue
        if record_role is not None:
            if reference_role is None:
                reference_role = record_role
            elif record_role != reference_role:
                result["binding_errors"].append(seq)
                kinds.add("role")
                continue
        if seq in canonical:
            if canonical[seq][1] == encoded:
                result["duplicates"].append(seq)
            else:
                conflicts.add(seq)
            continue
        canonical[seq] = (record, encoded)
        if seq < highest:
            result["out_of_order"].append(seq)
        highest = max(highest, seq)
    expected = 1
    for seq in sorted(canonical):
        if seq != expected:
            result["gaps"].append((expected, seq))
        expected = seq + 1
    contiguous = 0
    while contiguous + 1 in canonical and contiguous + 1 not in conflicts:
        contiguous += 1
    result["conflicts"] = sorted(conflicts)
    result["events"] = [copy.deepcopy(canonical[seq][0]) for seq in sorted(canonical)
                        if seq not in conflicts]
    result["event_count"] = len(result["events"])
    result["last_contiguous_seq"] = contiguous
    result["complete"] = not (result["conflicts"] or result["gaps"] or result["out_of_order"]
                              or result["binding_errors"] or result["record_type_errors"]
                              or result["invalid"])
    return result, kinds


def assess_event_log(records: Sequence[dict[str, Any]], *,
                     run_id: str | None = None) -> dict[str, Any]:
    """评估按持久化顺序读出的已封存事件（纯函数，不抛异常）。

    同一 seq 且固定快照字节相同 → duplicates；同一 seq 内容不同 → conflicts（该 seq 被隔离，不进入
    events）；seq 不连续 → gaps [(expected, got)]；按持久化顺序，新 seq 出现在更大的 seq 之后
    （例如 [1, 3, 2]：单写入方本应拒绝在 2 之前写入 3）→ out_of_order（按 seq 列出）；绑定不一致 →
    binding_errors（按 seq 列出，不参与其他判断）；不能读取 seq/run_id、绑定字段不可读（request_id 不是
    str，或 role 缺失、不是 str 也不是 null；M-01）或不能形成快照的记录 → invalid（按下标列出）；seq 与
    run_id 可读、但 record_type 不是精确的 "run-event"（例如带 seq 的 usage-observation）→ record_type_errors
    （按下标列出，不进入 events，与 append_event 的 E_SCHEMA_RECORD_TYPE 一致；P4）。

    绑定（binding_errors）按以下顺序核对：
    1. run_id 与参照值不一致。参照值：参数 run_id 不为 None 时就用它；它不是合法 run id（非 str，或不符合
       run- 加 32 位小写十六进制，例如 123、"garbage"）时每条可读记录都记为 binding_errors，不静默回退，也
       不因记录带同一个非法字符串而判为一致（P4 第 3 轮）；参数为 None 时取第一条可读记录的 run_id（不校验
       其语法，逐条结构校验见 validate_run_event）。
    2. request_id 与同一 run 内的参照值不一致（M-01，收尾轮）：参照值取第一条通过第 1 步的记录的 request_id。
    3. 非空 role 与同一 run 内的参照值不一致（M-01，收尾轮）：参照值取第一条通过前两步、role 非空的记录的
       role；role 为 null 的无角色核心事件（4.3.1）不参与比较。
    第 2、3 步只核对日志内部是否一致，不核对受信 RunContext；与受信上下文的 request/role 绑定用
    validate_run_event(context=...) 逐条核对。
    event_count（裁决 R-P8）：events 中的事件数（去重后、不含冲突的 seq）。

    complete 只表示“序号、绑定（同一 run 内 run_id、request_id 一致，非空 role 一致）与记录类型一致”：没有
    冲突、缺口、乱序、绑定错误、记录类型错误与不可读记录。它**不表示协议完整**，也不证明运行产生过事件：
    空日志（event_count=0）与尾部截断的日志也可以是 complete=True（裁决 R-P8 保留这一含义）；协议完整性由
    assess_stream_integrity 与 04 的结束证据判断（build_run_dimensions 的 protocol_complete）。只做序号/绑定/
    重复判定，逐条结构校验请先用 validate_run_event。
    """
    return _assess_event_log(records, run_id)[0]


def _committed_bindings(committed: dict[int, bytes], last: int) -> tuple[str | None, str | None]:
    """M-01：从已提交的快照字节取同一 run 的 request_id（seq 1）与第一个非空 role。

    committed 的值必须是本函数族写入的 run-event 快照字节；解析失败或绑定字段不可读说明 state 不满足
    不变式，抛 ValueError（不是 ProtocolError），与 append_event 对其他不变式的处理相同。
    """
    request: str | None = None
    role: str | None = None
    for seq in range(1, last + 1):
        try:
            record = json.loads(committed[seq].decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            record = None
        if type(record) is not dict or not _binding_fields_readable(record):
            raise ValueError("state.committed must hold snapshot bytes of run-event records; "
                             "rebuild it with recover_append_state()")
        if request is None:
            request = record["request_id"]
        if record["role"] is not None:
            role = record["role"]
            break
    return request, role


def append_event(state: dict[str, Any], record: dict[str, Any]) -> str:
    """单写入方追加（4.3.2）。state = {"run_id", "last_committed_seq": int, "committed": {seq: bytes}}。

    首条必须 seq=1；新事件必须是 last_committed_seq+1 → "appended"（**就地更新调用方传入的 state**）；
    已存在 seq 且字节相同 → "duplicate"（不改 state、不再次计费）；已存在 seq 内容不同 →
    E_EVENT_REPLAY_CONFLICT；跳号 → E_EVENT_GAP；run_id 不同 → E_CONTEXT_BINDING；record_type 不是
    run-event → E_SCHEMA_RECORD_TYPE。任何错误都不修改 state。
    M-01（收尾轮）：request_id 必须是 str（否则 E_SCHEMA_TYPE，/request_id），role 字段必须存在且为 str 或
    null（否则 E_SCHEMA_TYPE，/role）；已有提交时，request_id 必须等于已提交记录的 request_id
    （E_CONTEXT_BINDING，/request_id，reason=request_binding_mismatch），非空 role 必须等于已提交记录中第一个
    非空 role（E_CONTEXT_BINDING，/role，reason=role_binding_mismatch）。参照值从 committed 中的快照字节读出，
    state 的形状不变。检查顺序：record_type → run_id → 绑定字段可读 → request_id → role → seq。

    state 的不变式：committed 的键恰好是 1..last_committed_seq 的精确 int（不接受 bool、字符串等），值为
    对应记录的固定快照字节（bytes）。每次调用都完整核对（O(已提交条数)；本函数是内存参考模型，事务化
    实现归 07/04）。崩溃后必须用 recover_append_state(records, run_id=...) 从持久记录重建完整 state；只恢复
    last_committed_seq、从 JSON 读回（键变成字符串，包括追加下一个 seq 的情形）或键稀疏的 state 不满足
    不变式，会被拒绝（ValueError），而不是把重放误报成 E_EVENT_GAP，也不会得到混合键的 state。本函数只
    维护内存结构；持久提交与序号分配的事务协调由调用方（07/04）负责。
    """
    if (type(state) is not dict or type(state.get("run_id")) is not str
            or type(state.get("last_committed_seq")) is not int or state["last_committed_seq"] < 0
            or type(state.get("committed")) is not dict):
        raise ValueError("state must be {'run_id': str, 'last_committed_seq': int>=0, 'committed': dict}")
    committed = state["committed"]
    last = state["last_committed_seq"]
    # 键互不相同、都是 1..last 的精确 int、且恰有 last 个 → 键集合恰为 1..last（鸽巢原理）。
    if (len(committed) != last
            or not all(type(key) is int and 1 <= key <= last for key in committed)
            or not all(type(value) is bytes for value in committed.values())):
        raise ValueError("state.committed must map exactly the int seqs 1..last_committed_seq to snapshot "
                         "bytes; rebuild it with recover_append_state()")
    _expect_dict(record, None, phase="event")
    declared = record.get("record_type")
    if type(declared) is not str or declared != "run-event":
        raise ProtocolError("E_SCHEMA_RECORD_TYPE", field="/record_type", phase="event",
                            reason="record_type_mismatch")
    if record.get("run_id") != state["run_id"]:
        raise ProtocolError("E_CONTEXT_BINDING", field="/run_id", phase="event",
                            reason="run_binding_mismatch")
    if type(record.get("request_id")) is not str:
        raise ProtocolError("E_SCHEMA_TYPE", field="/request_id", phase="event", reason="string_required")
    if "role" not in record or not (record["role"] is None or type(record["role"]) is str):
        raise ProtocolError("E_SCHEMA_TYPE", field="/role", phase="event", reason="string_or_null_required")
    if last:
        reference_request, reference_role = _committed_bindings(committed, last)
        if record["request_id"] != reference_request:
            raise ProtocolError("E_CONTEXT_BINDING", field="/request_id", phase="event",
                                reason="request_binding_mismatch")
        if record["role"] is not None and reference_role is not None and record["role"] != reference_role:
            raise ProtocolError("E_CONTEXT_BINDING", field="/role", phase="event",
                                reason="role_binding_mismatch")
    seq = require_int(record.get("seq"), field="/seq", minimum=1)
    encoded = stable_snapshot_bytes(record)
    if seq in committed:
        if committed[seq] == encoded:
            return "duplicate"
        raise ProtocolError("E_EVENT_REPLAY_CONFLICT", field="/seq", phase="event",
                            reason="same_seq_different_content")
    if seq <= last:
        raise ValueError("state.committed is missing a committed seq; "
                         "rebuild it with recover_append_state()")
    if seq != last + 1:
        raise ProtocolError("E_EVENT_GAP", field="/seq", phase="event", reason="seq_not_next")
    committed[seq] = encoded
    state["last_committed_seq"] = seq
    return "appended"


def _assess_for_recovery(records: Sequence[dict[str, Any]], run_id: str | None) -> dict[str, Any]:
    if run_id is not None:
        validate_run_id(run_id)
    assessed, binding_kinds = _assess_event_log(records, run_id)
    # 先报“记录本身”的缺陷，再报序号问题（P4，复审 events-usage 视角）：他 run 记录、非事件记录或不可读
    # 记录位于日志中间时同时造成缺口；按原因报码，错误码才不随故障记录的位置变化（7.4）。
    # 绑定错误的 reason 按类别的固定顺序选择：run → request → role（M-01，收尾轮）。
    if "run" in binding_kinds:
        raise ProtocolError("E_CONTEXT_BINDING", phase="event", reason="log_contains_foreign_run")
    if "request" in binding_kinds:
        raise ProtocolError("E_CONTEXT_BINDING", phase="event", reason="log_request_binding_mismatch")
    if "role" in binding_kinds:
        raise ProtocolError("E_CONTEXT_BINDING", phase="event", reason="log_role_binding_mismatch")
    if assessed["record_type_errors"]:
        raise ProtocolError("E_SCHEMA_RECORD_TYPE", phase="event", reason="log_contains_non_event_record")
    if assessed["invalid"]:
        raise ProtocolError("E_SCHEMA_TYPE", phase="event", reason="log_contains_unreadable_record")
    if assessed["conflicts"]:
        raise ProtocolError("E_EVENT_REPLAY_CONFLICT", phase="event", reason="log_contains_conflict")
    if assessed["gaps"]:
        raise ProtocolError("E_EVENT_GAP", phase="event", reason="log_contains_gap")
    if assessed["out_of_order"]:
        raise ProtocolError("E_EVENT_GAP", phase="event", reason="log_out_of_order")
    return assessed


def recover_next_seq(records: Sequence[dict[str, Any]], *, run_id: str | None = None) -> int:
    """从持久记录（不是内存计数）恢复下一个 seq；空日志返回 1。

    按以下顺序报错（先报记录本身的缺陷，再报序号问题，错误码不随故障记录的位置变化）：绑定不一致
    E_CONTEXT_BINDING（run_id 不一致 reason=log_contains_foreign_run；同一 run 内 request_id 不一致
    reason=log_request_binding_mismatch；非空 role 不一致 reason=log_role_binding_mismatch；三者并存时按这一
    顺序取 reason，M-01）；有非 run-event 记录 E_SCHEMA_RECORD_TYPE；有无法读取的记录（含绑定字段不可读）
    E_SCHEMA_TYPE；有冲突 E_EVENT_REPLAY_CONFLICT；有缺口（reason=log_contains_gap）或乱序
    （reason=log_out_of_order）E_EVENT_GAP。
    这一全序对同时含多类缺陷的日志同样成立（按类别判定，与记录位置无关），是第 3.2 节“多缺陷输入不承诺
    首错”的明确例外（P4 第 3 轮，测试固定）。run_id（建议总是给出）：日志必须属于该 run，否则
    E_CONTEXT_BINDING（误读了另一个 run 的整份日志时不会静默返回序号）；不是合法 run id 时
    E_ID_SYNTAX。不给时只以第一条记录为参照。request_id 与 role 只核对日志内部是否一致（见 assess_event_log）。
    """
    return _assess_for_recovery(records, run_id)["last_contiguous_seq"] + 1


def recover_append_state(records: Sequence[dict[str, Any]], *, run_id: str) -> dict[str, Any]:
    """崩溃后从持久记录重建 append_event 所需的完整 state（4.3.2）。

    先按 recover_next_seq 的规则核验日志（绑定、记录类型、不可读记录、冲突、缺口、乱序都拒绝），再返回
    {"run_id", "last_committed_seq", "committed": {seq: 固定快照字节}}。这样重放已提交的相同事件得到
    "duplicate"，内容不同的重放得到 E_EVENT_REPLAY_CONFLICT。
    """
    if run_id is None:
        raise ProtocolError("E_ID_SYNTAX", field="/run_id", phase="identity",
                            reason="identifier_syntax")
    assessed = _assess_for_recovery(records, run_id)
    committed = {event["seq"]: stable_snapshot_bytes(event) for event in assessed["events"]}
    return {"run_id": run_id, "last_committed_seq": assessed["last_contiguous_seq"],
            "committed": committed}


def build_run_dimensions(*, process_exited: bool | None = None, exit_code: int | None = None,
                         cancelled_by: str | None = None, end_evidence: str | None = None,
                         protocol_integrity: str | None = None, usage_complete: bool | None = None,
                         wait_returncode: int | None = None, legacy_attempt_state: Any = None,
                         legacy_verdict: str | None = None, approval_ref: Any = None,
                         integration_observation: Any = None,
                         cancellation_checked: bool | None = None) -> dict[str, Any]:
    """分维度状态视图（01-A05，4.1.4 与 4.3.4 表）。各维度分开表达，未知保持 None；
    不提供 success/ok/passed/overall 之类聚合键。

    cancelled 是三态（P4，4.1.4“某维度未知时该维度保持未知”）：cancelled_by 不是 None → True（空串同样算
    给出了取消来源）；cancelled_by 是 None 且 cancellation_checked=True（调用方已核对取消来源，
    确认没有取消）→ False；其余 → None（未知）。cancellation_checked 必须是 bool 或 None。
    approval_present 同样按“是否为 None”判断，approval_ref 为 {}、"" 也算存在。

    protocol_complete：integrity 为 incomplete/pending → False；进程已退出（exit_code 非 None 或
    process_exited 为 True）但没有结束证据 → False（包括“退出 0 但缺结束证据”）；integrity 为
    complete 且有结束证据 → True；其余 → None（未知）。
    protocol_conflict：01 只认识结束证据 "success"：end_evidence == "success" 且 exit_code 非 0（含
    POSIX 被信号终止的负数）→ True；"success" 且 exit_code == 0 → False；exit_code 未知，或结束证据是
    其他任何值（例如 "failure"，其含义由 04 定义）→ None（未知），不断言“没有冲突”。
    process_exited=False 却给出 exit_code 是自相矛盾的输入 → E_SCHEMA_FIELDS。
    protocol_observation 原样保留 end_evidence 与 protocol_integrity：发生冲突时退出事实与结束证据
    两方都在视图中（01-F041），不只保留其中一方。
    approval_present 之外，contract_approval_ref 原样保留审核引用（4.1.4 建议的视图字段），以便追溯。
    """
    if process_exited is not None and type(process_exited) is not bool:
        raise ProtocolError("E_SCHEMA_TYPE", field="/process_exited", phase="dimensions",
                            reason="boolean_required")
    for name, value in (("exit_code", exit_code), ("wait_returncode", wait_returncode)):
        if value is not None and type(value) is not int:
            raise ProtocolError("E_SCHEMA_TYPE", field="/" + name, phase="dimensions",
                                reason="exact_integer_required")
    for name, value in (("cancelled_by", cancelled_by), ("end_evidence", end_evidence)):
        if value is not None and type(value) is not str:
            raise ProtocolError("E_SCHEMA_TYPE", field="/" + name, phase="dimensions",
                                reason="string_required")
    for name, value in (("usage_complete", usage_complete),
                        ("cancellation_checked", cancellation_checked)):
        if value is not None and type(value) is not bool:
            raise ProtocolError("E_SCHEMA_TYPE", field="/" + name, phase="dimensions",
                                reason="boolean_required")
    _enum(protocol_integrity, EVENT_INTEGRITY_VALUES, "/protocol_integrity", allow_null=True,
          phase="dimensions")
    _enum(legacy_verdict, LEGACY_VERDICTS, "/legacy_verdict", allow_null=True, phase="dimensions")
    if process_exited is False and exit_code is not None:
        raise ProtocolError("E_SCHEMA_FIELDS", field="/exit_code", phase="dimensions",
                            reason="exit_code_given_for_process_not_exited")

    exited = process_exited is True or exit_code is not None
    if protocol_integrity in ("incomplete", "pending"):
        protocol_complete: bool | None = False
    elif end_evidence is None and exited:
        protocol_complete = False
    elif protocol_integrity == "complete" and end_evidence is not None:
        protocol_complete = True
    else:
        protocol_complete = None
    if end_evidence == "success" and exit_code is not None:
        protocol_conflict: bool | None = exit_code != 0
    else:
        protocol_conflict = None
    if cancelled_by is not None:
        cancelled: bool | None = True
    elif cancellation_checked is True:
        cancelled = False
    else:
        cancelled = None
    return {
        "rule_revision": DIMENSIONS_RULE_REVISION,
        "process_observation": {"exited": process_exited, "exit_code": exit_code,
                                "cancelled_by": cancelled_by},
        # 01-F041“保留双方证据”：结束证据与完整性原样保留，不因与退出码冲突而丢弃（规格澄清 C-05）。
        "protocol_observation": {"end_evidence": end_evidence, "integrity": protocol_integrity},
        "wait_observation": {"returncode": wait_returncode, "note": "wait 返回值只说明查询动作"},
        "protocol_complete": protocol_complete,
        "protocol_conflict": protocol_conflict,
        "cancelled": cancelled,
        "usage_complete": usage_complete,
        "legacy_attempt_state": copy.deepcopy(legacy_attempt_state),
        "legacy_verdict": legacy_verdict,
        "approval_present": approval_ref is not None,
        "contract_approval_ref": copy.deepcopy(approval_ref),
        "approval_note": "存在审核引用不证明当前字节仍与审核输入匹配，也不证明审核仍有效",
        "integration_observation": copy.deepcopy(integration_observation),
    }


# ---------------------------------------------------------------------------
# 用量观测（4.3.7–4.3.9；01-D08：01 只做观测，07 唯一记账）
# ---------------------------------------------------------------------------

_USAGE_FIELDS = ("schema_version", "record_type", "usage_id", "request_id", "run_id", "role",
                 "provider", "model", "observation_kind", "input_tokens", "cache_read_tokens",
                 "cache_write_tokens", "output_tokens", "reasoning_tokens", "input_includes_cache",
                 "output_includes_reasoning", "reported_cost", "estimated_cost", "billing_source",
                 "billing_event_id", "supersedes_usage_id", "unknown_reasons", "evidence_refs")
_AMOUNT_RE = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]+)?")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_COST_FIELDS = ("reported_cost", "estimated_cost")
_DECIMAL_CONTEXT = decimal.Context(prec=1000, rounding=decimal.ROUND_HALF_EVEN,
                                   traps=[decimal.InvalidOperation, decimal.Inexact,
                                          decimal.Rounded, decimal.Overflow])


def _validate_money(value: Any, field: str) -> None:
    def reject(reason: str) -> ProtocolError:
        return ProtocolError("E_USAGE_VALUE", field=field, phase="usage", reason=reason)

    if type(value) is not dict or set(value) != {"amount", "currency"}:
        raise reject("money_object_required")
    amount, currency = value["amount"], value["currency"]
    if type(amount) is not str or len(amount) > 64 or _AMOUNT_RE.fullmatch(amount) is None:
        raise reject("amount_not_plain_nonnegative_decimal")
    if type(currency) is not str or _CURRENCY_RE.fullmatch(currency) is None:
        raise reject("currency_code_invalid")


def _usage_field_unknown(obs: dict[str, Any], name: str) -> bool:
    if name == "observation_kind":
        return obs["observation_kind"] == "unknown"
    return obs[name] is None


def _validate_usage(obs: Any, context: dict[str, Any] | None,
                    registry: ResourceKindRegistry | None, field: str) -> dict[str, Any]:
    _expect_dict(obs, field or None)
    _check_record_json(obs, field)
    _check_record_header(obs, "usage-observation", field)
    _require_closed(obs, _USAGE_FIELDS, field=field)

    def ptr(name: str) -> str:
        return _pointer(field, name)

    validate_logical_id(obs["usage_id"], field=ptr("usage_id"))
    validate_request_id(obs["request_id"], field=ptr("request_id"))
    validate_run_id(obs["run_id"], field=ptr("run_id"))
    validate_role(obs["role"], field=ptr("role"))
    _require_str(obs["provider"], ptr("provider"), max_len=128, allow_null=True)
    _require_str(obs["model"], ptr("model"), max_len=256, allow_null=True)
    _enum(obs["observation_kind"], OBSERVATION_KINDS, ptr("observation_kind"))
    for name in USAGE_COUNTERS:
        if obs[name] is not None:
            require_int(obs[name], field=ptr(name), minimum=0)
    _require_bool(obs["input_includes_cache"], ptr("input_includes_cache"), allow_null=True)
    _require_bool(obs["output_includes_reasoning"], ptr("output_includes_reasoning"), allow_null=True)
    for name in _COST_FIELDS:
        if obs[name] is not None:
            _validate_money(obs[name], ptr(name))
    _enum(obs["billing_source"], BILLING_SOURCES, ptr("billing_source"))
    # 估算不能冒充实际费用（4.3.7）：只有厂商报告或账单来源才能带 reported_cost。
    if obs["reported_cost"] is not None and obs["billing_source"] not in REPORTED_BILLING_SOURCES:
        raise ProtocolError("E_USAGE_VALUE", field=ptr("reported_cost"), phase="usage",
                            reason="reported_cost_requires_vendor_or_invoice_source")
    _require_str(obs["billing_event_id"], ptr("billing_event_id"), max_len=256, allow_null=True)
    if obs["supersedes_usage_id"] is not None:
        validate_logical_id(obs["supersedes_usage_id"], field=ptr("supersedes_usage_id"))
        if obs["supersedes_usage_id"] == obs["usage_id"]:
            raise ProtocolError("E_USAGE_CONFLICT", field=ptr("supersedes_usage_id"), phase="usage",
                                reason="observation_supersedes_itself")
    reasons = obs["unknown_reasons"]
    if type(reasons) is not dict:
        raise ProtocolError("E_SCHEMA_TYPE", field=ptr("unknown_reasons"), phase="schema",
                            reason="object_required")
    for key, text in reasons.items():
        if type(key) is not str or key not in UNKNOWABLE_USAGE_FIELDS:
            raise ProtocolError("E_USAGE_UNKNOWN_REASON", field=ptr("unknown_reasons"), phase="usage",
                                reason="reason_key_not_allowed")
        key_ptr = _pointer(ptr("unknown_reasons"), key)
        if type(text) is not str or not 1 <= len(text) <= 512 or _has_surrogate(text):
            raise ProtocolError("E_USAGE_UNKNOWN_REASON", field=key_ptr, phase="usage",
                                reason="reason_text_invalid")
        if not _usage_field_unknown(obs, key):
            raise ProtocolError("E_USAGE_UNKNOWN_REASON", field=key_ptr, phase="usage",
                                reason="reason_given_for_known_field")
    for name in UNKNOWABLE_USAGE_FIELDS:
        if _usage_field_unknown(obs, name) and name not in reasons:
            raise ProtocolError("E_USAGE_UNKNOWN_REASON", field=ptr(name), phase="usage",
                                reason="unknown_field_requires_reason")
    evidence = _expect_list(obs["evidence_refs"], ptr("evidence_refs"), max_items=256)
    for index, ref in enumerate(evidence):
        _validate_ref(ref, registry, _pointer(ptr("evidence_refs"), index))
    done = ["json_native_record", "identifier_syntax", "usage_money_format",
            "usage_billing_source_consistency", "usage_unknown_reasons", "usage_supersedes_self"]
    skipped = ["usage_scope_dedup_and_revision", "usage_accounting", "evidence_bytes_digest",
               "identifiers_allocated_by_trusted_core"]
    if evidence:
        done += ["resource_ref_shape", "business_path_lexical"]
        (done if registry is not None else skipped).append("resource_kind_registered")
    if context is not None:
        _require_trusted_context(context)
        for name in ("request_id", "run_id", "role"):
            if obs[name] != context[name]:
                raise ProtocolError("E_CONTEXT_BINDING", field=ptr(name), phase="context",
                                    reason="usage_binding_mismatch")
        done.append("context_binding")
    else:
        skipped.append("context_binding")
    return _report("usage-observation", payload_validated=True, done=done, skipped=skipped)


def validate_usage_observation(obs: Any, *, context: dict[str, Any] | None = None,
                               registry: ResourceKindRegistry | None = None) -> dict[str, Any]:
    """附录 A usage 结构（字段封闭）+ 单条语义检查（4.3.7、4.3.9）。

    计量为精确整数 0..2^53-1 或 None；金额为不带符号/千分位/指数/空格的十进制字符串，币种三位
    大写字母（E_USAGE_VALUE）；supersedes_usage_id 不能等于自身（E_USAGE_CONFLICT）；
    unknown_reasons 的键只能是 UNKNOWABLE_USAGE_FIELDS 中当前为 null（observation_kind 为 "unknown"）
    的字段，且每个这样的字段都必须有非空原因（E_USAGE_UNKNOWN_REASON）；reported_cost 非 null 时
    billing_source 必须是 vendor_reported 或 provider_invoice（估算不能冒充实际费用，E_USAGE_VALUE）；
    context 给出时 request_id/run_id/role 必须一致（E_CONTEXT_BINDING；context 本身不合法时 field 以
    /context 开头）。不做记账（07 唯一记账）。
    """
    if registry is not None:
        _require_registry(registry)
    return _validate_usage(obs, context, registry, "")


def _money_key(value: dict[str, str] | None) -> tuple[decimal.Decimal, str] | None:
    return None if value is None else (decimal.Decimal(value["amount"]), value["currency"])


def _usage_numbers(obs: dict[str, Any]) -> tuple[Any, ...]:
    """比较“数值是否相同”时使用的内容键（不含 usage_id、supersedes、证据与原因文字）。

    含 billing_source：同一账单事件的厂商报告与适配器估算不是“重复”，而是需要核对的冲突。
    """
    return (obs["request_id"], obs["run_id"], obs["role"], obs["provider"], obs["model"],
            obs["observation_kind"], *(obs[name] for name in USAGE_COUNTERS),
            obs["input_includes_cache"], obs["output_includes_reasoning"],
            _money_key(obs["reported_cost"]), _money_key(obs["estimated_cost"]),
            obs["billing_source"])


def _billing_key(obs: dict[str, Any]) -> tuple[str, str] | None:
    if obs["provider"] is None or obs["billing_event_id"] is None:
        return None
    return (obs["provider"], obs["billing_event_id"])


def _check_cumulative_series(series: Sequence[dict[str, Any]]) -> None:
    """同一范围内按逻辑顺序排列的 cumulative/final 观测必须自洽（4.3.4、4.3.8）。

    同一费用字段的币种不得变化（E_USAGE_CURRENCY_MISMATCH）；各计量与同币种金额不得回退
    （E_USAGE_CONFLICT）。未知值（null）不参与比较。
    """
    for name in _COST_FIELDS:
        currencies = {obs[name]["currency"] for obs in series if obs[name] is not None}
        if len(currencies) > 1:
            raise ProtocolError("E_USAGE_CURRENCY_MISMATCH", phase="usage",
                                reason="cumulative_series_changes_currency")
    for name in USAGE_COUNTERS:
        highest = None
        for obs in series:
            value = obs[name]
            if value is None:
                continue
            if highest is not None and value < highest:
                raise ProtocolError("E_USAGE_CONFLICT", phase="usage",
                                    reason="cumulative_value_decreased")
            highest = value
    for name in _COST_FIELDS:
        highest_amount = None
        for obs in series:
            money = obs[name]
            if money is None:
                continue
            amount = decimal.Decimal(money["amount"])
            if highest_amount is not None and amount < highest_amount:
                raise ProtocolError("E_USAGE_CONFLICT", phase="usage",
                                    reason="cumulative_value_decreased")
            highest_amount = amount


def usage_view(observations: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """用量观测视图（4.3.8）。这不是记账：返回值带 view_kind 与 is_accounting=False。

    处理顺序：
    1. 逐条校验（validate_usage_observation 的规则，不含 context 绑定）。
    2. 同一 usage_id：快照字节相同只算一次；内容不同 E_USAGE_CONFLICT。
    3. 重复投递归并：修订链成环 → E_USAGE_CONFLICT；(provider, billing_event_id) 都非 null 且相同、
       数值（_usage_numbers，含 billing_source）相同、修订目标也相同（目标按已归并的规范观测比较）的
       观测，是同一观测的多次投递，归并为到达最早的规范观测。同一账单事件中数值不同或修订目标不同
       的观测**不**归并（它们是修订链上的版本或冲突）。
    4. 修订链（在规范观测上解析）：修订只替代它明确指向的目标，连同目标的重复投递；不替代同一账单
       事件中数值不同、也不在这条链上的观测（P2 返工，复审 fidelity/events-usage）。被替代的观测不
       计入；指向不存在的 id → unresolved；同一观测被两条不同的修订替代（分叉）、修订改变
       request/run/role → E_USAGE_CONFLICT；同一修订链（包括经由费用为 null 的中间版本）中同一费用
       字段出现两种币种 → E_USAGE_CURRENCY_MISMATCH。修订继承被替代观测中最早的到达位置（逻辑顺序）。
    5. 同一外部账单事件在修订之后只能有一个有效值：一条修订链代表链上全部观测的账单事件；代表同一
       账单事件的各有效观测数值相同视为重复（只计一次，保留逻辑位置最早的），不同 E_USAGE_CONFLICT。
       duplicates 计入第 2 步的同 id 重放、有效观测的重复投递与本步的重复；被替代观测的重复投递随目标
       一起列在 superseded 中，不计入 duplicates。provider 为 null 时账单号无法去重（5b）：按成员判定，
       带 billing_event_id 的成员自身 observation_kind 不是 unknown、或所在链的有效链尾可计入时，这条链代表
       该账单号（P4 第 5 轮起链尾为 unknown 的链也登记其已知成员）；某个账单号由 provider 为 null 的成员代表时，
       同号的全部代表链（含 provider 已知的，P4 第 4 轮）不区分 request/run/role，两条以上且至少一条可计入 →
       E_USAGE_SCOPE_UNKNOWN（P4 第 3 轮起；判定在第 6 步开头的范围歧义判定之后执行）。
    6. 范围 (request_id, run_id, role, provider, model)：provider 或 model 为 null 的范围来源不明——
       同一 (request_id, run_id, role) 下还有其他含可计入观测的范围时 E_USAGE_SCOPE_UNKNOWN（不猜
       口径）；单独存在时照常解释，但它计入的计量与费用组都标为不完整（scope 报告 ambiguous_scope）。
       delta 相加；cumulative/final 按逻辑顺序检查自洽（币种不变、数值不回退），cumulative 取最后
       一条；有 final 时取 final（多个 final 数值不同 E_USAGE_CONFLICT；final 之后又出现 cumulative
       是终止后的新内容 E_USAGE_CONFLICT）；同范围既有 delta 又有 cumulative/final
       E_USAGE_SCOPE_UNKNOWN；observation_kind=unknown 的数值不计入并使全部计量不完整。
       cumulative/final 计入的那条观测某个字段为 null 时，取序列中此前最后一个已知值作为下界（标为不完整、
       unknown_count 加 1），费用按序列已校验的唯一币种归组；序列中没有已知值时才落入 0 或 unknown_currency
       （P4 第 3 轮）。没计入的已知观测列在 scope 报告的 ignored_usage_ids 中（其中可能有为 null 字段提供
       下界的观测）。
    7. 每个计量与每个币种分别给出 {known_total, is_complete, unknown_count}；计量合计超过 2^53-1
       E_INTEGER_RANGE。任何未知都向对应结果传播；cache/reasoning 从不加进 input/output；估算
       费用从不并入 reported。输入为空（没有任何有效观测）时，各计量与两类费用的 unknown_currency 组都标为
       不完整。
    """
    if isinstance(observations, (str, bytes)) or not isinstance(observations, Sequence):
        raise ProtocolError("E_SCHEMA_TYPE", phase="usage", reason="sequence_of_observations_required")
    items = list(observations)
    for index, obs in enumerate(items):
        _validate_usage(obs, None, None, _pointer("", index))

    # 2. 同一 usage_id：字节相同只算一次；内容不同是冲突。
    by_id: dict[str, dict[str, Any]] = {}
    encoded_by_id: dict[str, bytes] = {}
    order: list[str] = []
    duplicates = 0
    for index, obs in enumerate(items):
        usage_id = obs["usage_id"]
        encoded = stable_snapshot_bytes(obs)
        if usage_id in by_id:
            if encoded_by_id[usage_id] == encoded:
                duplicates += 1
                continue
            raise ProtocolError("E_USAGE_CONFLICT", field=_pointer(_pointer("", index), "usage_id"),
                                phase="usage", reason="same_usage_id_different_content")
        by_id[usage_id] = obs
        encoded_by_id[usage_id] = encoded
        order.append(usage_id)
    arrival = {usage_id: position for position, usage_id in enumerate(order)}

    # 3. 修订指针图与账单事件重复投递的归并。每个观测至多指向一个修订目标：先沿指针求深度（修订
    #    链成环 → E_USAGE_CONFLICT），再按深度从低到高归并“同一观测的重复投递”——(provider,
    #    billing_event_id) 都非 null 且相同、数值（_usage_numbers）相同、修订目标（按已归并的规范观测
    #    比较）也相同的观测，归并为到达最早的规范观测。数值不同或修订目标不同的同账单事件观测**不**
    #    归并：它们是修订链上的版本或冲突，由第 4、5 步处理。
    depth: dict[str, int] = {}
    for start in order:
        path: list[str] = []
        on_path: set[str] = set()
        current = start
        while current not in depth:
            if current in on_path:
                raise ProtocolError("E_USAGE_CONFLICT", phase="usage", reason="revision_cycle")
            old_id = by_id[current]["supersedes_usage_id"]
            if old_id is None or old_id not in by_id:
                depth[current] = 0
                break
            path.append(current)
            on_path.add(current)
            current = old_id
        level = depth[current]
        for usage_id in reversed(path):
            level += 1
            depth[usage_id] = level
    numbers = {usage_id: _usage_numbers(by_id[usage_id]) for usage_id in order}
    canonical: dict[str, str] = {}
    deliveries: dict[tuple[Any, ...], str] = {}
    for usage_id in sorted(order, key=lambda item: (depth[item], arrival[item])):
        obs = by_id[usage_id]
        canonical[usage_id] = usage_id
        key = _billing_key(obs)
        if key is None:
            continue
        old_id = obs["supersedes_usage_id"]
        if old_id is None:
            target_token: tuple[str, str] | None = None
        elif old_id in by_id:
            target_token = ("usage", canonical[old_id])  # 目标深度更低，已归并
        else:
            target_token = ("missing", old_id)
        canonical[usage_id] = deliveries.setdefault((key, numbers[usage_id], target_token), usage_id)
    members: dict[str, list[str]] = {}
    for usage_id in order:
        members.setdefault(canonical[usage_id], []).append(usage_id)
    classes = list(members)  # 规范观测，按到达顺序；同一类的成员深度相同、数值与修订目标相同

    # 4. 修订链（在规范观测上解析）：修订只替代它**明确指向**的目标（连同目标的重复投递），不替代同一
    #    账单事件中数值不同、也不在这条链上的其他观测。被替代的观测不计入；指向不存在的 id →
    #    unresolved；同一观测被两条不同的修订替代（分叉）、修订改变 request/run/role → E_USAGE_CONFLICT。
    unresolved = [usage_id for usage_id in order
                  if by_id[usage_id]["supersedes_usage_id"] is not None
                  and by_id[usage_id]["supersedes_usage_id"] not in by_id]
    superseded_by: dict[str, str] = {}
    for revision in classes:
        obs = by_id[revision]
        old_id = obs["supersedes_usage_id"]
        if old_id is None or old_id not in by_id:
            continue
        target = canonical[old_id]
        if superseded_by.get(target, revision) != revision:
            raise ProtocolError("E_USAGE_CONFLICT", phase="usage", reason="observation_superseded_twice")
        old = by_id[target]
        if (old["request_id"], old["run_id"], old["role"]) != (obs["request_id"], obs["run_id"],
                                                               obs["role"]):
            raise ProtocolError("E_USAGE_CONFLICT", phase="usage", reason="revision_changes_binding")
        superseded_by[target] = revision
    # 没有分叉，所以每条修订分量都是一条链；head 是链尾（有效值）。
    head: dict[str, str] = {}
    for cls in sorted(classes, key=lambda item: -depth[item]):
        superseder = superseded_by.get(cls)
        head[cls] = cls if superseder is None else head[superseder]
    currencies: dict[tuple[str, str], set[str]] = {}
    for cls in classes:
        for name in _COST_FIELDS:
            money = by_id[cls][name]
            if money is not None:
                currencies.setdefault((head[cls], name), set()).add(money["currency"])
    if any(len(found) > 1 for found in currencies.values()):
        raise ProtocolError("E_USAGE_CURRENCY_MISMATCH", phase="usage",
                            reason="revision_changes_currency")
    heads = [cls for cls in classes if cls not in superseded_by]
    position = {cls: arrival[cls] for cls in heads}
    for cls in classes:  # 修订继承被替代观测中最早的到达位置
        position[head[cls]] = min(position[head[cls]], arrival[cls])
    superseded_ids = sorted(usage_id for cls in superseded_by for usage_id in members[cls])

    # 5. 同一外部账单事件在修订之后只能有一个有效值。一条修订链代表链上全部观测的账单事件；代表同一
    #    账单事件的各有效观测：数值相同视为重复投递（只计一次，保留逻辑位置最早的），不同 →
    #    E_USAGE_CONFLICT（与到达顺序无关；也覆盖“另一条观测与已修订事件的旧版本同账单号但数值不同”）。
    represented: dict[tuple[str, str], list[str]] = {}
    for cls in classes:
        key = _billing_key(by_id[cls])
        if key is not None:
            owners = represented.setdefault(key, [])
            if head[cls] not in owners:
                owners.append(head[cls])
    keeper = {cls: cls for cls in heads}

    def find_keeper(cls: str) -> str:
        while keeper[cls] != cls:
            keeper[cls] = keeper[keeper[cls]]
            cls = keeper[cls]
        return cls

    for owners in represented.values():
        first = owners[0]
        for other in owners[1:]:
            if numbers[other] != numbers[first]:
                raise ProtocolError("E_USAGE_CONFLICT", phase="usage",
                                    reason="same_billing_event_different_values")
            root_a, root_b = find_keeper(first), find_keeper(other)
            if root_a != root_b:
                if (position[root_b], arrival[root_b]) < (position[root_a], arrival[root_a]):
                    root_a, root_b = root_b, root_a
                keeper[root_b] = root_a
    kept: list[dict[str, Any]] = []
    for cls in heads:
        if find_keeper(cls) == cls:
            duplicates += len(members[cls]) - 1
            kept.append(by_id[cls])
        else:
            duplicates += len(members[cls])
    kept.sort(key=lambda item: position[item["usage_id"]])

    # 6. 按范围解释观测口径。
    counters = {name: {"known_total": 0, "is_complete": True, "unknown_count": 0}
                for name in USAGE_COUNTERS}
    costs: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in _COST_FIELDS}
    distributions = {name: {"true": 0, "false": 0, "unknown": 0}
                     for name in ("input_includes_cache", "output_includes_reasoning")}

    def cost_group(kind: str, currency: str) -> dict[str, Any]:
        return costs[kind].setdefault(currency, {"total": decimal.Decimal(0), "is_complete": True,
                                                 "unknown_count": 0})

    def last_known(prior: Sequence[dict[str, Any]], name: str) -> Any:
        for earlier in reversed(prior):
            if earlier[name] is not None:
                return earlier[name]
        return None

    def count(obs: dict[str, Any], prior: Sequence[dict[str, Any]] = (), ambiguous: bool = False) -> None:
        """计入一条观测。prior 是同一 cumulative/final 序列中逻辑位置在它之前的已知观测（delta 为空）：
        计入观测的某个字段为 null 时，取 prior 中最后一个已知值作为下界（序列已校验单调不减），同时标为
        不完整、unknown_count 加 1（P4 第 3 轮，复审 events-usage 视角：不丢掉此前已证明的累计值）；费用
        按序列已校验的唯一币种归组。ambiguous：来源范围不明，计入的计量与费用组都标为不完整。"""
        for name in USAGE_COUNTERS:
            value = obs[name]
            if value is None:
                counters[name]["unknown_count"] += 1
                counters[name]["is_complete"] = False
                value = last_known(prior, name)
            if value is not None:
                counters[name]["known_total"] += value
            if ambiguous:
                counters[name]["is_complete"] = False
        for name in _COST_FIELDS:
            money = obs[name]
            if money is None:
                bound = last_known(prior, name)
                group = cost_group(name, UNKNOWN_CURRENCY if bound is None else bound["currency"])
                if bound is not None:
                    group["total"] = _DECIMAL_CONTEXT.add(group["total"], decimal.Decimal(bound["amount"]))
                group["unknown_count"] += 1
                group["is_complete"] = False
            else:
                group = cost_group(name, money["currency"])
                group["total"] = _DECIMAL_CONTEXT.add(group["total"],
                                                     decimal.Decimal(money["amount"]))
            if ambiguous:
                group["is_complete"] = False
        for name, table in distributions.items():
            flag = obs[name]
            table["unknown" if flag is None else ("true" if flag else "false")] += 1

    def mark_unknown(obs: dict[str, Any]) -> None:
        for name in USAGE_COUNTERS:
            counters[name]["unknown_count"] += 1
            counters[name]["is_complete"] = False
        for name in _COST_FIELDS:
            money = obs[name]
            group = cost_group(name, UNKNOWN_CURRENCY if money is None else money["currency"])
            group["unknown_count"] += 1
            group["is_complete"] = False

    scopes: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for obs in kept:
        key = (obs["request_id"], obs["run_id"], obs["role"], obs["provider"], obs["model"])
        scopes.setdefault(key, []).append(obs)
    bindings: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
    for key, members in scopes.items():
        if any(obs["observation_kind"] != "unknown" for obs in members):
            bindings.setdefault(key[:3], []).append(key)
    for keys in bindings.values():
        if len(keys) > 1 and any(key[3] is None or key[4] is None for key in keys):
            raise ProtocolError("E_USAGE_SCOPE_UNKNOWN", phase="usage",
                                reason="scope_provider_or_model_unknown")

    # 5b. provider 为 null 时账单号的命名空间未知，无法判断同一账单号的两条有效观测是同一账单事件的重复
    #     投递还是两个事件（P4，复审 events-usage 视角）：不猜、不相加（相加得到的 known_total 可能是高估，
    #     而不是已知部分的下界）。P4 第 3 轮：与第 5 步同一口径，一条修订链代表链上**全部**成员的账单号，
    #     不只看链尾自己的 billing_event_id；只按 billing_event_id 分组，不区分 request/run/role（provider
    #     已知时同一账单号跨 run/role 是 E_USAGE_CONFLICT，信息更少的 null provider 不能更宽松）。
    #     P4 第 4 轮（复审 events-usage 视角，major）：只要某个 billing_event_id 由 provider 为 null 的成员
    #     代表（该成员所在链的有效链尾可计入），同一 billing_event_id 下 provider **已知**的可计入链尾也一并
    #     参与：null 与已知 provider 的同号观测同样无法判断是否同一事件，不能相加（此前只收 provider 为 null
    #     的成员，已知 + null 跨 run/role 时直接相加；修订链变体下重复计费却标为完整）。同一账单号的
    #     可计入有效链尾（observation_kind 不是 unknown）有两条以上 → E_USAGE_SCOPE_UNKNOWN。同一条修订链的
    #     多个成员只算一次。判定放在上面的范围歧义判定之后：同一 (request, run, role) 下 provider 已知与
    #     null 的范围并存时，先报 scope_provider_or_model_unknown（两者都是 E_USAGE_SCOPE_UNKNOWN）。
    #     P4 第 5 轮（复审 events-usage 视角，major）：按**成员**判定登记，不只看链尾是否可计入。成员自身的
    #     observation_kind 不是 unknown（它带有已知数值），或它所在链的有效链尾可计入时，这条链（去重后的保留者）
    #     就代表该成员的账单号；该成员 provider 为 null 时，这个账单号无法去重。无法去重的账单号有两条以上不同
    #     的代表链、且其中至少一条可计入时 → E_USAGE_SCOPE_UNKNOWN。此前链尾为 unknown 的链（a ← r(unknown)）不
    #     登记 a 的账单号：provider 为 null 时，a 的同号重投或冲突值 c 被单独计入（被替代的值复活、冲突被静默
    #     接受），而 provider 已知时同样的输入随 a 一起被替代（计 0）或报 E_USAGE_CONFLICT。成员与链尾都是
    #     unknown 的链不登记（它不计入，也不代表任何已知数值）。
    kept_ids = {obs["usage_id"] for obs in kept}

    def countable_owner(owner: str) -> bool:
        return owner in kept_ids and by_id[owner]["observation_kind"] != "unknown"

    billing_owners: dict[str, set[str]] = {}
    unscoped_billing: set[str] = set()
    for usage_id in order:
        member = by_id[usage_id]
        billing = member["billing_event_id"]
        if billing is None:
            continue
        owner = find_keeper(head[canonical[usage_id]])
        if member["observation_kind"] == "unknown" and not countable_owner(owner):
            continue
        billing_owners.setdefault(billing, set()).add(owner)
        if member["provider"] is None:
            unscoped_billing.add(billing)
    for billing in unscoped_billing:
        owners = billing_owners[billing]
        if len(owners) > 1 and any(countable_owner(owner) for owner in owners):
            raise ProtocolError("E_USAGE_SCOPE_UNKNOWN", phase="usage",
                                reason="billing_event_without_provider_repeated")
    scope_reports = []
    counted_ids: list[str] = []
    for key, members in scopes.items():
        known = [obs for obs in members if obs["observation_kind"] != "unknown"]
        kinds = {obs["observation_kind"] for obs in known}
        ambiguous = key[3] is None or key[4] is None
        if "delta" in kinds and kinds & {"cumulative", "final"}:
            raise ProtocolError("E_USAGE_SCOPE_UNKNOWN", phase="usage",
                                reason="delta_mixed_with_cumulative_or_final")
        prior: list[dict[str, Any]] = []  # cumulative/final：计入观测之前的序列成员（null 字段取其下界）
        if "delta" in kinds:
            mode, contributing = "delta", known
        elif kinds:
            _check_cumulative_series(known)
            final_positions = [index for index, obs in enumerate(known)
                               if obs["observation_kind"] == "final"]
            if final_positions:
                finals = [known[index] for index in final_positions]
                if len({_usage_numbers(obs) for obs in finals}) > 1:
                    raise ProtocolError("E_USAGE_CONFLICT", phase="usage",
                                        reason="multiple_final_values_without_revision")
                if any(obs["observation_kind"] == "cumulative"
                       for obs in known[final_positions[0] + 1:]):
                    raise ProtocolError("E_USAGE_CONFLICT", phase="usage",
                                        reason="observation_after_final")
                mode, contributing = "final", finals[-1:]
                prior = known[:final_positions[-1]]
            else:
                mode, contributing = "cumulative", known[-1:]
                prior = known[:-1]
        else:
            mode, contributing = "unknown_only", []
        contributing_ids = [obs["usage_id"] for obs in contributing]
        for obs in contributing:
            count(obs, prior, ambiguous)
        counted_ids.extend(contributing_ids)
        for obs in members:
            if obs["observation_kind"] == "unknown":
                mark_unknown(obs)
        scope_reports.append({"request_id": key[0], "run_id": key[1], "role": key[2],
                              "provider": key[3], "model": key[4], "mode": mode,
                              "ambiguous_scope": ambiguous,
                              "counted_usage_ids": contributing_ids,
                              "ignored_usage_ids": [obs["usage_id"] for obs in known
                                                    if obs["usage_id"] not in contributing_ids],
                              "unknown_kind_usage_ids": [obs["usage_id"] for obs in members
                                                         if obs["observation_kind"] == "unknown"]})
    for name in USAGE_COUNTERS:
        if counters[name]["known_total"] > MAX_SAFE_INTEGER:
            raise ProtocolError("E_INTEGER_RANGE", phase="usage",
                                reason="view_total_exceeds_safe_integer:" + name)

    # 7. 传播未知：未知币种的费用可能属于任何币种；未解析的修订链与“没有任何观测”都不完整。
    #    没有任何有效观测时，两类费用各给出一个 unknown_currency 组（unknown_count=0、不完整）：只读费用
    #    分组的下游不会把“没有观测”读成“费用为 0 且没有未知”（P4，复审 events-usage 视角）。
    if not kept:
        for name in _COST_FIELDS:
            cost_group(name, UNKNOWN_CURRENCY)
    for name in _COST_FIELDS:
        unknown_group = costs[name].get(UNKNOWN_CURRENCY)
        if unknown_group is not None and unknown_group["unknown_count"]:
            for group in costs[name].values():
                group["is_complete"] = False
    if unresolved or not kept:
        for metric in counters.values():
            metric["is_complete"] = False
        for groups in costs.values():
            for group in groups.values():
                group["is_complete"] = False

    def render_costs(groups: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {currency: {"known_total": format(group["total"], "f"),
                           "is_complete": group["is_complete"],
                           "unknown_count": group["unknown_count"]}
                for currency, group in sorted(groups.items())}

    view: dict[str, Any] = {
        "view_kind": "usage-observation-view",
        "is_accounting": False,
        "observation_count": len(items),
        "duplicates": duplicates,
        "superseded": superseded_ids,
        "unresolved_supersedes": unresolved,
        "counted_usage_ids": counted_ids,
    }
    for name in USAGE_COUNTERS:
        view[name] = dict(counters[name])
    view["reported_cost"] = render_costs(costs["reported_cost"])
    view["estimated_cost"] = render_costs(costs["estimated_cost"])
    view["distributions"] = distributions
    view["scopes"] = scope_reports
    view["is_complete"] = (all(metric["is_complete"] for metric in counters.values())
                           and all(group["is_complete"] for groups in costs.values()
                                   for group in groups.values())
                           and not unresolved and bool(kept))
    return view


# ---------------------------------------------------------------------------
# fixture 来源清单（4.3.3、5.3、附录 C.1、01-A12）
# ---------------------------------------------------------------------------

#: 条目字段（封闭）。integrity、platform_scope、capture 由裁决 R-P11 新增，排在 expected 之后：缺字段按本元组
#: 的顺序报告，附录 C.1 示例（缺 expected）仍先报 /expected。
_FIXTURE_ENTRY_FIELDS = ("schema_version", "record_type", "fixture_id", "origin", "representation",
                         "redaction", "resource", "proves", "does_not_prove", "expected",
                         "integrity", "platform_scope", "capture")
_ORIGIN_FIELDS = ("class", "source_revision", "source_test_id", "source_path", "generator_revision",
                  "derived_from", "fault", "description", "traceable")
_REDACTION_FIELDS = ("applied", "rule_revision", "length_changed", "reason", "protocol_structure_preserved")
_EXPECTED_FIELDS = ("outcome", "code", "entry", "expected_ref")
_CAPTURE_FIELDS = ("environment_ref", "range")
EXPECTED_OUTCOMES = ("accept", "reject", "legacy-judgment")
#: fixture 的完整性（R-P11，文档 6.6“封存”一步的“完整性”）。
FIXTURE_INTEGRITY_VALUES = ("complete", "partial", "truncated", "corrupted")
#: platform_scope 取 ["any"]，或 PLATFORMS 的非空、无重复子集（R-P11，文档 1.1“平台适用性”）。
PLATFORM_SCOPE_ANY = "any"
#: integrity 不是 complete 的条目不得声称完整运行流（R-P11）。这两个前缀是收尾轮实施者选定的典型名称（裁决原文只写
#: “不得声称完整运行流”，没有规定名称）；收尾轮返工起判定改按下面的受控词表（_is_complete_stream_claim），这两个
#: 前缀开头的声明都落在词表之内。
COMPLETE_STREAM_CLAIM_PREFIXES = ("complete_run_stream", "full_run")
#: protocol_structure_preserved=false 的条目不得声称证明协议字段语义（R-P11，文档 4.3.3“无法保证语义字段未被脱敏
#: 破坏时，该 fixture 不能证明该字段的真实协议兼容性”）。名称同样由实施者选定；判定按受控词表
#: （_is_field_semantics_claim），以它开头的声明落在词表之内。
PROTOCOL_FIELD_SEMANTICS_CLAIM = "protocol_field_semantics"

# 收尾轮返工（复审 fidelity 视角 major）：声明的词法是开放的（[a-z][a-z0-9_]{0,127}），只按上面两个前缀判定时，
# complete_run、run_stream_complete、full_event_log、entire_run_stream、field_semantics 这类同义名称都能绕过 R-P11
# 的两条规则。改为按受控词表判定：声明按 "_" 切成词元，按词元的开头匹配词干（不做子串匹配，所以 incomplete_*、
# successfully_* 不算完整性声明）。词表之外的写法（例如把词干接在其他词后面的 runcomplete）不受限制，见
# protocols-api.md 14.2“现行规则的空缺”。
#: 完整性词干：任一词元以其中之一开头即为完整性声明（complete、completed、completeness、completion、full、fully、
#: fullrun、entire、entirely、whole、intact、untruncated、unabridged、endtoend、e2e……）。
COMPLETENESS_CLAIM_STEMS = ("complet", "full", "entire", "whole", "intact", "untruncat", "unabridg", "endtoend",
                            "e2e")
#: 完整性词组：连续出现这些词元即为完整性声明（end_to_end_run 等）。
COMPLETENESS_CLAIM_PHRASES = (("end", "to", "end"),)
#: 全称量词：词元恰为其一，且另有词元以 STREAM_SUBJECT_STEMS 之一开头时为完整性声明（all_events_present 等）。
COMPLETENESS_CLAIM_QUANTIFIERS = ("all", "every")
#: 运行流的对象词干（只与全称量词配合使用）。
STREAM_SUBJECT_STEMS = ("run", "stream", "event", "log", "record", "line", "message", "transcript", "session",
                        "output", "trace", "segment")
#: 语义词干：任一词元以其中之一开头即为协议字段语义声明（semantic、semantics、semantically、meaning……）。
FIELD_SEMANTICS_CLAIM_STEMS = ("semantic", "meaning")
#: 协议对象词干与字段词干：同时含两类词元时也是协议字段语义声明（protocol_fields、wire_field_types 等）。
PROTOCOL_SUBJECT_STEMS = ("protocol", "wire")
FIELD_SUBJECT_STEMS = ("field",)


def _claim_tokens(claim: str) -> tuple[str, ...]:
    return tuple(claim.split("_"))


def _has_token_stem(tokens: Sequence[str], stems: Sequence[str]) -> bool:
    return any(token.startswith(tuple(stems)) for token in tokens if token)


def _is_complete_stream_claim(claim: str) -> bool:
    """声明是否属于“完整运行流”一类（R-P11 第 5 步第一条的受控词表，收尾轮返工）。"""
    if claim.startswith(COMPLETE_STREAM_CLAIM_PREFIXES):
        return True
    tokens = _claim_tokens(claim)
    if _has_token_stem(tokens, COMPLETENESS_CLAIM_STEMS):
        return True
    for phrase in COMPLETENESS_CLAIM_PHRASES:
        width = len(phrase)
        if any(tokens[i:i + width] == phrase for i in range(len(tokens) - width + 1)):
            return True
    return (any(token in COMPLETENESS_CLAIM_QUANTIFIERS for token in tokens)
            and _has_token_stem(tokens, STREAM_SUBJECT_STEMS))


def _is_field_semantics_claim(claim: str) -> bool:
    """声明是否属于“协议字段语义”一类（R-P11 第 5 步第二条的受控词表，收尾轮返工）。"""
    if claim.startswith(PROTOCOL_FIELD_SEMANTICS_CLAIM):
        return True
    tokens = _claim_tokens(claim)
    if _has_token_stem(tokens, FIELD_SEMANTICS_CLAIM_STEMS):
        return True
    return _has_token_stem(tokens, PROTOCOL_SUBJECT_STEMS) and _has_token_stem(tokens, FIELD_SUBJECT_STEMS)


def _is_real_claim(claim: str) -> bool:
    return claim in REAL_COMPATIBILITY_CLAIMS or claim.startswith(REAL_CLAIM_PREFIXES)


def _is_vendor_claim(claim: str) -> bool:
    return claim in VENDOR_COMPATIBILITY_CLAIMS or claim.startswith(VENDOR_CLAIM_PREFIXES)


#: 能力声明的封闭词法（P4 第 3 轮，复审 tests-mutation 视角，major）：小写 ASCII 字母开头，只含小写字母、数字
#: 与下划线，最长 128。大小写或连字符变体（REAL_CLI_COMPATIBILITY、Vendor_Protocol、vendor-protocol）在结构层
#: 即被拒绝，不能绕过按精确名称与 real_/vendor_/legacy_ 前缀判定的来源规则。
_CLAIM_RE = re.compile(r"[a-z][a-z0-9_]{0,127}")


def _claim_list(value: Any, field: str) -> list[str]:
    items = _expect_list(value, field)
    for index, item in enumerate(items):
        pointer = _pointer(field, index)
        _require_str(item, pointer, max_len=128)
        if _CLAIM_RE.fullmatch(item) is None:
            raise ProtocolError("E_SCHEMA_FIELDS", field=pointer, phase="schema", reason="claim_syntax")
    return items


def _platform_scope(value: Any, field: str) -> list[str]:
    """R-P11：["any"]，或 PLATFORMS 的非空、无重复子集。不是数组 E_SCHEMA_TYPE；其余 E_SCHEMA_FIELDS。"""
    items = _expect_list(value, field)
    if not items:
        raise ProtocolError("E_SCHEMA_FIELDS", field=field, phase="schema", reason="platform_scope_empty")
    seen: set[str] = set()
    for index, item in enumerate(items):
        pointer = _pointer(field, index)
        if type(item) is str and item == PLATFORM_SCOPE_ANY:
            if len(items) != 1:
                raise ProtocolError("E_SCHEMA_FIELDS", field=pointer, phase="schema",
                                    reason="platform_scope_any_must_be_alone")
        else:
            _enum(item, PLATFORMS, pointer)
        if item in seen:
            raise ProtocolError("E_SCHEMA_FIELDS", field=pointer, phase="schema", reason="duplicate_platform")
        seen.add(item)
    return items


def _validate_fixture_entry(entry: Any, registry: ResourceKindRegistry, field: str) -> None:
    _expect_dict(entry, field or None)
    _check_record_json(entry, field)
    _check_record_header(entry, "fixture-manifest", field)
    _require_closed(entry, _FIXTURE_ENTRY_FIELDS, field=field)

    def ptr(*names: str) -> str:
        result = field
        for name in names:
            result = _pointer(result, name)
        return result

    def claim_error(reason: str, *names: str) -> ProtocolError:
        return ProtocolError("E_PROVENANCE_CLAIM", field=ptr(*names), phase="provenance", reason=reason)

    fixture_id = validate_logical_id(entry["fixture_id"], field=ptr("fixture_id"))
    origin = _expect_dict(entry["origin"], ptr("origin"))
    _require_closed(origin, _ORIGIN_FIELDS, field=ptr("origin"))
    origin_class = _enum(origin["class"], ORIGIN_CLASSES, ptr("origin", "class"))
    revision = origin["source_revision"]
    if revision is not None and (type(revision) is not str or _ANY_GIT_OID_RE.fullmatch(revision) is None):
        raise ProtocolError("E_GIT_OID_SYNTAX", field=ptr("origin", "source_revision"), phase="provenance",
                            reason="full_lowercase_oid_required")
    _require_str(origin["source_test_id"], ptr("origin", "source_test_id"), max_len=512, allow_null=True)
    if origin["source_path"] is not None:
        validate_business_path(origin["source_path"], field=ptr("origin", "source_path"))
    _require_str(origin["generator_revision"], ptr("origin", "generator_revision"), max_len=128,
                 allow_null=True)
    if origin["derived_from"] is not None:
        validate_logical_id(origin["derived_from"], field=ptr("origin", "derived_from"))
    _require_str(origin["fault"], ptr("origin", "fault"), max_len=512, allow_null=True)
    _require_str(origin["description"], ptr("origin", "description"), max_len=4096)
    _require_bool(origin["traceable"], ptr("origin", "traceable"))
    representation = _enum(entry["representation"], REPRESENTATIONS, ptr("representation"))
    redaction = _expect_dict(entry["redaction"], ptr("redaction"))
    _require_closed(redaction, _REDACTION_FIELDS, field=ptr("redaction"))
    _require_bool(redaction["applied"], ptr("redaction", "applied"))
    _require_str(redaction["rule_revision"], ptr("redaction", "rule_revision"), max_len=128,
                 allow_null=True)
    _require_bool(redaction["length_changed"], ptr("redaction", "length_changed"), allow_null=True)
    _require_str(redaction["reason"], ptr("redaction", "reason"), max_len=4096)
    _require_bool(redaction["protocol_structure_preserved"], ptr("redaction", "protocol_structure_preserved"),
                  allow_null=True)
    _validate_ref(entry["resource"], registry, ptr("resource"))
    if entry["resource"]["kind"] != "fixture":
        raise ProtocolError("E_RESOURCE_KIND", field=ptr("resource", "kind"), phase="provenance",
                            reason="fixture_resource_kind_required")
    proves = _claim_list(entry["proves"], ptr("proves"))
    does_not_prove = _claim_list(entry["does_not_prove"], ptr("does_not_prove"))
    expected = _expect_dict(entry["expected"], ptr("expected"))
    _require_closed(expected, _EXPECTED_FIELDS, field=ptr("expected"))
    outcome = _enum(expected["outcome"], EXPECTED_OUTCOMES, ptr("expected", "outcome"))
    _require_str(expected["code"], ptr("expected", "code"), max_len=128, allow_null=True)
    _require_str(expected["entry"], ptr("expected", "entry"), max_len=256)
    if expected["expected_ref"] is not None:
        _validate_ref(expected["expected_ref"], registry, ptr("expected", "expected_ref"))
    if outcome == "reject" and expected["code"] is None:
        raise ProtocolError("E_SCHEMA_FIELDS", field=ptr("expected", "code"), phase="provenance",
                            reason="reject_requires_code")
    if outcome == "accept" and expected["code"] is not None:
        raise ProtocolError("E_SCHEMA_FIELDS", field=ptr("expected", "code"), phase="provenance",
                            reason="accept_must_not_carry_code")
    # R-P11 新增的三个条目字段（结构检查）。
    integrity = _enum(entry["integrity"], FIXTURE_INTEGRITY_VALUES, ptr("integrity"))
    _platform_scope(entry["platform_scope"], ptr("platform_scope"))
    capture = entry["capture"]
    if capture is not None:
        _expect_dict(capture, ptr("capture"))
        _require_closed(capture, _CAPTURE_FIELDS, field=ptr("capture"))
        _validate_ref(capture["environment_ref"], registry, ptr("capture", "environment_ref"))
        _require_str(capture["range"], ptr("capture", "range"), max_len=512)

    # 来源一致性（E_PROVENANCE_CLAIM）。
    if set(proves) & set(does_not_prove):
        raise claim_error("claim_both_proved_and_not_proved", "proves")
    if redaction["applied"] and redaction["rule_revision"] is None:
        raise claim_error("redaction_requires_rule_revision", "redaction", "rule_revision")
    # 4.3.3：脱敏元数据至少记录“是否改变字节长度”（P4，复审 fidelity 视角）。
    if redaction["applied"] and redaction["length_changed"] is None:
        raise claim_error("redaction_requires_length_changed", "redaction", "length_changed")
    if representation == "redacted_raw" and not redaction["applied"]:
        raise claim_error("redacted_raw_requires_applied_redaction", "redaction", "applied")
    # 4.3.3“避免‘原始’一词掩盖内容已变换”：脱敏过的字节不能标为 captured_raw（P4）。
    if representation == "captured_raw" and redaction["applied"]:
        raise claim_error("captured_raw_must_not_be_redacted", "redaction", "applied")
    if origin_class == "synthetic":
        if representation != "synthetic_fixture":
            raise claim_error("synthetic_requires_synthetic_fixture", "representation")
        if any(_is_real_claim(claim) for claim in proves):
            raise claim_error("synthetic_cannot_prove_real_compatibility", "proves")
    elif origin_class == "captured":
        # 4.3.3“标准化事件保留其来源”：采集流经适配器解释出的标准事件可以登记为 captured + normalized_event，
        # 但必须写明解释它的修订（generator_revision）；合成样本仍不能冒充采集（P4，复审 fidelity 视角）。
        if representation not in ("captured_raw", "redacted_raw", "normalized_event"):
            raise claim_error("captured_requires_captured_redacted_or_normalized", "representation")
        if revision is None and origin["source_test_id"] is None:
            raise claim_error("captured_requires_source", "origin", "source_revision")
        if representation == "normalized_event" and origin["generator_revision"] is None:
            raise claim_error("normalized_event_requires_generator_revision", "origin", "generator_revision")
    elif origin_class == "legacy":
        if revision is None:
            raise claim_error("legacy_requires_source_revision", "origin", "source_revision")
        if origin["source_path"] is None and origin["source_test_id"] is None:
            raise claim_error("legacy_requires_source_path_or_test", "origin", "source_path")
    else:  # negative
        if origin["derived_from"] is None or origin["fault"] is None:
            raise claim_error("negative_requires_derived_from_and_fault", "origin", "derived_from")
        if origin["derived_from"] == fixture_id:
            raise claim_error("negative_cannot_derive_from_itself", "origin", "derived_from")
        # 注入故障后的字节不再是采集字节（P4）。
        if representation in ("captured_raw", "redacted_raw"):
            raise claim_error("negative_cannot_use_captured_representation", "representation")
    # R-P11（文档 4.3.3“来源是否可追溯”、附录 C.1“采集环境、截取范围”）：legacy 与 captured 的来源必须可追溯；
    # captured 必须有 capture，其他来源类别 capture 必须为 null。
    if origin_class in ("legacy", "captured") and origin["traceable"] is not True:
        raise claim_error("traceable_required_for_legacy_or_captured", "origin", "traceable")
    if origin_class == "captured" and capture is None:
        raise claim_error("captured_requires_capture", "capture")
    if origin_class != "captured" and capture is not None:
        raise claim_error("capture_only_for_captured", "capture")
    # 能力声明按来源类别与表示判定，作用于全部条目（P4，fixtures 复审 major）：synthetic_fixture 条目
    # （包括 legacy 类的 stub 流与 negative 类）不能靠换来源类别声称真实兼容。
    for claim in proves:
        if _is_vendor_claim(claim):
            if origin_class != "captured" or representation not in ("captured_raw", "redacted_raw"):
                raise claim_error("real_compatibility_claim_requires_captured_raw", "proves")
        elif claim == LEGACY_DISPATCH_CLAIM:
            if origin_class != "legacy" or outcome != "legacy-judgment":
                raise claim_error("legacy_dispatch_claim_requires_legacy_judgment", "proves")
        elif claim.startswith("legacy_") and origin_class not in ("legacy", "negative"):
            raise claim_error("legacy_claim_requires_legacy_or_negative_origin", "proves")
    # R-P11：完整性与协议结构对能力声明的限制（作用于全部条目，排在上面的来源规则之后）。
    # protocol_structure_preserved=false 不得声称协议字段语义（R-P11 原文）；脱敏过（applied=true）却没有记录是否
    # 保留协议结构（null）时同样不得声称：4.3.3“无法保证语义字段未被脱敏破坏时，该 fixture 不能证明该字段的真实
    # 协议兼容性”（收尾轮，保守方向；null 本身仍合法）。
    # 两类声明都按受控词表判定（收尾轮返工），不只看 COMPLETE_STREAM_CLAIM_PREFIXES 与 PROTOCOL_FIELD_SEMANTICS_CLAIM
    # 两个前缀：同义名称（complete_run、full_event_log、field_semantics、protocol_fields 等）同样拒绝。
    structure = redaction["protocol_structure_preserved"]
    for claim in proves:
        if integrity != "complete" and _is_complete_stream_claim(claim):
            raise claim_error("incomplete_fixture_cannot_prove_complete_stream", "proves")
        if _is_field_semantics_claim(claim):
            if structure is False:
                raise claim_error("structure_not_preserved_cannot_prove_field_semantics", "proves")
            if structure is None and redaction["applied"]:
                raise claim_error("structure_preservation_unrecorded_cannot_prove_field_semantics", "proves")


def validate_fixture_manifest_entry(entry: Any, registry: ResourceKindRegistry) -> None:
    """fixture manifest 单条目：封闭结构 + 来源一致性（E_PROVENANCE_CLAIM）。

    规则（protocols-api.md 14.2）：proves 与 does_not_prove 不相交；redaction.applied=true 时 rule_revision
    与 length_changed 非空；redacted_raw 必须 applied=true，captured_raw 必须 applied=false；synthetic 只能
    是 synthetic_fixture 且不得声称 real_/vendor_/legacy_ 能力；captured 只能是 captured_raw、redacted_raw
    或 normalized_event（后者要求 generator_revision），且须有 source_revision 或 source_test_id；legacy 须有
    完整 OID 与 source_path 或 source_test_id；negative 须有 derived_from（不是自身）与 fault，且不能用
    captured_raw/redacted_raw。能力声明对**全部**条目生效：real_cli_compatibility、vendor_protocol 与
    real_/vendor_ 前缀只允许 captured + captured_raw/redacted_raw；legacy_dispatch_behavior 只允许
    legacy + expected.outcome=legacy-judgment；其他 legacy_ 前缀只允许 legacy 或 negative。
    proves 与 does_not_prove 中的每个声明都必须符合封闭词法 [a-z][a-z0-9_]{0,127}，否则 E_SCHEMA_FIELDS
    （/proves/<i>，reason=claim_syntax；P4 第 3 轮），大小写与连字符变体不能绕过上述规则。

    收尾轮（裁决 R-P11）新增字段：integrity（FIXTURE_INTEGRITY_VALUES）、platform_scope（["any"] 或 PLATFORMS
    的非空无重复子集）、capture（null 或 {"environment_ref": ResourceRef, "range": 1–512 字符}）、
    redaction.protocol_structure_preserved（布尔或 null）、origin.traceable（布尔）；字段仍然封闭。新增规则
    （E_PROVENANCE_CLAIM）：legacy 与 captured 的 traceable 必须为 true；captured 必须有 capture，其他来源类别
    capture 必须为 null；integrity 不是 complete 时 proves 不得含完整运行流一类的声明；
    protocol_structure_preserved=false（或 applied=true 而它为 null）时 proves 不得含协议字段语义一类的声明。
    两类声明按受控词表判定（收尾轮返工）：以 COMPLETE_STREAM_CLAIM_PREFIXES / PROTOCOL_FIELD_SEMANTICS_CLAIM 开头，
    或按 "_" 切出的词元以 COMPLETENESS_CLAIM_STEMS（含 COMPLETENESS_CLAIM_PHRASES，以及
    COMPLETENESS_CLAIM_QUANTIFIERS 配 STREAM_SUBJECT_STEMS）/ FIELD_SEMANTICS_CLAIM_STEMS（以及
    PROTOCOL_SUBJECT_STEMS 配 FIELD_SUBJECT_STEMS）开头。词表之外的写法不受限制。检查顺序与 reason 见
    protocols-api.md 14.2。
    """
    _validate_fixture_entry(entry, _require_registry(registry), "")


def validate_fixture_manifest_set(doc: Any, registry: ResourceKindRegistry) -> dict[str, Any]:
    """整份 manifest：{"schema_version": 1, "record_type": "fixture-manifest-set", "entries": [...]}。

    整份清单先做 JSON 原生值检查（与单条记录相同的 1 MiB 字节下界预算，共享容器不会指数级遍历；P4）。
    逐条校验；fixture_id 不得重复（E_SCHEMA_FIELDS）；resource.path 不得大小写/归一化碰撞
    （E_PATH_COLLISION），同一拼写只允许指向同一摘要；negative 的 derived_from 必须是本清单中的
    fixture_id，且不能是另一个 negative（reason=negative_must_derive_from_valid_sample，P4 第 3 轮），声明
    legacy_ 前缀能力的 negative 必须从 legacy 条目派生（E_PROVENANCE_CLAIM）。
    条目结构与单条规则（含收尾轮 R-P11 新增的 integrity、platform_scope、capture、
    redaction.protocol_structure_preserved、origin.traceable）见 validate_fixture_manifest_entry。
    清单不含自身摘要。返回 {"entries": 条目数, "fixture_ids": [...]}。
    """
    _require_registry(registry)
    _expect_dict(doc, None)
    _check_record_json(doc)
    _check_record_header(doc, "fixture-manifest-set")
    _require_closed(doc, ("schema_version", "record_type", "entries"))
    entries = _expect_list(doc["entries"], "/entries")
    ids: dict[str, int] = {}
    paths: dict[str, str] = {}
    for index, entry in enumerate(entries):
        field = _pointer("/entries", index)
        _validate_fixture_entry(entry, registry, field)
        fixture_id = entry["fixture_id"]
        if fixture_id in ids:
            raise ProtocolError("E_SCHEMA_FIELDS", field=_pointer(field, "fixture_id"),
                                phase="provenance", reason="duplicate_fixture_id")
        ids[fixture_id] = index
        path, digest = entry["resource"]["path"], entry["resource"]["sha256"]
        if path in paths and paths[path] != digest:
            raise ProtocolError("E_PATH_COLLISION", field=_pointer(_pointer(field, "resource"), "path"),
                                phase="provenance", reason="same_path_different_digest")
        paths[path] = digest
    check_no_name_collisions(paths)
    for index, entry in enumerate(entries):
        field = _pointer("/entries", index)
        derived = entry["origin"]["derived_from"]
        if derived is not None and derived not in ids:
            raise ProtocolError("E_PROVENANCE_CLAIM", field=_pointer(_pointer(field, "origin"), "derived_from"),
                                phase="provenance", reason="derived_from_unknown_fixture")
        # 7.4“每个负例从一份通过该层全部前置校验的有效样本复制”：负例不能从另一个负例派生（链式派生与负例之间
        # 的派生环都在此拒绝；单条目只能看到自引用，P4 第 3 轮，复审 tests-mutation 视角）。
        if (entry["origin"]["class"] == "negative"
                and entries[ids[derived]]["origin"]["class"] == "negative"):
            raise ProtocolError("E_PROVENANCE_CLAIM", field=_pointer(_pointer(field, "origin"), "derived_from"),
                                phase="provenance", reason="negative_must_derive_from_valid_sample")
        # 负例从 legacy 样本派生时才能沿用 legacy_ 能力（旧实现对该负例的判断）；从合成样本派生的负例
        # 不能声称旧行为（P4，fixtures 复审 major 的清单层部分）。
        if (entry["origin"]["class"] == "negative"
                and any(claim.startswith("legacy_") for claim in entry["proves"])
                and entries[ids[derived]]["origin"]["class"] != "legacy"):
            raise ProtocolError("E_PROVENANCE_CLAIM", field=_pointer(field, "proves"), phase="provenance",
                                reason="negative_legacy_claim_requires_legacy_base")
    return {"entries": len(entries), "fixture_ids": list(ids)}


__all__ = [
    "PROTOCOL_FAMILY", "PUBLIC_SCHEMA_VERSION", "SCHEMA_ID", "SNAPSHOT_PROFILE", "MAX_SAFE_INTEGER",
    "DEFAULT_MAX_BYTES", "DEFAULT_MAX_DEPTH", "ROLES", "PLATFORMS", "PUBLIC_RECORD_TYPES",
    "REPRESENTATIONS", "ORIGIN_CLASSES", "LEGACY_VERDICTS", "UNKNOWABLE_USAGE_FIELDS",
    "ERROR_CODES", "ErrorSpec", "ProtocolError",
    "strict_json_loads", "check_json_value", "stable_snapshot_bytes", "sha256_hex",
    "new_request_id", "new_run_id", "validate_request_id", "validate_run_id", "validate_task_id",
    "validate_logical_id", "validate_resource_kind", "validate_sha256", "require_int",
    "validate_plan_version", "validate_attempt", "validate_role", "validate_platform",
    "platform_from_system", "validate_run_context", "derive_run_context", "route_public_record",
    "require_schema_version", "validate_public_record",
    "validate_business_path", "find_name_collisions", "check_no_name_collisions",
    "resolve_existing_path_for_review", "PATH_REVIEW_LIMITATIONS",
    "ResourceKind", "ResourceKindRegistry", "build_default_registry", "validate_resource_ref",
    "verify_resource_bytes", "check_resource_dag", "PAYLOAD_REPRESENTATIONS", "RESOURCE_KIND_DECLARED_FIELDS",
    "validate_resource_payload",
    "validate_git_oid", "git_cat_file_type_argv", "interpret_git_object_type",
    "format_observed_at", "parse_observed_at",
    "EventType", "EventTypeRegistry", "build_default_event_types", "validate_raw_span",
    "validate_run_event", "assess_stream_integrity", "assess_event_log", "append_event",
    "recover_next_seq", "recover_append_state", "build_run_dimensions",
    "validate_usage_observation", "usage_view",
    "validate_fixture_manifest_entry", "validate_fixture_manifest_set",
    "FIXTURE_INTEGRITY_VALUES", "PLATFORM_SCOPE_ANY", "COMPLETE_STREAM_CLAIM_PREFIXES",
    "PROTOCOL_FIELD_SEMANTICS_CLAIM", "COMPLETENESS_CLAIM_STEMS", "COMPLETENESS_CLAIM_PHRASES",
    "COMPLETENESS_CLAIM_QUANTIFIERS", "STREAM_SUBJECT_STEMS", "FIELD_SEMANTICS_CLAIM_STEMS",
    "PROTOCOL_SUBJECT_STEMS", "FIELD_SUBJECT_STEMS",
]
