"""分析评测:真实 POST /api/faults/{id}/analyze 路由 + 合成模型边界适配器。

覆盖:
- 分类/弃答分母(正常质量 suite);
- 原始引用声明(清洗前)与最终引用(清洗后)分开统计;
- 格式修复初次/最终输出分开标记;
- 失败注入(timeout/429/非法 JSON/Schema/无效引用/部分 usage/调用期间失效);
- 缓存复用(新增调用为 0)与来源复核探针;
- 无权分析 403、注入文本无副作用;
- 进程内 API 耗时(非生产网络端到端性能)。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import func, select

from app.ai.json_out import extract_json
from app.ai.schema import AnalysisReport
from app.ai.service import _source_currently_valid
from app.eval.datasets import Dataset
from app.eval.metrics import metric, timing_summary
from app.eval.synthetic import SyntheticChatAdapter, SyntheticCall
from app.models import AuditEvent, Sop, SopStatus, User
from app.seed import SYSTEM_CATEGORIES

CATEGORY_NAMES = {name for name, _ in SYSTEM_CATEGORIES}

# 受控来源 ID:只允许后端下发的 case/sop + 正整数 ID,可选 #v<版本>。
# 其他任何模型返回字符串都视为不可信,不原样进入报告。
_CONTROLLED_REF_RE = re.compile(r"^(case|sop):(\d{1,18})(?:#v(\d{1,9}))?$")
_UNTRUSTED_TOKEN_RE = re.compile(r"^<untrusted:[a-z_]+:[a-zA-Z]+:len=\d+:sha256=[0-9a-f]{12}>$")


def _safe_ref_token(value) -> str:
    """把不可信引用值转成受控、限长的安全标识,不保留原始文本。

    受控格式的来源 ID 原样保留;任意其他字符串/数字/对象只记录类型、长度
    和 SHA-256 前缀,避免模型输出中的敏感串进入报告。
    """
    if isinstance(value, str) and _CONTROLLED_REF_RE.fullmatch(value):
        return value
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=lambda o: f"<{type(o).__name__}>")
    except Exception:  # noqa: BLE001
        raw = f"<{type(value).__name__}>"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    type_name = type(value).__name__
    length = min(len(raw), 100_000)
    return f"<untrusted:ref:{type_name}:len={length}:sha256={digest}>"


def _raw_ref_id(value) -> str | None:
    """仅接受受控来源 ID 作为可匹配/可展示的 ref_id。"""
    if isinstance(value, str) and _CONTROLLED_REF_RE.fullmatch(value):
        return value
    return None


def _ref_identity_for_final(ref_id: str) -> tuple[str, int, int | None] | None:
    """只解析 ref_id 中编码的身份;显式字段不能覆盖它。"""
    m = _CONTROLLED_REF_RE.fullmatch(ref_id or "")
    if m is None:
        return None
    return m.group(1), int(m.group(2)), (int(m.group(3)) if m.group(3) else None)


def _final_ref_is_valid(db, ref_id: str, fallback: dict | None) -> tuple[bool | None, str]:
    """按当前数据库状态/版本复核最终展示引用。

    身份一致性优先:ref_id 编码的 type/ID/version 必须与显式 source_type/
    source_id/version 一致;显式冲突直接判无效,不能用 fallback 覆盖成有效。
    返回 (True/False/None, reason);None 表示缺少数据库复核会话,不可观察。
    """
    ident = _ref_identity_for_final(ref_id)
    if ident is None:
        return False, "uncontrolled_ref_id"
    stype, sid, encoded_version = ident
    effective_version = encoded_version
    if fallback is not None:
        fallback_type = fallback.get("source_type")
        if fallback_type not in ("case", "sop"):
            return False, "invalid_source_type"
        if fallback_type != stype:
            return False, "source_type_conflict"
        try:
            fallback_id = int(fallback.get("source_id"))
        except (TypeError, ValueError):
            return False, "malformed_source_id"
        if fallback_id != sid:
            return False, "source_id_conflict"
        fallback_version = fallback.get("version")
        if fallback_version is not None:
            if not isinstance(fallback_version, int) or isinstance(fallback_version, bool):
                return False, "invalid_version"
            if stype == "case":
                return False, "case_version_must_be_null"
            if encoded_version is not None and fallback_version != encoded_version:
                return False, "version_conflict"
            effective_version = fallback_version
        elif stype == "case" and encoded_version is not None:
            return False, "case_version_must_be_null"
    elif stype == "case" and encoded_version is not None:
        return False, "case_version_must_be_null"
    if db is None:
        return None, "no_database_revalidation_session"
    return (
        (True, "")
        if _source_currently_valid(db, stype, sid, effective_version)
        else (False, "source_not_current_valid")
    )


@dataclass
class AnalysisRun:
    report: dict
    exit_code: int


def _usage_status(usage: dict | None) -> str:
    if usage is None:
        return "unknown"
    required = {"prompt_tokens", "completion_tokens", "total_tokens"}
    if required <= set(usage):
        return "complete"
    return "partial"


def _raw_ref_summary(raw: dict | None, prompt_refs: list[dict]) -> dict | None:
    """统计模型原始输出(清洗前)的引用声明。

    口径:每一个可观察的声明都进入分母,包括非对象元素、缺少 ref_id、
    未知 ID 和类型/ID/版本不匹配;valid + invalid 必须等于 occurrences。
    不可信字符串只以类型/长度/哈希标识进入报告,不保留原文。
    """
    if raw is None:
        return None
    prompt_by_id = {str(r["ref_id"]): r for r in prompt_refs}
    valid_occ = 0
    invalid_occ = 0
    valid_distinct: set[str] = set()
    invalid_distinct: set[str] = set()
    reasons: Counter[str] = Counter()
    occurrences: list[str] = []

    def _invalid(token: str, reason: str) -> None:
        nonlocal invalid_occ
        invalid_occ += 1
        invalid_distinct.add(token)
        reasons[reason] += 1

    if not isinstance(raw, dict):
        token = _safe_ref_token(raw)
        occurrences.append(token)
        _invalid(token, "malformed_root")
    else:
        raw_references = raw.get("references")
        if raw_references is None:
            raw_references = []
        if not isinstance(raw_references, list):
            token = _safe_ref_token(raw_references)
            occurrences.append(token)
            _invalid(token, "references_not_a_list")
            raw_references = []
        for item in raw_references:
            if not isinstance(item, dict):
                token = _safe_ref_token(item)
                occurrences.append(token)
                _invalid(token, "malformed_reference")
                continue
            raw_ref = item.get("ref_id")
            safe_ref = _safe_ref_token(raw_ref)
            occurrences.append(safe_ref)
            prompt_ref = prompt_by_id.get(raw_ref) if isinstance(raw_ref, str) else None
            if prompt_ref is None:
                _invalid(safe_ref, "unknown_or_missing_ref_id")
                continue
            try:
                source_type = str(item.get("source_type") or "")
                source_id = int(item.get("source_id"))
                version = item.get("version")
            except (TypeError, ValueError):
                _invalid(safe_ref, "malformed_source_id")
                continue
            if (
                source_type != prompt_ref["source_type"]
                or source_id != prompt_ref["source_id"]
                or version != prompt_ref["version"]
            ):
                _invalid(safe_ref, "type_id_version_mismatch")
                continue
            valid_occ += 1
            valid_distinct.add(safe_ref)

        raw_hypotheses = raw.get("hypotheses")
        if raw_hypotheses is None:
            raw_hypotheses = []
        if not isinstance(raw_hypotheses, list):
            token = _safe_ref_token(raw_hypotheses)
            occurrences.append(token)
            _invalid(token, "hypotheses_not_a_list")
            raw_hypotheses = []
        for hyp in raw_hypotheses:
            if not isinstance(hyp, dict):
                continue
            hyp_refs = hyp.get("reference_ids")
            if hyp_refs is None:
                continue
            if not isinstance(hyp_refs, list):
                token = _safe_ref_token(hyp_refs)
                occurrences.append(token)
                _invalid(token, "hypothesis_reference_ids_not_a_list")
                continue
            for ref_id in hyp_refs:
                safe_ref = _safe_ref_token(ref_id)
                occurrences.append(safe_ref)
                if isinstance(ref_id, str) and ref_id in prompt_by_id:
                    valid_occ += 1
                    valid_distinct.add(safe_ref)
                else:
                    _invalid(safe_ref, "unknown_hypothesis_ref_id")

    total = len(occurrences)
    return {
        "observable": True,
        "occurrences": total,
        "distinct": len(set(occurrences)),
        "occurrence_refs": occurrences,
        "distinct_refs": sorted(set(occurrences)),
        "valid_occurrences": valid_occ,
        "invalid_occurrences": invalid_occ,
        "denominator_consistent": valid_occ + invalid_occ == total,
        "valid_distinct": len(valid_distinct),
        "invalid_distinct": len(invalid_distinct),
        "valid_refs": sorted(valid_distinct),
        "invalid_refs": sorted(invalid_distinct),
        "invalid_reasons": dict(reasons),
        "untrusted_occurrences": sum(
            1 for ref in occurrences if _UNTRUSTED_TOKEN_RE.fullmatch(ref)
        ),
    }


def _final_ref_summary(report: dict | None, db=None) -> dict:
    """统计最终展示引用,并用当前数据库状态/版本复核有效性。

    final 引用不是「清洗后总数即全部有效」的推断:逐条按受控 ref_id、
    来源状态(case approved / SOP published)和 SOP 版本重新核对;
    分析失败或没有最终报告时明确标记为不可观察。
    """
    if not report:
        return {
            "occurrences": 0,
            "distinct": 0,
            "occurrence_refs": [],
            "distinct_refs": [],
            "reference_details": [],
            "valid_occurrences": 0,
            "invalid_occurrences": 0,
            "validity": metric(
                0,
                0,
                definition=(
                    "最终展示引用中,按当前数据库复核为已审核案例/有效已发布 SOP "
                    "且版本匹配的引用出现次数 / 最终可观察引用出现次数"
                ),
                status_if_zero="not_applicable",
            ),
            "observable": False,
            "source_check_available": db is not None,
            "observation_reason": "分析失败或没有最终报告,最终引用不可观察",
            "permission_basis": (
                "最终引用来自按用户权限检索后的响应;此处复核来源状态/版本;"
                "无报告时权限与引用均不可观察"
            ),
            "invalid_reasons": {},
            "no_reference": None,
            "report_present": False,
        }

    fallback_map = {}
    occurrences: list[str] = []
    reference_details: list[dict] = []
    valid_occ = 0
    invalid_occ = 0
    unknown_occ = 0
    invalid_distinct: set[str] = set()
    valid_distinct: set[str] = set()
    reasons: Counter[str] = Counter()

    raw_references = report.get("references")
    if raw_references is None:
        raw_references = []
    if not isinstance(raw_references, list):
        raw_references = []
    for item in raw_references:
        if not isinstance(item, dict):
            token = _safe_ref_token(item)
            occurrences.append(token)
            invalid_occ += 1
            invalid_distinct.add(token)
            reasons["malformed_reference"] += 1
            continue
        raw_ref = item.get("ref_id")
        safe_ref = _safe_ref_token(raw_ref)
        occurrences.append(safe_ref)
        if isinstance(raw_ref, str) and _CONTROLLED_REF_RE.fullmatch(raw_ref):
            fallback_map[raw_ref] = item
        valid, reason = _final_ref_is_valid(db, safe_ref, item)
        if valid is True:
            valid_occ += 1
            valid_distinct.add(safe_ref)
        elif valid is False:
            invalid_occ += 1
            invalid_distinct.add(safe_ref)
            reasons[reason] += 1
        else:
            unknown_occ += 1
            invalid_distinct.add(safe_ref)
            reasons[reason] += 1
        reference_details.append(
            {
                "ref_id": safe_ref,
                "source_type": item.get("source_type")
                if item.get("source_type") in ("case", "sop")
                else None,
                "source_id": item.get("source_id")
                if isinstance(item.get("source_id"), int)
                else None,
                "version": item.get("version")
                if item.get("version") is None or isinstance(item.get("version"), int)
                else None,
                "valid": valid,
            }
        )

    raw_hypotheses = report.get("hypotheses")
    if raw_hypotheses is None:
        raw_hypotheses = []
    if not isinstance(raw_hypotheses, list):
        raw_hypotheses = []
    for hyp in raw_hypotheses:
        if not isinstance(hyp, dict):
            continue
        hyp_refs = hyp.get("reference_ids")
        if hyp_refs is None:
            continue
        if not isinstance(hyp_refs, list):
            token = _safe_ref_token(hyp_refs)
            occurrences.append(token)
            invalid_occ += 1
            invalid_distinct.add(token)
            reasons["hypothesis_reference_ids_not_a_list"] += 1
            continue
        for ref_id in hyp_refs:
            safe_ref = _safe_ref_token(ref_id)
            occurrences.append(safe_ref)
            fallback = fallback_map.get(safe_ref) if isinstance(safe_ref, str) else None
            valid, reason = _final_ref_is_valid(db, safe_ref, fallback)
            if valid is True:
                valid_occ += 1
                valid_distinct.add(safe_ref)
            elif valid is False:
                invalid_occ += 1
                invalid_distinct.add(safe_ref)
                reasons[reason] += 1
            else:
                unknown_occ += 1
                invalid_distinct.add(safe_ref)
                reasons[reason] += 1
            reference_details.append(
                {
                    "ref_id": safe_ref,
                    "source_type": fallback.get("source_type") if fallback else None,
                    "source_id": fallback.get("source_id") if fallback else None,
                    "version": fallback.get("version") if fallback else None,
                    "valid": valid,
                }
            )

    total = len(occurrences)
    source_check_available = db is not None
    if not source_check_available:
        validity = metric(
            0,
            0,
            definition="最终引用有效性:按当前数据库状态/版本复核",
            status_if_zero="not_applicable",
        )
    else:
        validity = metric(
            valid_occ,
            total,
            definition=(
                "最终展示引用中,按当前数据库复核为已审核案例/有效已发布 SOP "
                "且版本匹配的引用出现次数 / 最终可观察引用出现次数;"
                "分析失败/无报告不计入分母"
            ),
        )
    return {
        "occurrences": total,
        "distinct": len(set(occurrences)),
        "occurrence_refs": occurrences,
        "distinct_refs": sorted(set(occurrences)),
        "reference_details": reference_details,
        "valid_occurrences": valid_occ,
        "invalid_occurrences": invalid_occ,
        "unknown_occurrences": unknown_occ,
        "validity": validity,
        "observable": source_check_available,
        "source_check_available": source_check_available,
        "observation_reason": (
            ""
            if source_check_available
            else "没有数据库复核会话,最终引用有效性不可观察"
        ),
        "permission_basis": (
            "最终引用来自按用户权限检索后的响应;此处按 case=approved / "
            "SOP=published 且版本匹配复核来源状态;当前 Schema 无案例级额外 ACL"
        ),
        "invalid_reasons": dict(reasons),
        "valid_refs": sorted(valid_distinct),
        "invalid_refs": sorted(invalid_distinct),
        "no_reference": total == 0,
        "report_present": True,
    }


def _parse_calls(calls: list[SyntheticCall]) -> list[dict]:
    parsed = []
    for call in calls:
        raw = extract_json(call.raw_text) if call.raw_text else None
        schema_ok = False
        if raw is not None:
            try:
                AnalysisReport.model_validate(raw)
                schema_ok = True
            except ValidationError:
                schema_ok = False
        parsed.append({"call": call, "raw": raw, "schema_ok": schema_ok})
    return parsed


def _observe_references(
    parsed_calls: list[dict],
    analysis_status: str,
    report: dict | None,
    final_summary: dict | None = None,
) -> dict:
    initial = parsed_calls[0] if parsed_calls else None
    adopted = None
    if analysis_status in ("success", "incomplete") and parsed_calls:
        last = parsed_calls[-1]
        if last["schema_ok"]:
            adopted = last
    initial_summary = (
        _raw_ref_summary(initial["raw"], initial["call"].prompt_refs)
        if initial is not None
        else None
    )
    adopted_summary = (
        _raw_ref_summary(adopted["raw"], adopted["call"].prompt_refs)
        if adopted is not None
        else None
    )
    final = final_summary if final_summary is not None else _final_ref_summary(report)
    removed_distinct = []
    added_distinct = []
    raw_valid_removed = []
    raw_invalid_removed = []
    if adopted_summary is not None:
        removed_distinct = sorted(set(adopted_summary["distinct_refs"]) - set(final["distinct_refs"]))
        added_distinct = sorted(set(final["distinct_refs"]) - set(adopted_summary["distinct_refs"]))
        raw_valid_removed = sorted(set(adopted_summary["valid_refs"]) - set(final["distinct_refs"]))
        raw_invalid_removed = sorted(set(adopted_summary["invalid_refs"]) - set(final["distinct_refs"]))
    return {
        "initial": initial_summary,
        "adopted": adopted_summary,
        "final": final,
        "removed_distinct": removed_distinct,
        "added_distinct": added_distinct,
        "raw_valid_removed": raw_valid_removed,
        "raw_invalid_removed": raw_invalid_removed,
        "observable": adopted_summary is not None,
        "no_reference": (
            (adopted_summary is not None and adopted_summary["occurrences"] == 0 and final["occurrences"] == 0)
        ),
        "format_repair": {
            "calls": len(parsed_calls),
            "initial_parse_ok": bool(initial and initial["raw"] is not None),
            "initial_schema_ok": bool(initial and initial["schema_ok"]),
            "repaired_success": bool(len(parsed_calls) > 1 and adopted is not None and adopted is not initial),
            "repair_failed": bool(len(parsed_calls) > 1 and adopted is None),
        },
    }


def _classify(sample, label, analysis: dict | None, http_status: int) -> dict:
    report = (analysis or {}).get("report") or {}
    suggested = report.get("suggested_category")
    status = (analysis or {}).get("status")
    failed = status == "failed"
    http_ok = http_status == 201
    valid_category = suggested in CATEGORY_NAMES
    is_quality = "analysis" in sample.tasks
    category_correct = None
    abstain_correct = None
    reference_mismatch = None
    if is_quality and not label.abstain:
        category_correct = bool(
            http_ok and not failed and valid_category and suggested in label.allowed_categories
        )
        reference_mismatch = bool(suggested is not None and suggested not in label.allowed_categories)
    elif is_quality and label.abstain:
        abstain_correct = bool(
            http_ok
            and not failed
            and suggested is None
            and bool(report.get("insufficient_evidence"))
        )
    bucket = (
        label.expected_category
        or ("/".join(label.allowed_categories) if label.allowed_categories else "")
        or ("abstain" if label.abstain else "unknown")
    )
    return {
        "is_quality": is_quality,
        "suggested_category": suggested,
        "valid_category": valid_category,
        "failed": failed,
        "category_correct": category_correct,
        "abstain_correct": abstain_correct,
        "reference_mismatch": reference_mismatch,
        "category_bucket": bucket,
    }


def _request_row(
    client,
    headers,
    *,
    sample,
    label,
    fault_id: int,
    chat_adapter: SyntheticChatAdapter,
    use_cache: bool,
    suite: str,
    session_factory=None,
) -> dict:
    before = len(chat_adapter.calls)
    start = time.monotonic()
    resp = client.post(
        f"/api/faults/{fault_id}/analyze",
        json={"trigger_type": "manual", "use_cache": use_cache, "input": None},
        headers=headers,
    )
    elapsed_ms = (time.monotonic() - start) * 1000.0
    calls = chat_adapter.calls[before:]
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        payload = {}
    analysis = payload.get("analysis") if isinstance(payload, dict) else None
    status = analysis.get("status") if isinstance(analysis, dict) else None
    report = analysis.get("report") if isinstance(analysis, dict) else None
    parsed_calls = _parse_calls(calls)
    final_db = session_factory() if session_factory is not None else None
    try:
        final_summary = _final_ref_summary(report, final_db)
    finally:
        if final_db is not None:
            final_db.close()
    refs = _observe_references(parsed_calls, status or "", report, final_summary)
    classify = _classify(sample, label, analysis if isinstance(analysis, dict) else None, resp.status_code)
    usage = analysis.get("usage") if isinstance(analysis, dict) else None
    return {
        "suite": suite,
        "sample_id": sample.sample_id,
        "group": sample.group,
        "tasks": list(sample.tasks),
        "fault_id": fault_id,
        "http_status": resp.status_code,
        "http_ok": resp.status_code == 201,
        "business_status": status,
        "error_code": (analysis or {}).get("error_code", "") if isinstance(analysis, dict) else "",
        "error_message": ((analysis or {}).get("error_message", "") if isinstance(analysis, dict) else "")[:300],
        "analysis_id": (analysis or {}).get("id") if isinstance(analysis, dict) else None,
        "input_hash": (analysis or {}).get("input_hash", "") if isinstance(analysis, dict) else "",
        "model_config_version": (analysis or {}).get("model_config_version", "") if isinstance(analysis, dict) else "",
        "knowledge_version": (analysis or {}).get("knowledge_version", "") if isinstance(analysis, dict) else "",
        "is_demo": bool((analysis or {}).get("is_demo")) if isinstance(analysis, dict) else False,
        "reused": bool(payload.get("reused")) if isinstance(payload, dict) else False,
        "new_model_calls": len(calls),
        "historical_call_count": (analysis or {}).get("call_count", 0) if isinstance(analysis, dict) else 0,
        "usage": usage,
        "usage_status": _usage_status(usage),
        "suggested_category": classify["suggested_category"],
        "expected_category": label.expected_category,
        "allowed_categories": list(label.allowed_categories),
        "abstain_expected": label.abstain,
        "category_correct": classify["category_correct"],
        "abstain_correct": classify["abstain_correct"],
        "valid_category": classify["valid_category"],
        "category_bucket": classify["category_bucket"],
        "reference_mismatch": classify["reference_mismatch"],
        "incomplete_reasons": ((analysis or {}).get("incomplete_reasons") or [])[:8]
        if isinstance(analysis, dict)
        else [],
        "references": refs,
        "timing_ms": round(elapsed_ms, 3),
        "expected_analysis_status": label.expected_analysis_status,
        "expected_behavior": label.expected_behavior,
        "is_quality": classify["is_quality"],
    }


def _empty_reference_summary() -> dict:
    return {
        "initial": None,
        "adopted": None,
        "final": _final_ref_summary(None),
        "removed_distinct": [],
        "added_distinct": [],
        "raw_valid_removed": [],
        "raw_invalid_removed": [],
        "observable": False,
        "no_reference": False,
        "format_repair": {
            "calls": 0,
            "initial_parse_ok": False,
            "initial_schema_ok": False,
            "repaired_success": False,
            "repair_failed": False,
        },
    }


def _error_row(
    sample,
    label,
    suite: str,
    exc: Exception,
    *,
    fault_id: int | None = None,
    new_model_calls: int = 0,
    elapsed_ms: float = 0.0,
) -> dict:
    """运行中断时保留一条可审计的失败行,不从分母中悄悄删除该样本。"""
    is_quality = "analysis" in sample.tasks
    return {
        "suite": suite,
        "sample_id": sample.sample_id,
        "group": sample.group,
        "tasks": list(sample.tasks),
        "fault_id": fault_id,
        "http_status": None,
        "http_ok": False,
        "business_status": None,
        "error_code": "eval_runner_exception",
        "error_message": f"{type(exc).__name__}: {str(exc)[:300]}",
        "analysis_id": None,
        "input_hash": "",
        "model_config_version": "",
        "knowledge_version": "",
        "is_demo": False,
        "reused": False,
        "new_model_calls": new_model_calls,
        "historical_call_count": 0,
        "usage": None,
        "usage_status": "unknown",
        "suggested_category": None,
        "expected_category": label.expected_category,
        "allowed_categories": list(label.allowed_categories),
        "abstain_expected": label.abstain,
        "category_correct": (
            False if is_quality and not label.abstain else None
        ),
        "abstain_correct": (
            False if is_quality and label.abstain else None
        ),
        "valid_category": False,
        "category_bucket": (
            label.expected_category
            or ("/".join(label.allowed_categories) if label.allowed_categories else "")
            or ("abstain" if label.abstain else "unknown")
        ),
        "reference_mismatch": None,
        "incomplete_reasons": [],
        "references": _empty_reference_summary(),
        "timing_ms": round(elapsed_ms, 3),
        "expected_analysis_status": label.expected_analysis_status,
        "expected_behavior": label.expected_behavior,
        "is_quality": is_quality,
        "passed": False,
    }


def _interruption(suite: str, sample, exc: Exception) -> dict:
    return {
        "stage": suite,
        "sample_id": sample.sample_id,
        "error_type": type(exc).__name__,
        "message": str(exc)[:300],
    }


def _metrics_for_quality(rows: list[dict]) -> dict:
    non_abstain = [r for r in rows if r["is_quality"] and not r["abstain_expected"]]
    abstain = [r for r in rows if r["is_quality"] and r["abstain_expected"]]
    correct = sum(1 for r in non_abstain if r["category_correct"] is True)
    covered = sum(1 for r in non_abstain if r["valid_category"] is True)
    no_category_success = sum(
        1 for r in non_abstain if r["business_status"] in ("success", "incomplete") and r["suggested_category"] is None
    )
    failed = sum(1 for r in non_abstain if (not r["http_ok"]) or r["business_status"] == "failed")
    incomplete = sum(1 for r in non_abstain if r["business_status"] == "incomplete")
    abstain_correct = sum(1 for r in abstain if r["abstain_correct"] is True)
    mismatch_rows = [r for r in non_abstain if r["suggested_category"] is not None]
    mismatch = sum(1 for r in mismatch_rows if r["reference_mismatch"] is True)
    per_category: dict[str, dict] = {}
    for row in non_abstain:
        bucket = row["category_bucket"]
        entry = per_category.setdefault(bucket, {"planned": 0, "correct": 0})
        entry["planned"] += 1
        if row["category_correct"] is True:
            entry["correct"] += 1
    return {
        "planned": len(rows),
        "non_abstain_planned": len(non_abstain),
        "abstain_planned": len(abstain),
        "accuracy": metric(
            correct,
            len(non_abstain),
            definition="正确且有效的非空分类建议数 / 该组全部计划非弃答样本数;失败/无建议/非法分类不从分母删除",
        ),
        "suggestion_coverage": metric(
            covered,
            len(non_abstain),
            definition="有效非空分类建议数 / 计划非弃答样本数",
        ),
        "success_without_category": no_category_success,
        "failed": failed,
        "incomplete": incomplete,
        "abstain_accuracy": metric(
            abstain_correct,
            len(abstain),
            definition="应弃答样本中,成功处理且未提供分类/insufficient_evidence=true 的比例;超时/失败不算正确弃答",
        ),
        "reference_label_mismatch": metric(
            mismatch,
            len(mismatch_rows),
            definition="有非空建议且不在允许集合内的样本数 / 有非空建议的计划样本数;这是相对合成参考标签的推算,不是真人修改比例",
        ),
        "human_modification": {
            "numerator": None,
            "denominator": None,
            "value": None,
            "status": "not_available",
            "definition": "真实人工修改比例只来自可对应样本/AI 原建议/人工最终值的真人审核记录",
            "reason": "当前评测数据没有真人复核记录;预置标签/AI 自评不计为人工修改",
        },
        "per_category": dict(sorted(per_category.items())),
    }


def _reference_metrics(rows: list[dict]) -> dict:
    adopted = [r for r in rows if r["references"]["adopted"] is not None]
    initial_observable = [r for r in rows if r["references"]["initial"] is not None]
    raw_adopted_occ = sum(r["references"]["adopted"]["occurrences"] for r in adopted)
    raw_adopted_valid = sum(r["references"]["adopted"]["valid_occurrences"] for r in adopted)
    raw_adopted_invalid = sum(r["references"]["adopted"]["invalid_occurrences"] for r in adopted)
    raw_initial_occ = sum(r["references"]["initial"]["occurrences"] for r in initial_observable)
    raw_initial_valid = sum(r["references"]["initial"]["valid_occurrences"] for r in initial_observable)
    raw_initial_invalid = sum(r["references"]["initial"]["invalid_occurrences"] for r in initial_observable)
    final_observable_rows = [
        r for r in rows if r["references"]["final"].get("observable") is True
    ]
    final_occ = sum(r["references"]["final"]["occurrences"] for r in final_observable_rows)
    final_distinct = sum(r["references"]["final"]["distinct"] for r in final_observable_rows)
    final_valid = sum(
        r["references"]["final"].get("valid_occurrences", 0) for r in final_observable_rows
    )
    final_invalid = sum(
        r["references"]["final"].get("invalid_occurrences", 0) for r in final_observable_rows
    )
    final_unknown = sum(
        r["references"]["final"].get("unknown_occurrences", 0) for r in final_observable_rows
    )
    removed = sum(len(r["references"]["removed_distinct"]) for r in rows)
    added = sum(len(r["references"]["added_distinct"]) for r in rows)
    raw_valid_removed = sum(len(r["references"]["raw_valid_removed"]) for r in rows)
    raw_invalid_removed = sum(len(r["references"]["raw_invalid_removed"]) for r in rows)
    no_reference = sum(1 for r in rows if r["references"]["no_reference"] is True)
    repair_rows = [r for r in rows if r["references"]["format_repair"]["calls"] > 1]
    repaired_success = sum(1 for r in repair_rows if r["references"]["format_repair"]["repaired_success"])
    repair_failed = sum(1 for r in repair_rows if r["references"]["format_repair"]["repair_failed"])
    source_invalidated = sum(
        1
        for r in rows
        if r["expected_behavior"] == "source_invalidation" and r["references"]["raw_valid_removed"]
    )
    return {
        "planned_samples": len(rows),
        "adopted_observable_samples": len(adopted),
        "initial_observable_samples": len(initial_observable),
        "unobservable_samples": len(rows) - len(adopted),
        "raw_initial": {
            "occurrences": raw_initial_occ,
            "valid_occurrences": raw_initial_valid,
            "invalid_occurrences": raw_initial_invalid,
            "validity": metric(
                raw_initial_valid,
                raw_initial_occ,
                definition="初次模型输出(清洗前)中,按本次下发片段校验有效的引用出现次数 / 初次引用出现次数;无法解析时为不可观察",
            ),
        },
        "raw_adopted": {
            "occurrences": raw_adopted_occ,
            "valid_occurrences": raw_adopted_valid,
            "invalid_occurrences": raw_adopted_invalid,
            "validity": metric(
                raw_adopted_valid,
                raw_adopted_occ,
                definition="最终采用输出(清洗前)中,按本次下发片段校验有效的引用出现次数 / 采用输出引用出现次数;0 引用为 not_applicable",
            ),
            "invalid_reasons": dict(
                sum(
                    (Counter(r["references"]["adopted"]["invalid_reasons"]) for r in adopted),
                    Counter(),
                )
            ),
        },
        "final": {
            "observable_samples": len(final_observable_rows),
            "unobservable_samples": len(rows) - len(final_observable_rows),
            "occurrences": final_occ,
            "distinct": final_distinct,
            "valid_occurrences": final_valid,
            "invalid_occurrences": final_invalid,
            "unknown_occurrences": final_unknown,
            "validity": metric(
                final_valid,
                final_occ,
                definition=(
                    "最终展示引用中,按当前数据库复核为已审核案例/有效已发布 SOP "
                    "且版本匹配的引用出现次数 / 最终可观察引用出现次数;"
                    "分析失败/无报告不计入分母"
                ),
            ),
            "permission_basis": (
                "最终引用来自按用户权限检索后的响应;此处按 case=approved / "
                "SOP=published 且版本匹配复核来源状态;当前 Schema 无案例级额外 ACL"
            ),
            "removed_distinct": removed,
            "added_distinct": added,
            "raw_valid_removed": raw_valid_removed,
            "raw_invalid_removed": raw_invalid_removed,
            "no_reference_samples": no_reference,
        },
        "format_repair": {
            "samples": len(repair_rows),
            "repaired_success": repaired_success,
            "repair_failed": repair_failed,
        },
        "source_invalidated_during_call_samples": source_invalidated,
        "notes": [
            "原始引用声明在模型输出边界观察,不是从清洗后的最终引用倒推。",
            "有效性与内容是否真正支持结论是不同问题;本指标只衡量结构/来源/版本/下发范围。",
            "无法观察原始声明的样本计入 unobservable_samples,不当作 100% 有效。",
        ],
    }


def _cache_source_probe(
    client,
    owner_headers,
    sample,
    label,
    fault_id: int,
    chat_adapter: SyntheticChatAdapter,
    session_factory,
    first_row: dict,
) -> dict:
    """在缓存建立后废弃其 SOP 来源,再通过真实分析 API 验证结果。

    不是直接调用 revalidate_report_sources helper:必须走业务路由,观察
    真实缓存策略是「命中后复核」还是「缓存失效后重新分析」,并准确统计
    新增模型调用。没有有效 SOP 引用时标记不适用/跳过,不当作通过。
    """
    final = (first_row.get("references") or {}).get("final") or {}
    details = final.get("reference_details") or []
    sop_details = [
        d
        for d in details
        if d.get("source_type") == "sop"
        and isinstance(d.get("source_id"), int)
        and d.get("valid") is True
    ]
    if not sop_details:
        return {
            "status": "not_applicable",
            "applicable": False,
            "passed": None,
            "sample_id": sample.sample_id,
            "reason": "缓存样本最终无有效 SOP 引用,缓存来源复核前置条件未满足;不计为通过",
            "api_requests_during_source_probe": 0,
        }
    target = sop_details[0]
    sop_id = int(target["source_id"])
    sop_ref_id = str(target.get("ref_id") or "")

    db = session_factory()
    original = None
    try:
        sop = db.get(Sop, sop_id)
        if sop is None:
            return {
                "status": "not_applicable",
                "applicable": False,
                "passed": None,
                "sample_id": sample.sample_id,
                "reason": "缓存样本引用的 SOP 不存在,前置条件未满足;不计为通过",
                "api_requests_during_source_probe": 0,
            }
        original = sop.status
        sop.status = SopStatus.deprecated
        db.commit()
    finally:
        db.close()

    before = len(chat_adapter.calls)
    start = time.monotonic()
    probe_row = None
    probe_error = None
    try:
        probe_row = _request_row(
            client,
            owner_headers,
            sample=sample,
            label=label,
            fault_id=fault_id,
            chat_adapter=chat_adapter,
            use_cache=True,
            suite="cache_probe",
            session_factory=session_factory,
        )
    except Exception as exc:  # noqa: BLE001
        probe_error = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        # 恢复来源状态;即使探测请求异常也必须在 finally 中恢复
        db = session_factory()
        try:
            sop = db.get(Sop, sop_id)
            if sop is not None and original is not None:
                sop.status = original
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        finally:
            db.close()

    new_model_calls = len(chat_adapter.calls) - before
    if probe_row is None:
        return {
            "status": "error",
            "applicable": True,
            "passed": False,
            "sample_id": sample.sample_id,
            "sop_ref_id": sop_ref_id,
            "api_requests_during_source_probe": 1,
            "new_model_calls": new_model_calls,
            "timing_ms": round(elapsed_ms, 3),
            "reason": f"来源变化后的业务 API 请求异常: {probe_error}",
        }

    final_after = (probe_row.get("references") or {}).get("final") or {}
    after_details = final_after.get("reference_details") or []
    sop_absent = not any(
        d.get("ref_id") == sop_ref_id
        or (
            d.get("source_type") == "sop"
            and d.get("source_id") == sop_id
        )
        for d in after_details
    )
    reused = bool(probe_row.get("reused"))
    path = (
        "cache_hit_revalidated"
        if reused
        else "cache_invalidated_reanalysis"
        if new_model_calls > 0
        else "cache_dropped_without_call"
    )
    passed = bool(
        probe_row.get("http_ok")
        and sop_absent
        and (reused or new_model_calls > 0)
    )
    return {
        "status": "available" if passed else "failed",
        "applicable": True,
        "passed": passed,
        "sample_id": sample.sample_id,
        "sop_ref_id": sop_ref_id,
        "sop_source_id": sop_id,
        "api_requests_during_source_probe": 1,
        "http_status": probe_row.get("http_status"),
        "business_status": probe_row.get("business_status"),
        "reused": reused,
        "path": path,
        "new_model_calls": new_model_calls,
        "invalid_reference_absent": sop_absent,
        "final_reference_details": after_details[:10],
        "incomplete_reasons": (probe_row.get("incomplete_reasons") or [])[:5],
        "timing_ms": round(elapsed_ms, 3),
        "reason": (
            "来源失效后业务 API 未再返回该引用"
            if passed
            else "来源失效后业务 API 仍返回该引用,或没有命中/重新分析"
        ),
    }


def run_analysis(
    client,
    owner_headers,
    viewer_headers,
    dataset: Dataset,
    fixtures,
    chat_adapter: SyntheticChatAdapter,
    session_factory,
    *,
    limit: int = 0,
) -> AnalysisRun:
    main_samples = dataset.samples_for("analysis")
    failure_samples = dataset.samples_for("analysis_failure")
    cache_samples = dataset.samples_for("analysis_cache")
    permission_samples = dataset.samples_for("analysis_permission")
    if limit > 0:
        main_samples = main_samples[:limit]
        failure_samples = failure_samples[: max(0, limit - len(main_samples))]
        cache_samples = cache_samples[: max(0, limit - len(main_samples) - len(failure_samples))]
        permission_samples = permission_samples[: max(0, limit - len(main_samples) - len(failure_samples) - len(cache_samples))]

    rows: list[dict] = []
    before_users = None
    before_audit = None
    db = session_factory()
    try:
        before_users = db.scalar(select(func.count()).select_from(User))
        before_audit = db.scalar(select(func.count()).select_from(AuditEvent))
    finally:
        db.close()

    interrupted: dict | None = None
    cache_probes: list[dict] = []

    # 正常质量 suite:关闭缓存,避免主效果样本污染
    if interrupted is None:
        for sample in main_samples:
            label = dataset.labels[sample.sample_id]
            fault_id = int(fixtures.analysis_faults[sample.sample_id])
            calls_before = len(chat_adapter.calls)
            start = time.monotonic()
            try:
                row = _request_row(
                    client,
                    owner_headers,
                    sample=sample,
                    label=label,
                    fault_id=fault_id,
                    chat_adapter=chat_adapter,
                    use_cache=False,
                    suite="main",
                    session_factory=session_factory,
                )
            except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
                rows.append(
                    _error_row(
                        sample,
                        label,
                        "main",
                        exc,
                        fault_id=fault_id,
                        new_model_calls=len(chat_adapter.calls) - calls_before,
                        elapsed_ms=(time.monotonic() - start) * 1000.0,
                    )
                )
                interrupted = _interruption("main", sample, exc)
                break
            rows.append(row)

    # 失败注入 suite:同一真实路由,不改产品重试规则
    invalidation_state = {"sample": None, "sop_id": None, "original": None}

    def _restore_invalidation_source() -> None:
        if invalidation_state["sop_id"] is None:
            return
        db2 = session_factory()
        try:
            sop = db2.get(Sop, invalidation_state["sop_id"])
            if sop is not None and invalidation_state["original"] is not None:
                sop.status = invalidation_state["original"]
                db2.commit()
        finally:
            db2.close()
            invalidation_state.update({"sample": None, "sop_id": None, "original": None})

    def invalidate_once(sample_id, prompt_refs):
        if sample_id != "S037":
            return
        for ref in prompt_refs:
            if ref.get("source_type") == "sop":
                db2 = session_factory()
                try:
                    sop = db2.get(Sop, int(ref["source_id"]))
                    if sop is not None:
                        invalidation_state.update(
                            {"sample": sample_id, "sop_id": sop.id, "original": sop.status}
                        )
                        sop.status = SopStatus.deprecated
                        db2.commit()
                finally:
                    db2.close()
                break

    if interrupted is None:
        chat_adapter.before_return = invalidate_once
        try:
            for sample in failure_samples:
                label = dataset.labels[sample.sample_id]
                fault_id = int(fixtures.analysis_faults[sample.sample_id])
                calls_before = len(chat_adapter.calls)
                start = time.monotonic()
                try:
                    row = _request_row(
                        client,
                        owner_headers,
                        sample=sample,
                        label=label,
                        fault_id=fault_id,
                        chat_adapter=chat_adapter,
                        use_cache=False,
                        suite="failure",
                        session_factory=session_factory,
                    )
                except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
                    rows.append(
                        _error_row(
                            sample,
                            label,
                            "failure",
                            exc,
                            fault_id=fault_id,
                            new_model_calls=len(chat_adapter.calls) - calls_before,
                            elapsed_ms=(time.monotonic() - start) * 1000.0,
                        )
                    )
                    interrupted = _interruption("failure", sample, exc)
                    break
                rows.append(row)
                if invalidation_state["sop_id"] is not None and sample.sample_id == "S037":
                    _restore_invalidation_source()
        finally:
            chat_adapter.before_return = None
            # 即使请求异常,也必须恢复调用期间被临时废弃的来源
            try:
                _restore_invalidation_source()
            except Exception:  # noqa: BLE001
                pass

    # 缓存 suite:第一次产生缓存,第二次必须复用且新增调用为 0
    cache_rows: list[dict] = []
    if interrupted is None:
        for sample in cache_samples:
            label = dataset.labels[sample.sample_id]
            fault_id = int(fixtures.analysis_faults[sample.sample_id])
            first = None
            try:
                calls_before = len(chat_adapter.calls)
                start = time.monotonic()
                first = _request_row(
                    client,
                    owner_headers,
                    sample=sample,
                    label=label,
                    fault_id=fault_id,
                    chat_adapter=chat_adapter,
                    use_cache=True,
                    suite="cache",
                    session_factory=session_factory,
                )
                second = _request_row(
                    client,
                    owner_headers,
                    sample=sample,
                    label=label,
                    fault_id=fault_id,
                    chat_adapter=chat_adapter,
                    use_cache=True,
                    suite="cache",
                    session_factory=session_factory,
                )
            except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
                if first is not None:
                    cache_rows.append(first)
                    rows.append(first)
                cache_rows.append(
                    _error_row(
                        sample,
                        label,
                        "cache",
                        exc,
                        fault_id=fault_id,
                        new_model_calls=len(chat_adapter.calls) - calls_before,
                        elapsed_ms=(time.monotonic() - start) * 1000.0,
                    )
                )
                rows.append(cache_rows[-1])
                interrupted = _interruption("cache", sample, exc)
                break
            cache_rows.extend([first, second])
            rows.extend([first, second])
            try:
                probe = _cache_source_probe(
                    client,
                    owner_headers,
                    sample,
                    label,
                    fault_id,
                    chat_adapter,
                    session_factory,
                    first,
                )
            except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
                probe = {
                    "status": "error",
                    "applicable": True,
                    "passed": False,
                    "sample_id": sample.sample_id,
                    "api_requests_during_source_probe": 0,
                    "new_model_calls": 0,
                    "reason": f"缓存来源探测异常: {type(exc).__name__}: {str(exc)[:300]}",
                }
                if isinstance(exc, KeyboardInterrupt):
                    cache_probes.append(probe)
                    interrupted = _interruption("cache_probe", sample, exc)
                    break
            cache_probes.append(probe)

    # 分析权限 suite:查看者分析所有者故障必须 403,不得调用模型
    permission_rows: list[dict] = []
    if interrupted is None:
        for sample in permission_samples:
            label = dataset.labels[sample.sample_id]
            fault_id = int(fixtures.analysis_faults[sample.sample_id])
            before = len(chat_adapter.calls)
            start = time.monotonic()
            try:
                resp = client.post(
                    f"/api/faults/{fault_id}/analyze",
                    json={"trigger_type": "manual", "use_cache": False, "input": None},
                    headers=viewer_headers,
                )
            except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
                permission_rows.append(
                    _error_row(
                        sample,
                        label,
                        "permission",
                        exc,
                        fault_id=fault_id,
                        new_model_calls=len(chat_adapter.calls) - before,
                        elapsed_ms=(time.monotonic() - start) * 1000.0,
                    )
                )
                interrupted = _interruption("permission", sample, exc)
                break
            elapsed = (time.monotonic() - start) * 1000.0
            permission_rows.append(
                {
                    "suite": "permission",
                    "sample_id": sample.sample_id,
                    "group": sample.group,
                    "tasks": list(sample.tasks),
                    "expected": label.expected_permission,
                    "http_status": resp.status_code,
                    "http_ok": resp.status_code == 201,
                    "business_status": None,
                    "error_code": "",
                    "new_model_calls": len(chat_adapter.calls) - before,
                    "historical_call_count": 0,
                    "usage": None,
                    "usage_status": "unknown",
                    "timing_ms": round(elapsed, 3),
                    "passed": resp.status_code == 403 and len(chat_adapter.calls) == before,
                }
            )

    rows.extend(permission_rows)

    # 注入副作用检查
    db = session_factory()
    try:
        after_users = db.scalar(select(func.count()).select_from(User))
        after_audit = db.scalar(select(func.count()).select_from(AuditEvent))
        command_audit = db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action.in_(["command", "sql", "shell", "exec"]))
        )
    finally:
        db.close()
    injection_ok = before_users == after_users and before_audit == after_audit and command_audit == 0

    main_rows = [r for r in rows if r["suite"] == "main"]
    failure_rows = [r for r in rows if r["suite"] == "failure"]
    main_metrics = _metrics_for_quality(main_rows)
    main_refs = _reference_metrics(main_rows)
    failure_refs = _reference_metrics(failure_rows)

    # 失败 suite 机制断言
    by_id = {r["sample_id"]: r for r in failure_rows}
    assertions = []

    def _assert(name: str, passed: bool, detail="", applicable: bool = True) -> None:
        if not applicable:
            assertions.append(
                {"name": name, "passed": None, "status": "skipped", "detail": "本轮未执行该样本/套件"}
            )
            return
        assertions.append(
            {"name": name, "passed": bool(passed), "status": "passed" if passed else "failed", "detail": detail}
        )

    _assert(
        "timeout: 状态 failed 且 error_code=timeout 且只调用一次",
        by_id.get("S030", {}).get("business_status") == "failed"
        and by_id.get("S030", {}).get("error_code") == "timeout"
        and by_id.get("S030", {}).get("new_model_calls") == 1,
        "S030 status={} error={} calls={}".format(
            by_id.get("S030", {}).get("business_status"),
            by_id.get("S030", {}).get("error_code"),
            by_id.get("S030", {}).get("new_model_calls"),
        ),
        applicable="S030" in by_id,
    )
    _assert(
        "rate_limited: 状态 failed 且 error_code=rate_limited 且只调用一次",
        by_id.get("S031", {}).get("business_status") == "failed"
        and by_id.get("S031", {}).get("error_code") == "rate_limited"
        and by_id.get("S031", {}).get("new_model_calls") == 1,
        "S031 status={} error={} calls={}".format(
            by_id.get("S031", {}).get("business_status"),
            by_id.get("S031", {}).get("error_code"),
            by_id.get("S031", {}).get("new_model_calls"),
        ),
        applicable="S031" in by_id,
    )
    _assert(
        "bad_json: 状态 failed 且有限修复调用两次",
        by_id.get("S032", {}).get("business_status") == "failed"
        and by_id.get("S032", {}).get("new_model_calls") == 2,
        "S032 status={} calls={}".format(
            by_id.get("S032", {}).get("business_status"),
            by_id.get("S032", {}).get("new_model_calls"),
        ),
        applicable="S032" in by_id,
    )
    _assert(
        "schema_bad: 状态 failed 且有限修复调用两次",
        by_id.get("S033", {}).get("business_status") == "failed"
        and by_id.get("S033", {}).get("new_model_calls") == 2,
        "S033 status={} calls={}".format(
            by_id.get("S033", {}).get("business_status"),
            by_id.get("S033", {}).get("new_model_calls"),
        ),
        applicable="S033" in by_id,
    )
    _assert(
        "schema_repair: 初次/修复输出分开标记且修复成功",
        by_id.get("S034", {}).get("references", {}).get("format_repair", {}).get("repaired_success") is True,
        "S034 repair={} calls={}".format(
            by_id.get("S034", {}).get("references", {}).get("format_repair", {}).get("repaired_success"),
            by_id.get("S034", {}).get("new_model_calls"),
        ),
        applicable="S034" in by_id,
    )
    _assert(
        "invalid_reference: 原始无效引用被清洗且最终更少",
        by_id.get("S035", {}).get("business_status") == "incomplete"
        and (by_id.get("S035", {}).get("references", {}).get("adopted") or {}).get("invalid_occurrences", 0) > 0
        and len(by_id.get("S035", {}).get("references", {}).get("raw_invalid_removed", [])) > 0,
        "S035 status={} raw_invalid={} removed={}".format(
            by_id.get("S035", {}).get("business_status"),
            (by_id.get("S035", {}).get("references", {}).get("adopted") or {}).get("invalid_occurrences"),
            len(by_id.get("S035", {}).get("references", {}).get("raw_invalid_removed", [])),
        ),
        applicable="S035" in by_id,
    )
    _assert(
        "partial_usage: 不补造缺失 usage 字段",
        by_id.get("S036", {}).get("usage_status") == "partial"
        and by_id.get("S036", {}).get("usage") is not None
        and "total_tokens" not in by_id.get("S036", {}).get("usage"),
        "S036 usage={} usage_status={}".format(
            by_id.get("S036", {}).get("usage"),
            by_id.get("S036", {}).get("usage_status"),
        ),
        applicable="S036" in by_id,
    )
    _assert(
        "source_invalidation: 调用期间失效来源被剔除并标 incomplete",
        by_id.get("S037", {}).get("business_status") == "incomplete"
        and bool(by_id.get("S037", {}).get("references", {}).get("raw_valid_removed")),
        "S037 status={} raw_valid_removed={}".format(
            by_id.get("S037", {}).get("business_status"),
            by_id.get("S037", {}).get("references", {}).get("raw_valid_removed"),
        ),
        applicable="S037" in by_id,
    )
    cache_reuse_ok = bool(
        len(cache_rows) >= 2
        and cache_rows[0].get("reused") is False
        and cache_rows[1].get("reused") is True
        and cache_rows[1].get("new_model_calls") == 0
        and cache_rows[0].get("analysis_id") is not None
        and cache_rows[0].get("analysis_id") == cache_rows[1].get("analysis_id")
    )
    _assert(
        "cache: 第二次复用且新增模型调用为 0",
        cache_reuse_ok,
        "reused={} new_calls={} same_analysis={}".format(
            [r.get("reused") for r in cache_rows],
            [r.get("new_model_calls") for r in cache_rows],
            bool(
                len(cache_rows) >= 2
                and cache_rows[0].get("analysis_id") is not None
                and cache_rows[0].get("analysis_id") == cache_rows[1].get("analysis_id")
            ),
        ),
        applicable=bool(cache_rows),
    )
    if cache_probes:
        for probe in cache_probes:
            _assert(
                f"cache[{probe.get('sample_id')}]: 来源变化后业务 API 不返回失效引用",
                probe.get("passed") is True,
                "status={} path={} api_requests={} new_calls={} sop_absent={}".format(
                    probe.get("status"),
                    probe.get("path"),
                    probe.get("api_requests_during_source_probe"),
                    probe.get("new_model_calls"),
                    probe.get("invalid_reference_absent"),
                ),
                applicable=bool(probe.get("applicable")),
            )
    else:
        _assert(
            "cache: 来源变化后业务 API 不返回失效引用",
            False,
            "未执行缓存来源探测(缓存 suite 未运行或中断);不计为通过",
            applicable=False,
        )
    _assert(
        "permission: 无权分析 403 且零模型调用",
        all(r["passed"] for r in permission_rows) if permission_rows else False,
        "http_statuses={} new_calls={}".format(
            [r.get("http_status") for r in permission_rows],
            [r.get("new_model_calls") for r in permission_rows],
        ),
        applicable=bool(permission_rows),
    )
    _assert(
        "injection: 注入文本无用户/审计命令副作用",
        injection_ok,
        "users_unchanged={} audit_unchanged={} command_audit={}".format(
            before_users == after_users,
            before_audit == after_audit,
            command_audit,
        ),
    )

    classification = {
        "main": main_metrics,
        "groups": {
            group: _metrics_for_quality([r for r in main_rows if r["group"] == group])
            for group in sorted({r["group"] for r in main_rows})
        },
    }
    references = {
        "main": main_refs,
        "failure": failure_refs,
        "cache": _reference_metrics(cache_rows),
    }
    timing = {
        "method": "in-process TestClient;不含迁移/夹具/索引构建",
        "main": timing_summary([r["timing_ms"] for r in main_rows]),
        "failure": timing_summary([r["timing_ms"] for r in failure_rows]),
        "cache": timing_summary([r["timing_ms"] for r in cache_rows]),
        "permission": timing_summary([r["timing_ms"] for r in permission_rows]),
        "note": "这是进程内 API 耗时,不能冒充生产网络端到端性能;失败注入的延迟是合成行为",
    }
    failures = {
        "main": {
            "planned": len(main_samples),
            "executed": len(main_rows),
            "http_failures": sum(1 for r in main_rows if not r["http_ok"]),
            "business_failed": sum(1 for r in main_rows if r["business_status"] == "failed"),
            "incomplete": sum(1 for r in main_rows if r["business_status"] == "incomplete"),
            "success": sum(1 for r in main_rows if r["business_status"] == "success"),
            "failure_rate": metric(
                sum(1 for r in main_rows if (not r["http_ok"]) or r["business_status"] == "failed"),
                len(main_samples),
                definition="(非预期 HTTP 错误 + HTTP 成功但 analysis.status=failed) / 计划样本数;同一请求不重复计数",
            ),
        },
        "failure_suite": {
            "planned": len(failure_samples),
            "executed": len(failure_rows),
            "expected_vs_actual": [
                {
                    "sample_id": r["sample_id"],
                    "expected": r["expected_analysis_status"],
                    "actual": r["business_status"],
                    "passed": r["expected_analysis_status"] == r["business_status"],
                }
                for r in failure_rows
            ],
        },
        "cache": {
            "planned_requests": len(cache_samples) * 2,
            "executed_requests": len(cache_rows),
            "reused": sum(1 for r in cache_rows if r.get("reused")),
            "new_model_calls": sum(r.get("new_model_calls", 0) for r in cache_rows),
            "historical_call_count_sum": sum(r.get("historical_call_count", 0) for r in cache_rows),
            "source_probe_requests": sum(
                p.get("api_requests_during_source_probe", 0) for p in cache_probes
            ),
            "source_probe_new_model_calls": sum(
                p.get("new_model_calls", 0) for p in cache_probes
            ),
        },
        "permission": permission_rows,
    }
    probe_calls = sum(p.get("new_model_calls", 0) for p in cache_probes)
    usage = {
        "chat_calls_new": sum(r.get("new_model_calls", 0) for r in rows) + probe_calls,
        "chat_calls_by_suite": {
            "main": sum(r.get("new_model_calls", 0) for r in main_rows),
            "failure": sum(r.get("new_model_calls", 0) for r in failure_rows),
            "cache": sum(r.get("new_model_calls", 0) for r in cache_rows),
            "cache_probe": probe_calls,
            "permission": sum(r.get("new_model_calls", 0) for r in permission_rows),
        },
        "historical_call_count_sum": sum(r.get("historical_call_count", 0) for r in rows),
        "usage_status_counts": dict(Counter(r.get("usage_status", "unknown") for r in rows)),
        "real_chat_calls": 0,
        "real_embedding_calls": 0,
        "note": "合成适配器调用与真实调用分开;usage 缺失保持未知,部分字段不补造总量;不计算费用",
    }
    if len(cache_probes) == 1:
        cache_probe = cache_probes[0]
    elif cache_probes:
        cache_probe = {
            "status": "multiple",
            "applicable": True,
            "probes": cache_probes,
        }
    else:
        cache_probe = {
            "status": "not_applicable",
            "applicable": False,
            "passed": None,
            "api_requests_during_source_probe": 0,
            "reason": "没有缓存样本或缓存 suite 未执行;不当作通过",
        }
    passed_assertions = sum(1 for a in assertions if a.get("passed") is True)
    failed_assertions = [a for a in assertions if a.get("passed") is False]
    skipped_assertions = [a for a in assertions if a.get("status") == "skipped"]
    planned_counts = {
        "main": len(main_samples),
        "failure": len(failure_samples),
        "cache": len(cache_samples) * 2,
        "permission": len(permission_samples),
    }
    executed_counts = {
        "main": len(main_rows),
        "failure": len(failure_rows),
        "cache": len(cache_rows),
        "permission": len(permission_rows),
    }
    unexecuted_counts = {
        key: max(0, planned_counts[key] - executed_counts[key]) for key in planned_counts
    }
    full_analysis_planned = (
        len(dataset.samples_for("analysis"))
        + len(dataset.samples_for("analysis_failure"))
        + len(dataset.samples_for("analysis_cache")) * 2
        + len(dataset.samples_for("analysis_permission"))
    )
    limit_restricted = (
        limit > 0
        and interrupted is None
        and sum(executed_counts.values()) < full_analysis_planned
    )
    complete = interrupted is None and not limit_restricted
    cancelled = bool(
        interrupted is not None
        and interrupted.get("error_type") == "KeyboardInterrupt"
    )
    report = {
        "suite": "analysis",
        "complete": complete,
        "cancelled": cancelled,
        "complete_reason": (
            "cancelled"
            if cancelled
            else "interrupted"
            if interrupted is not None
            else "limit"
            if limit_restricted
            else "full"
        ),
        "interrupted": interrupted,
        "interrupted_stage": interrupted.get("stage") if interrupted else None,
        "interrupted_sample": interrupted.get("sample_id") if interrupted else None,
        "unexecuted_reason": (
            f"运行在 {interrupted['stage']}/{interrupted['sample_id']} "
            f"{'被用户取消' if cancelled else '中断'}:"
            f"{interrupted['error_type']};后续样本未执行"
            if interrupted
            else f"--limit={limit} 仅执行 {sum(executed_counts.values())}/"
            f"{full_analysis_planned} 个数据集请求;剩余未执行,不是完整评测"
            if limit_restricted
            else ""
        ),
        "dataset_planned_requests": full_analysis_planned,
        "planned": planned_counts,
        "executed": executed_counts,
        "unexecuted": unexecuted_counts,
        "limit": limit,
        "classification": classification,
        "references": references,
        "timing": timing,
        "failures": failures,
        "usage": usage,
        "cache_probe": cache_probe,
        "cache_probes": cache_probes,
        "injection": {
            "samples": [r["sample_id"] for r in main_rows if r["group"] == "injection"],
            "users_unchanged": before_users == after_users,
            "audit_unchanged": before_audit == after_audit,
            "command_audit_count": command_audit,
            "passed": injection_ok,
        },
        "mechanism_assertions": assertions,
        "assertion_counts": {
            "passed": passed_assertions,
            "failed": len(failed_assertions),
            "skipped": len(skipped_assertions),
            "total": len(assertions),
        },
        "cases": [
            {k: v for k, v in row.items() if k not in ("_report", "final_reference_details")}
            for row in rows
        ],
        "notes": [
            "合成适配器只验证路由/校验/引用/指标机制,不代表真实模型分类质量。",
            "参考标签为开发 AI 编写的 provisional 合成标签,不是真人 gold。",
            "真实人工修改比例不可用;没有真人复核记录。",
            "时间均为进程内 TestClient 耗时,不是生产网络端到端性能。",
            "运行中断/取消时保留已完成行和未执行计数;complete=false 时不是完整效果评测。",
            "机制断言 skipped 不计为 passed;失败或普通中断时退出码为 4,用户取消为 130。",
        ],
    }
    if interrupted is not None:
        exit_code = 130 if cancelled else 4
    else:
        exit_code = 4 if failed_assertions else 0
    return AnalysisRun(report=report, exit_code=exit_code)
