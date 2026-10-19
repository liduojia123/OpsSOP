"""AI 生成 SOP 草稿编排(第三阶段第一批):案例 → 可信输入快照 → 模型 → 校验 → draft。

关键行为(AGENTS.md「业务可靠性」/ 第三阶段第一批要求):
- 仅以指定已审核案例为生成依据,不额外检索其他知识;输入为可信来源快照
  (人工确认根因/处理/恢复验证 + 必要故障证据),不发送整份故障历史;
- 调用前校验来源(已通过 + 生成权限),返回后、创建草稿前重新读取来源状态与
  输入快照(列级 select,绕过会话 identity map);来源已变化不得落成有效草稿;
- doc_id/version/status/source_case_id/创建者/维护人由后端决定,模型不可指定;
- JSON 语法/结构错误共享一次修复预算(共用 app.ai.json_out),usage/call_count
  逐次累计,缺失保持未知;失败只记安全元信息,不创建空白或半成品 SOP;
- request_token 幂等:同一次请求重复提交/重试不重复创建草稿、不重复计费;
  用户主动再次生成(新 token)可创建另一份新草稿;
- demo 显式模拟并标注;disabled 明确不可用;真实失败不回退演示或模板结果。
"""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai import service as ai_service
from app.ai.json_out import complete_json
from app.ai.llm import ChatMessage, DemoAdapter, build_adapter
from app.ai.sanitize import sanitize_text
from app.ai.schema import EvidenceItem
from app.ai.sop_prompts import SOP_SYSTEM_PROMPT, SOP_USER_TEMPLATE
from app.ai.sop_schema import (
    CONTENT_FIELDS,
    KEY_SOURCE_FIELDS,
    PENDING_MARKERS,
    SopDraftContent,
    SopFieldSource,
)
from app.config import Settings
from app.deps import record_audit
from app.models import (
    Case,
    CaseStatus,
    Category,
    Fault,
    Role,
    Sop,
    SopAiGenerationRun,
    SopGenStatus,
    SopStatus,
    User,
    utcnow,
)

# 每个证据片段进入提示词前的单条上限(先脱敏后截断)
MAX_EVIDENCE_CHARS = 3000

# 进程崩溃遗留的 running 记录:超过此时长后允许同 token 接管重试(防永久卡死)
STALE_RUNNING_SECONDS = 15 * 60


class SourceRejected(Exception):
    """来源不可用(案例不存在/未通过/状态已变化):不调用模型、不落生成记录。"""


class TokenConflict(Exception):
    """幂等冲突:同一请求正在生成中,或 request_token 与既有请求不匹配。"""


class AiUnavailable(Exception):
    """AI 生成明确不可用(AI_MODE=disabled);模板生成与手工编辑不受影响。"""


@dataclass
class SopGenInput:
    """可信输入快照:来源案例的人工确认内容 + 必要故障证据(不含整份故障历史)。"""

    case_id: int
    fault_id: int
    created_by: int
    status: CaseStatus
    title: str = ""
    symptom: str = ""
    environment: str = ""
    service: str = ""
    component_version: str = ""
    category_id: int | None = None
    category_name: str = ""
    tags: list[str] = field(default_factory=list)
    confirmed_root_cause: str = ""
    handling_steps: str = ""
    recovery_verification: str = ""
    evidence: list[EvidenceItem] = field(default_factory=list)


@dataclass
class SopGenOutcome:
    """一次生成的结果;content 为 None 时按 error_code/error_message 失败。"""

    content: SopDraftContent | None
    incomplete_reasons: list[str] = field(default_factory=list)
    error_code: str = ""
    error_message: str = ""
    usage: dict | None = None
    call_count: int = 0
    latency_ms: int = 0
    is_demo: bool = False


@dataclass
class SopGenResult:
    run: SopAiGenerationRun
    sop: Sop | None
    reused: bool  # 幂等重复返回(同 request_token 的成功结果),未重复计费


def _clip(text: str, limit: int) -> str:
    """先脱敏后截断(与分析提示词同一契约:截断不得绕过打码)。"""
    return sanitize_text(text)[:limit]


def load_sop_gen_input(db: Session, case_id: int) -> SopGenInput | None:
    """读取生成输入快照:列级 select 直读数据库(模型调用期间的外部变更必须可见)。"""
    row = db.execute(
        select(
            Case.id,
            Case.fault_id,
            Case.created_by,
            Case.status,
            Case.title,
            Case.symptom,
            Case.environment,
            Case.service,
            Case.component_version,
            Case.category_id,
            Case.tags,
            Case.confirmed_root_cause,
            Case.handling_steps,
            Case.recovery_verification,
            Fault.description,
            Fault.error_logs,
            Fault.recent_changes,
        )
        .join(Fault, Fault.id == Case.fault_id)
        .where(Case.id == case_id)
    ).first()
    if row is None:
        return None

    evidence: list[EvidenceItem] = []
    env_text = "\n".join(
        line
        for line in [
            f"环境: {row.environment}" if row.environment else "",
            f"服务: {row.service}" if row.service else "",
            f"组件版本: {row.component_version}" if row.component_version else "",
        ]
        if line
    )
    for eid, source, text in [
        ("case.applicable_env", "case:applicable_env", env_text),
        ("case.symptom", "case:symptom", row.symptom),
        ("case.confirmed_root_cause", "case:confirmed_root_cause", row.confirmed_root_cause),
        ("case.handling_steps", "case:handling_steps", row.handling_steps),
        ("case.recovery_verification", "case:recovery_verification", row.recovery_verification),
        ("fault.description", "fault:description", row.description),
        ("fault.error_logs", "fault:error_logs", row.error_logs),
        ("fault.recent_changes", "fault:recent_changes", row.recent_changes),
    ]:
        if text and text.strip():
            evidence.append(EvidenceItem(evidence_id=eid, source=source, text=text.strip()))

    category_name = ""
    if row.category_id is not None:
        name = db.execute(select(Category.name).where(Category.id == row.category_id)).scalar_one_or_none()
        category_name = name or ""

    return SopGenInput(
        case_id=row.id,
        fault_id=row.fault_id,
        created_by=row.created_by,
        status=row.status,
        title=row.title or "",
        symptom=row.symptom or "",
        environment=row.environment or "",
        service=row.service or "",
        component_version=row.component_version or "",
        category_id=row.category_id,
        category_name=category_name,
        tags=list(row.tags or []),
        confirmed_root_cause=row.confirmed_root_cause or "",
        handling_steps=row.handling_steps or "",
        recovery_verification=row.recovery_verification or "",
        evidence=evidence,
    )


def compute_input_hash(inp: SopGenInput) -> str:
    """输入快照哈希:来源内容任一变化即变(返回后复核依据)。"""
    payload = {
        "case_id": inp.case_id,
        "title": inp.title,
        "symptom": inp.symptom,
        "environment": inp.environment,
        "service": inp.service,
        "component_version": inp.component_version,
        "category_id": inp.category_id,
        "tags": list(inp.tags or []),
        "confirmed_root_cause": inp.confirmed_root_cause,
        "handling_steps": inp.handling_steps,
        "recovery_verification": inp.recovery_verification,
        "evidence": [[e.evidence_id, e.text] for e in inp.evidence],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_user_prompt(inp: SopGenInput) -> str:
    evidence_block = "\n".join(
        f"[{e.evidence_id}] ({e.source}) {_clip(e.text, MAX_EVIDENCE_CHARS)}" for e in inp.evidence
    ) or "(无可引用证据)"
    prompt = SOP_USER_TEMPLATE.format(
        title=_clip(inp.title, 500),
        category=inp.category_name or "未分类",
        environment=_clip(inp.environment, 200),
        service=_clip(inp.service, 200),
        component_version=_clip(inp.component_version, 200),
        confirmed_root_cause=_clip(inp.confirmed_root_cause, 2000),
        evidence_block=evidence_block,
    )
    # 最终请求边界统一脱敏(所有进入模型的来源字符串,审查 P1 #3 同一契约)
    return sanitize_text(prompt)


def verify_draft_content(
    content: SopDraftContent, inp: SopGenInput
) -> tuple[SopDraftContent, list[str]]:
    """校验来源标注与缺失标注:无效证据引用移除,缺失/推测内容强制标注,记录原因。"""
    reasons: list[str] = []
    valid_evidence = {e.evidence_id for e in inp.evidence}

    new_sources: list[SopFieldSource] = []
    for s in content.sources:
        if s.target not in CONTENT_FIELDS:
            reasons.append(f"sources: 移除未知目标字段 {s.target}")
            continue
        kept = [i for i in s.evidence_ids if i in valid_evidence]
        dropped = [i for i in s.evidence_ids if i not in valid_evidence]
        if dropped:
            reasons.append(f"sources[{s.target}]: 移除无效证据引用 {dropped}")
        if kept:
            new_sources.append(SopFieldSource(target=s.target, evidence_ids=kept))
    content.sources = new_sources

    # 材料缺失的字段必须标注「待补/待人工确认」,不得用看似完整的内容掩盖缺失
    updates: dict[str, str] = {}
    empty_sources = {
        "applicable_env": not any([inp.environment, inp.service, inp.component_version]),
        "symptom": not inp.symptom.strip(),
        "handling_steps": not inp.handling_steps.strip(),
        "recovery_verification": not inp.recovery_verification.strip(),
    }
    for fname, src_empty in empty_sources.items():
        text = getattr(content, fname)
        if src_empty and text.strip() and not any(m in text for m in PENDING_MARKERS):
            updates[fname] = text.rstrip() + "\n【待补】来源材料缺失,以上内容需人工核对补充。"
            reasons.append(f"{fname}: 来源材料缺失且未标注待补,已追加待补标注")

    # 关键内容无有效来源映射时明确「待人工确认」(复审 P2-4):
    # 全局证据清单不能当作字段来源映射;没有可核对依据的关键内容不伪装成有依据。
    mapped = {s.target for s in content.sources}
    for fname in KEY_SOURCE_FIELDS:
        text = updates.get(fname) or getattr(content, fname)
        if text.strip() and fname not in mapped and not any(m in text for m in PENDING_MARKERS):
            updates[fname] = text.rstrip() + "\n【待人工确认】该字段缺少可核对的来源依据,请人工核对。"
            reasons.append(f"{fname}: 无有效来源映射,已标注待人工确认")

    # 猜测性回滚不得写成已验证方案
    if content.rollback_steps.strip() and not any(
        m in content.rollback_steps for m in ("待验证", "待人工确认", "待补")
    ):
        updates["rollback_steps"] = (
            content.rollback_steps.rstrip() + "\n【待验证】回滚方式未经验证,使用前请人工确认。"
        )
        reasons.append("rollback_steps: 回滚方式缺少待验证标注,已追加")

    if updates:
        content = content.model_copy(update=updates)
    return content, reasons


def _demo_content(inp: SopGenInput) -> SopDraftContent:
    """演示模式的确定性模拟草稿:仅整理真实来源材料,明确标注演示数据。"""
    env_lines = [
        f"环境: {inp.environment}" if inp.environment else "",
        f"服务: {inp.service}" if inp.service else "",
        f"组件版本: {inp.component_version}" if inp.component_version else "",
    ]
    valid_ids = {e.evidence_id for e in inp.evidence}
    sources = [
        SopFieldSource(target="handling_steps", evidence_ids=["case.handling_steps"]),
        SopFieldSource(target="recovery_verification", evidence_ids=["case.recovery_verification"]),
        SopFieldSource(target="applicable_env", evidence_ids=["case.applicable_env"]),
        SopFieldSource(target="symptom", evidence_ids=["case.symptom"]),
    ]
    sources = [
        SopFieldSource(target=s.target, evidence_ids=[i for i in s.evidence_ids if i in valid_ids])
        for s in sources
    ]
    sources = [s for s in sources if s.evidence_ids]
    return SopDraftContent(
        title=f"{inp.title[:120]} 处理 SOP",
        applicable_env="\n".join(l for l in env_lines if l) or "【待补】适用环境与版本待人工确认",
        symptom=inp.symptom or "【待补】现象待人工补充",
        preconditions="【演示数据 / 模拟生成】前置条件待人工确认",
        check_steps="【演示数据 / 模拟生成】排查步骤为模拟整理(命令仅文本,不会执行)",
        handling_steps=inp.handling_steps or "【待补】处理方法待人工补充",
        recovery_verification=inp.recovery_verification or "【待补】恢复验证待人工补充",
        rollback_steps="【待验证】演示数据 / 模拟生成:回滚方式未经验证,待人工确认",
        notes="演示数据 / 模拟生成:不代表真实模型能力。",
        missing_information=["演示数据:待人工补充前置条件、排查步骤与回滚方案"],
        sources=sources,
    )


def _generate(inp: SopGenInput, settings: Settings, adapter) -> SopGenOutcome:
    """执行生成(demo 确定性模拟;真实模式走共用 JSON 有限修复)。"""
    if isinstance(adapter, DemoAdapter):
        content = _demo_content(inp)
        content, reasons = verify_draft_content(content, inp)
        return SopGenOutcome(
            content=content,
            incomplete_reasons=reasons,
            usage=None,  # 模拟生成不虚构 usage
            call_count=0,  # 未调用真实模型
            latency_ms=0,
            is_demo=True,
        )

    user_prompt = _build_user_prompt(inp)
    max_chars = settings.ai_max_context_chars
    if len(user_prompt) > max_chars:
        user_prompt = user_prompt[:max_chars] + "\n…[上下文已截断]"
    messages = [
        ChatMessage(role="system", content=SOP_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_prompt),
    ]
    completion = complete_json(
        adapter,
        messages,
        max_output_tokens=settings.ai_max_output_tokens,
        validate=SopDraftContent.model_validate,
        json_mode=True,
        subject="SOP 草稿",
    )
    if completion.value is None:
        return SopGenOutcome(
            content=None,
            error_code=completion.error_code,
            error_message=completion.error_message,
            usage=completion.usage,
            call_count=completion.call_count,
            latency_ms=completion.latency_ms,
            is_demo=False,
        )
    content, reasons = verify_draft_content(completion.value, inp)
    return SopGenOutcome(
        content=content,
        incomplete_reasons=reasons,
        usage=completion.usage,
        call_count=completion.call_count,
        latency_ms=completion.latency_ms,
        is_demo=False,
    )


def _field_sources_dict(content: SopDraftContent) -> list[dict]:
    """校验后的字段来源映射(持久化用):[{target, evidence_ids}],不含来源正文。"""
    return [{"target": s.target, "evidence_ids": list(s.evidence_ids)} for s in content.sources]


def _create_draft(
    db: Session,
    user: User,
    inp: SopGenInput,
    content: SopDraftContent,
    is_demo: bool,
    incomplete_reasons: list[str],
) -> Sop:
    """由后端决定 doc_id/version/status/来源/维护人;模型内容仅填充 SOP 内容字段。

    字段来源映射与校验提示一并写入 notes,供人工在草稿编辑页核对
    (刷新/重新进入草稿后仍可核对关键字段与有效证据的对应关系,复审 P2-4)。
    """
    notes_parts: list[str] = []
    if is_demo:
        notes_parts.append("【演示数据 / 模拟生成】此草稿由演示适配器模拟生成,不代表真实模型能力。")
    notes_parts.append("【AI 辅助生成草稿,请人工核对后提交审核】")
    if content.notes.strip():
        notes_parts.append(content.notes.strip())
    if content.missing_information:
        notes_parts.append(
            "【待补信息(AI 整理,请人工核对后处理)】\n"
            + "\n".join(f"- {m.strip()}" for m in content.missing_information if m.strip())
        )
    # 字段来源映射(只列证据 ID,不含来源正文;无有效来源的关键内容明确待人工确认)
    mapped = {s.target: s.evidence_ids for s in content.sources}
    source_lines = [
        f"- {t} ← {', '.join(mapped[t])}" for t in CONTENT_FIELDS if t in mapped
    ]
    for t in KEY_SOURCE_FIELDS:
        if t not in mapped:
            source_lines.append(f"- {t} ← (无有效来源,待人工确认)")
    notes_parts.append(
        "【字段来源依据(证据 ID 为生成时的来源材料标识,请人工核对)】\n" + "\n".join(source_lines)
    )
    if incomplete_reasons:
        notes_parts.append(
            "【AI 整理校验提示(请人工核对)】\n"
            + "\n".join(f"- {r}" for r in incomplete_reasons)
        )
    sop = Sop(
        doc_id=str(uuid.uuid4()),
        version=1,
        title=content.title.strip(),
        category_id=inp.category_id,
        tags=list(inp.tags or []),
        applicable_env=content.applicable_env,
        symptom=content.symptom,
        preconditions=content.preconditions,
        check_steps=content.check_steps,
        handling_steps=content.handling_steps,
        recovery_verification=content.recovery_verification,
        rollback_steps=content.rollback_steps,
        notes="\n\n".join(notes_parts),
        source_case_id=inp.case_id,
        maintainer_id=user.id,
        status=SopStatus.draft,
        created_by=user.id,
    )
    db.add(sop)
    db.flush()
    return sop


# ---- 幂等返回与执行领取(复审 P1-1 / P2-1 / P2-3)----


def _find_run_by_token(db: Session, request_token: str) -> SopAiGenerationRun | None:
    return db.execute(
        select(SopAiGenerationRun).where(SopAiGenerationRun.request_token == request_token)
    ).scalar_one_or_none()


def _check_run_owner(run: SopAiGenerationRun, user: User, case_id: int) -> None:
    """所有既有结果返回路径的一致归属复核:不匹配不得序列化他人的 run(P1-1)。"""
    if run.case_id != case_id or run.created_by != user.id:
        raise TokenConflict("request_token 与既有请求不匹配")


def _success_result(db: Session, run: SopAiGenerationRun, reused: bool) -> SopGenResult:
    """幂等成功返回路径的可见性复核:只返回调用者本人的草稿(P1-1)。"""
    sop = db.get(Sop, run.sop_id) if run.sop_id else None
    if sop is None or sop.created_by != run.created_by:
        raise TokenConflict("既有生成结果不可用(草稿不存在或不可见),请重新生成")
    return SopGenResult(run=run, sop=sop, reused=reused)


def _conflict_or_reuse(db: Session, user: User, case_id: int, request_token: str) -> SopGenResult:
    """领取/首撞失败后的一致出口:复用本人已完成结果,或明确冲突(P1-1)。"""
    current = _find_run_by_token(db, request_token)
    if current is not None:
        _check_run_owner(current, user, case_id)
        if current.status == SopGenStatus.success.value:
            return _success_result(db, current, reused=True)
        if current.status == SopGenStatus.running.value:
            raise TokenConflict("同一请求正在生成中,请勿重复提交")
    raise TokenConflict("同一请求已由其他执行接管,本次未重复执行")


def _claim_run(
    db: Session,
    run_id: int,
    input_hash: str,
    input_snapshot: dict,
    config_ver: str,
    is_demo: bool,
) -> int | None:
    """原子领取 failed/过期 running 的执行权(CAS 条件更新,复审 P2-1)。

    并发下至多一个执行者领取成功;同时把输入快照/模型配置刷新为本次执行的实际值
    (复审 P2-3:重试产物不得沿用首次失败时的配置标识)。
    返回本次执行的 attempt(结果提交条件);未领取到返回 None。
    """
    now = utcnow()
    stale_cutoff = now - timedelta(seconds=STALE_RUNNING_SECONDS)
    stmt = (
        update(SopAiGenerationRun)
        .where(
            SopAiGenerationRun.id == run_id,
            or_(
                SopAiGenerationRun.status == SopGenStatus.failed.value,
                and_(
                    SopAiGenerationRun.status == SopGenStatus.running.value,
                    SopAiGenerationRun.updated_at < stale_cutoff,
                ),
            ),
        )
        .values(
            status=SopGenStatus.running.value,
            attempt=SopAiGenerationRun.attempt + 1,
            input_hash=input_hash,
            input_snapshot=input_snapshot,
            model_config_version=config_ver,
            is_demo=is_demo,
            error_code="",
            error_message="",
            sop_id=None,
            missing_information=[],
            incomplete_reasons=[],
            field_sources=[],
            usage=None,
            call_count=0,
            latency_ms=0,
            updated_at=now,
        )
        .returning(SopAiGenerationRun.attempt)
    )
    row = db.execute(stmt).first()
    db.commit()  # 尽快释放行锁;模型调用在事务外执行
    return int(row[0]) if row is not None else None


def _finalize_run(db: Session, run: SopAiGenerationRun, attempt: int, **fields) -> bool:
    """条件提交结果:仅当仍是本执行者领取的 running 记录才写入(P2-1)。

    被接管(attempt 已递增)后返回 False:调用方不得覆盖新结果;
    成功时刷新实体,保证响应反映实际提交的字段。
    """
    res = db.execute(
        update(SopAiGenerationRun)
        .where(
            SopAiGenerationRun.id == run.id,
            SopAiGenerationRun.attempt == attempt,
            SopAiGenerationRun.status == SopGenStatus.running.value,
        )
        .values(updated_at=utcnow(), **fields)
    )
    if res.rowcount == 1:
        db.refresh(run)
        return True
    return False


def _finish_failed(
    db: Session,
    run: SopAiGenerationRun,
    attempt: int,
    user: User,
    case_id: int,
    request_token: str,
    error_code: str,
    error_message: str,
    outcome: SopGenOutcome,
) -> SopGenResult:
    ok = _finalize_run(
        db,
        run,
        attempt,
        status=SopGenStatus.failed.value,
        sop_id=None,
        missing_information=[],
        incomplete_reasons=[],
        field_sources=[],
        error_code=error_code,
        error_message=error_message,
        usage=outcome.usage,
        call_count=outcome.call_count,
        latency_ms=outcome.latency_ms,
        is_demo=outcome.is_demo,
    )
    if ok:
        db.commit()
        return SopGenResult(run=run, sop=None, reused=False)
    db.rollback()  # 被接管:不覆盖新结果
    return _conflict_or_reuse(db, user, case_id, request_token)


def generate_sop_draft(
    db: Session,
    user: User,
    case_id: int,
    request_token: str,
    settings: Settings,
) -> SopGenResult:
    """执行一次 AI 生成 SOP 草稿并落库(含幂等/限流/来源复核)。

    调用方(路由)须已完成生成权限与案例状态检查;本函数仍会复核来源,
    并在模型返回后、创建草稿前再次复核(来源已变化不得落成有效草稿)。
    执行权经数据库原子领取,结果按 attempt 条件提交:
    并发同 token 至多一个执行者、一份草稿;被接管的旧执行者不创建草稿、不覆盖新结果。
    """
    if settings.ai_mode == "disabled":
        raise AiUnavailable("AI 生成不可用(AI_MODE=disabled);模板生成与手工编辑仍可用")

    # 调用前来源校验:列级读取,案例必须仍为已通过
    inp = load_sop_gen_input(db, case_id)
    if inp is None:
        raise SourceRejected("案例不存在")
    if inp.status != CaseStatus.approved:
        raise SourceRejected("来源案例当前状态不允许生成(仅已通过案例可生成)")
    if user.role == Role.operator and inp.created_by != user.id:
        raise SourceRejected("只能从自己提交的案例生成草稿")
    input_hash = compute_input_hash(inp)
    input_snapshot = {
        "case_title": inp.title[:200],
        "evidence_ids": [e.evidence_id for e in inp.evidence],
    }

    # 幂等:同 request_token 的成功结果直接返回(不重复创建草稿/不重复计费)
    existing = _find_run_by_token(db, request_token)
    if existing is not None:
        _check_run_owner(existing, user, case_id)  # 所有返回路径一致复核归属(P1-1)
        if existing.status == SopGenStatus.success.value:
            return _success_result(db, existing, reused=True)  # 含草稿可见性复核

    # 配置错误在落记录前失败(不创建空白记录);失败不回退演示/模板结果
    adapter = build_adapter(settings)
    is_demo = isinstance(adapter, DemoAdapter)
    config_ver = settings.ai_config_version

    ai_service._acquire_slot(user.id, settings)  # 频率/并发限制
    try:
        run: SopAiGenerationRun
        if existing is None:
            run = SopAiGenerationRun(
                case_id=case_id,
                created_by=user.id,
                request_token=request_token,
                attempt=1,
                input_hash=input_hash,
                input_snapshot=input_snapshot,
                model_config_version=config_ver,
                is_demo=is_demo,
                status=SopGenStatus.running.value,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            db.add(run)
            try:
                db.commit()
                my_attempt = 1
            except IntegrityError:
                # 并发同 token 首撞:既有结果同样必须复核归属/可见性(P1-1),不得返回他人草稿
                db.rollback()
                return _conflict_or_reuse(db, user, case_id, request_token)
        else:
            # failed/过期 running:原子领取(并发只有一个执行者),并刷新本次输入/配置绑定(P2-3)
            my_attempt = _claim_run(db, existing.id, input_hash, input_snapshot, config_ver, is_demo)
            if my_attempt is None:
                return _conflict_or_reuse(db, user, case_id, request_token)
            run = existing

        outcome = _generate(inp, settings, adapter)
        if outcome.content is None:
            return _finish_failed(
                db, run, my_attempt, user, case_id, request_token,
                outcome.error_code, outcome.error_message, outcome,
            )

        # 返回后、创建草稿前:来源状态与输入快照复核(调用期间来源变化不得落成有效草稿)
        current = load_sop_gen_input(db, case_id)
        if (
            current is None
            or current.status != CaseStatus.approved
            or compute_input_hash(current) != input_hash
        ):
            return _finish_failed(
                db, run, my_attempt, user, case_id, request_token,
                "source_changed",
                "来源案例在生成期间发生变化(状态或内容),本次结果未落为草稿",
                outcome,
            )

        # 成功:草稿与运行结果同一提交;被接管则整体丢弃(旧执行者不留下孤儿草稿,P2-1)
        sop = _create_draft(db, user, inp, outcome.content, outcome.is_demo, outcome.incomplete_reasons)
        ok = _finalize_run(
            db,
            run,
            my_attempt,
            status=SopGenStatus.success.value,
            sop_id=sop.id,
            missing_information=[
                m.strip() for m in outcome.content.missing_information if m and m.strip()
            ],
            incomplete_reasons=outcome.incomplete_reasons,
            field_sources=_field_sources_dict(outcome.content),
            error_code="",
            error_message="",
            usage=outcome.usage,
            call_count=outcome.call_count,
            latency_ms=outcome.latency_ms,
            is_demo=outcome.is_demo,
        )
        if not ok:
            db.rollback()  # 丢弃未提交草稿:被接管后的旧执行者不得再创建草稿/覆盖新结果
            return _conflict_or_reuse(db, user, case_id, request_token)
        record_audit(
            db,
            user,
            "sop.ai_generate",
            "sop",
            sop.id,
            {"case_id": case_id, "is_demo": outcome.is_demo, "request_token": request_token[:16]},
        )
        db.commit()
        return SopGenResult(run=run, sop=sop, reused=False)
    finally:
        ai_service._release_slot(user.id)
