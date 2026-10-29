"""第四阶段第一批:显式演示内容 seed 的幂等、边界与负面夹具回归。

覆盖:
- 未执行不写业务示例;production 拒绝;
- 正向场景(含同现象不同根因)写入已通过案例/已发布 SOP,标记为演示预置;
- 待审/退回案例、草稿/待审/历史/废弃 SOP 不进入正式检索;
- 信息不足、知识库无匹配、提示词注入只保存为待分析数据;
- 重复执行不重复创建、不覆盖人工修改;失败整体回滚。
"""

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.demo_content import (
    BOUNDARY_SCENARIOS,
    DEMO_SEED_VERSION,
    DemoSeedError,
    DemoSeedForbidden,
    PRESET_FIELD_PREFIX,
    PRESET_MARK,
    seed_demo_content,
)
from app.models import (
    AiAnalysis,
    Case,
    CaseStatus,
    Category,
    DemoSeedRecord,
    Fault,
    FaultStatus,
    Role,
    Sop,
    SopAiGenerationRun,
    SopStatus,
)
from app.search.keyword import search_knowledge
from app.seed import SYSTEM_CATEGORIES, ensure_seed
from tests.conftest import TestingSessionLocal, make_user


@pytest.fixture
def demo_env(db, monkeypatch):
    """内置分类 + 独立运维/审核账号(不创建演示业务内容)。"""
    settings = Settings(opssop_env="development", opssop_seed_demo=False)
    monkeypatch.setattr("app.seed.get_settings", lambda: settings)
    ensure_seed(session_factory=TestingSessionLocal)
    operator = make_user(db, "demo_op", Role.operator)
    reviewer = make_user(db, "demo_rev", Role.reviewer)
    return settings, operator, reviewer


def _run_seed(db, settings, operator="demo_op", reviewer="demo_rev"):
    return seed_demo_content(
        db,
        operator_username=operator,
        reviewer_username=reviewer,
        settings=settings,
    )


def _counts(db):
    return {
        "fault": db.scalar(select(func.count()).select_from(Fault)),
        "case": db.scalar(select(func.count()).select_from(Case)),
        "sop": db.scalar(select(func.count()).select_from(Sop)),
        "link": db.scalar(select(func.count()).select_from(DemoSeedRecord)),
    }


def test_startup_seed_does_not_create_business_examples(demo_env, db):
    names = {c.name for c in db.scalars(select(Category)).all()}
    assert names >= {name for name, _ in SYSTEM_CATEGORIES}
    assert _counts(db) == {"fault": 0, "case": 0, "sop": 0, "link": 0}


def test_production_refuses_demo_content(demo_env, db):
    with pytest.raises(DemoSeedForbidden):
        _run_seed(db, Settings(opssop_env="production"))
    assert _counts(db) == {"fault": 0, "case": 0, "sop": 0, "link": 0}


def test_seed_creates_marked_presets_and_negative_fixtures(demo_env, db):
    settings, _, _ = demo_env
    summary = _run_seed(db, settings)

    assert summary["created"] == {"fault": 18, "case": 15, "sop": 14}
    assert summary["seed_version"] == DEMO_SEED_VERSION
    assert "demo.oomkilled" in summary["scenarios"]
    assert "demo.insufficient-info" in summary["boundary_scenarios"]
    assert "demo.negative.pending-case" in summary["negative_scenarios"]

    positive_cases = db.scalars(
        select(Case).where(Case.title.startswith(PRESET_MARK))
    ).all()
    assert len(positive_cases) == 9
    for case in positive_cases:
        assert case.status == CaseStatus.approved
        assert case.reviewed_by is not None
        assert "演示预置" in case.review_comment
        assert case.confirmed_root_cause.startswith(PRESET_FIELD_PREFIX)

    published_presets = db.scalars(
        select(Sop)
        .where(Sop.status == SopStatus.published)
        .where(Sop.title.startswith(PRESET_MARK))
    ).all()
    assert len(published_presets) == 9
    for sop in published_presets:
        assert sop.version == 1
        assert sop.source_case_id is not None
        assert "演示预置" in sop.notes
        assert sop.reviewed_by is not None

    # 预置示例不是 AI 结果:不得写入分析历史或 AI 生成运行记录
    assert db.scalar(select(func.count()).select_from(AiAnalysis)) == 0
    assert db.scalar(select(func.count()).select_from(SopAiGenerationRun)) == 0

    # 来源关系:案例 -> 故障,SOP -> 来源案例
    oom_case = db.scalar(select(Case).where(Case.title.contains("DEMO-OOMKILLED")))
    assert oom_case is not None
    oom_fault = db.get(Fault, oom_case.fault_id)
    assert oom_fault is not None and oom_fault.status == FaultStatus.resolved
    oom_sop = db.scalar(select(Sop).where(Sop.source_case_id == oom_case.id))
    assert oom_sop is not None and oom_sop.source_case_id == oom_case.id

    # 负面夹具状态明确
    assert db.scalar(
        select(Case.status).where(Case.title.contains("DEMO-NEG-PENDING-CASE"))
    ) == CaseStatus.pending_review
    assert db.scalar(
        select(Case.status).where(Case.title.contains("DEMO-NEG-RETURNED-CASE"))
    ) == CaseStatus.returned
    assert db.scalar(
        select(Sop.status).where(Sop.title.contains("DEMO-NEG-DRAFT-SOP"))
    ) == SopStatus.draft
    assert db.scalar(
        select(Sop.status).where(Sop.title.contains("DEMO-NEG-SUBMITTED-SOP"))
    ) == SopStatus.submitted
    assert db.scalar(
        select(Sop.status).where(Sop.title.contains("DEMO-NEG-SUPERSEDED-SOP"))
    ) == SopStatus.superseded
    assert db.scalar(
        select(Sop.status).where(Sop.title.contains("DEMO-NEG-DEPRECATED-SOP"))
    ) == SopStatus.deprecated


def test_negative_fixtures_and_no_knowledge_match_excluded(demo_env, db):
    settings, operator, _ = demo_env
    _run_seed(db, settings)

    def hits(query: str):
        return search_knowledge(db, operator, query)[0]

    # 正向来源可检索,并带可刷新定位链接/版本
    oom_hits = hits("DEMO-OOMKILLED")
    assert {h.source_type for h in oom_hits} == {"case", "sop"}
    for hit in oom_hits:
        assert hit.link == (
            f"/cases/{hit.source_id}" if hit.source_type == "case" else f"/sops/{hit.source_id}"
        )
        if hit.source_type == "sop":
            assert hit.version == 1

    # 负面夹具:待审/退回案例、草稿/待审/历史/废弃 SOP 均不进入正式检索
    for query in (
        "DEMO-NEG-PENDING-CASE",
        "DEMO-NEG-RETURNED-CASE",
        "DEMO-NEG-DRAFT-SOP",
        "DEMO-NEG-SUBMITTED-SOP",
        "DEMO-NEG-SUPERSEDED-SOP",
        "DEMO-NEG-DEPRECATED-SOP",
        "DEMO-NOMATCH-7K2P",
    ):
        assert hits(query) == [], query

    current = hits("DEMO-NEG-SUPERSEDED-CURRENT")
    assert len(current) == 1
    assert current[0].source_type == "sop" and current[0].version == 2
    assert current[0].source_id == db.scalar(
        select(Sop.id).where(Sop.title.contains("DEMO-NEG-SUPERSEDED-CURRENT"))
    )

    # 相同现象「接口超时」有多个不同根因,不能被当成单一根因
    timeout_cases = [h for h in hits("接口超时") if h.source_type == "case"]
    assert len(timeout_cases) >= 2
    roots = {
        c.confirmed_root_cause
        for c in db.scalars(
            select(Case).where(Case.id.in_([h.source_id for h in timeout_cases]))
        ).all()
    }
    assert len(roots) >= 2
    dns_case = db.scalar(select(Case).where(Case.title.contains("DEMO-TIMEOUT-DNS")))
    redis_case = db.scalar(select(Case).where(Case.title.contains("DEMO-TIMEOUT-REDIS")))
    hit_ids = {h.source_id for h in timeout_cases}
    assert dns_case is not None and redis_case is not None
    assert {dns_case.id, redis_case.id} <= hit_ids
    assert dns_case.confirmed_root_cause != redis_case.confirmed_root_cause


def test_prompt_injection_is_data_only(demo_env, db):
    settings, operator, _ = demo_env
    _run_seed(db, settings)
    fault = db.scalar(select(Fault).where(Fault.title.contains("DEMO-INJECTION-Q9")))
    assert fault is not None
    # 注入文本原样保存,未执行任何命令/状态变更
    assert "rm -rf /" in fault.error_logs
    assert fault.status == FaultStatus.open
    assert db.scalar(select(Case).where(Case.fault_id == fault.id)) is None
    # 注入场景没有创建正式知识,检索无匹配
    assert search_knowledge(db, operator, "DEMO-INJECTION-Q9")[0] == []


def test_rerun_is_idempotent_and_preserves_manual_edits(demo_env, db):
    settings, _, _ = demo_env
    _run_seed(db, settings)
    first = _counts(db)
    assert first == {"fault": 18, "case": 15, "sop": 14, "link": 47}

    summary = _run_seed(db, settings)
    assert summary["created"] == {"fault": 0, "case": 0, "sop": 0}
    assert summary["skipped"] == {"fault": 18, "case": 15, "sop": 14}
    assert _counts(db) == first

    # 人工修改标题/根因后再次执行:稳定标识仍定位同一记录,不重复创建、不覆盖
    oom_case = db.scalar(select(Case).where(Case.title.contains("DEMO-OOMKILLED")))
    oom_fault = db.get(Fault, oom_case.fault_id)
    old_ids = (oom_case.id, oom_fault.id)
    oom_case.title = "人工修改后的演示案例标题"
    oom_case.confirmed_root_cause = "人工修改后的根因(应保留)"
    oom_fault.title = "人工修改后的故障标题"
    db.commit()

    _run_seed(db, settings)
    db.expire_all()
    assert _counts(db) == first
    oom_case2 = db.get(Case, old_ids[0])
    assert oom_case2.title == "人工修改后的演示案例标题"
    assert oom_case2.confirmed_root_cause == "人工修改后的根因(应保留)"
    assert db.get(Fault, old_ids[1]).title == "人工修改后的故障标题"


def test_failure_rolls_back_whole_run(demo_env, db, monkeypatch):
    settings, _, _ = demo_env

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟中途失败")

    monkeypatch.setattr("app.demo_content._create_sop", boom)
    with pytest.raises(RuntimeError, match="模拟中途失败"):
        _run_seed(db, settings)
    # 本次写入的故障/案例/映射全部回滚,不留下半成品
    assert _counts(db) == {"fault": 0, "case": 0, "sop": 0, "link": 0}


def test_boundary_scenarios_have_no_formal_knowledge(demo_env, db):
    settings, _, _ = demo_env
    _run_seed(db, settings)
    markers = ("DEMO-INSUFFICIENT-INFO", "DEMO-NOMATCH-7K2P", "DEMO-INJECTION-Q9")
    for marker in markers:
        fault = db.scalar(select(Fault).where(Fault.title.contains(marker)))
        assert fault is not None and fault.status == FaultStatus.open
        assert db.scalar(select(Case).where(Case.fault_id == fault.id)) is None
        assert db.scalar(
            select(Sop).where(Sop.source_case_id.is_not(None)).join(
                Case, Sop.source_case_id == Case.id
            ).where(Case.fault_id == fault.id)
        ) is None

    # 正式知识数量:13 个已通过案例(9 正向 + 4 负面夹具场景),10 个当前有效已发布 SOP
    assert db.scalar(
        select(func.count()).select_from(Case).where(Case.status == CaseStatus.approved)
    ) == 13
    assert db.scalar(
        select(func.count()).select_from(Sop).where(Sop.status == SopStatus.published)
    ) == 10
    assert len(BOUNDARY_SCENARIOS) == 3


def test_seed_requires_existing_accounts(demo_env, db):
    settings, _, _ = demo_env
    with pytest.raises(DemoSeedError, match="演示 seed 需要已存在的活动"):
        _run_seed(db, settings, operator="missing-operator")
    assert _counts(db) == {"fault": 0, "case": 0, "sop": 0, "link": 0}
