"""评测夹具准备:稳定键映射、合成用户/分类、知识夹具与分析故障。

夹具只写入已确认的本轮 `opssop_eval_*` 库:
- 知识夹具使用稳定 key -> (entity_type, entity_id, version) 映射,不依赖标题;
- 映射写入 eval_run_manifest,重复准备时按稳定键跳过,不重复/不覆盖;
- 分析故障按 sample_id 稳定映射;记录输入证据作为待分析数据;
- 预置审核/发布状态只是评测夹具,不是真实业务审核流程证据。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.eval.datasets import Dataset, KnowledgeFixture, Sample
from app.models import (
    Case,
    CaseStatus,
    Category,
    Fault,
    FaultRecord,
    FaultStatus,
    RecordKind,
    Role,
    Sop,
    SopStatus,
    User,
    utcnow,
)
from app.security import hash_password
from app.seed import SYSTEM_CATEGORIES

# 合成评测账号口令(只在隔离评测库中使用;不写入报告/日志)
EVAL_PASSWORDS = {
    "eval_owner": "eval-owner-synthetic-password-1",
    "eval_viewer": "eval-viewer-synthetic-password-1",
    "eval_reviewer": "eval-reviewer-synthetic-password-1",
}
EVAL_USERS = {
    "eval_owner": ("评测所有者", Role.operator),
    "eval_viewer": ("评测查看者", Role.operator),
    "eval_reviewer": ("评测审核者", Role.reviewer),
}
FIXTURE_TAGS = ["evaluation-fixture"]


class FixtureError(Exception):
    """评测夹具准备失败(缺分类、重复键、映射不一致等)。"""


@dataclass
class FixtureSet:
    users: dict[str, User]
    fixture_map: dict[str, dict]
    analysis_faults: dict[str, int]
    manifest_data: dict


def _ensure_categories(db: Session) -> dict[str, Category]:
    existing = {c.name: c for c in db.scalars(select(Category)).all()}
    for name, sort_order in SYSTEM_CATEGORIES:
        if name not in existing:
            row = Category(name=name, is_system=True, sort_order=sort_order)
            db.add(row)
            db.flush()
            existing[name] = row
    return existing


def _ensure_users(db: Session) -> dict[str, User]:
    users: dict[str, User] = {}
    for username, (display_name, role) in EVAL_USERS.items():
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            user = User(
                username=username,
                display_name=display_name,
                password_hash=hash_password(EVAL_PASSWORDS[username]),
                role=role,
            )
            db.add(user)
            db.flush()
        users[username] = user
    return users


def _find_entity(db: Session, entity_type: str, entity_id: int):
    if entity_type == "case":
        return db.get(Case, entity_id)
    if entity_type == "sop":
        return db.get(Sop, entity_id)
    return None


def _create_case_fixture(
    db: Session,
    fixture: KnowledgeFixture,
    category_id: int,
    owner: User,
    reviewer: User,
) -> Case:
    data = fixture.data
    status = CaseStatus(fixture.status)
    reviewed = status in (CaseStatus.approved, CaseStatus.returned)
    fault = Fault(
        title=f"评测夹具故障:{fixture.key}",
        description=data.get("symptom", ""),
        environment=data.get("environment", "eval"),
        service=data.get("service", "eval-svc"),
        component_version=data.get("component_version", "1.0"),
        category_id=category_id,
        tags=[*FIXTURE_TAGS, "synthetic-case-fixture"],
        status=FaultStatus.resolved,
        actual_root_cause=data.get("confirmed_root_cause", ""),
        handling_steps=data.get("handling_steps", ""),
        recovery_verification=data.get("recovery_verification", ""),
        resolved_at=utcnow(),
        created_by=owner.id,
    )
    db.add(fault)
    db.flush()
    row = Case(
        fault_id=fault.id,
        title=data["title"],
        symptom=data.get("symptom", ""),
        environment=data.get("environment", "eval"),
        service=data.get("service", "eval-svc"),
        component_version=data.get("component_version", "1.0"),
        category_id=category_id,
        tags=[*FIXTURE_TAGS, *data.get("tags", [])],
        confirmed_root_cause=data.get("confirmed_root_cause", ""),
        handling_steps=data.get("handling_steps", ""),
        recovery_verification=data.get("recovery_verification", ""),
        status=status,
        review_comment=("合成评测夹具:预置状态,不是真实审核流程" if reviewed else ""),
        reviewed_by=reviewer.id if reviewed else None,
        reviewed_at=utcnow() if reviewed else None,
        created_by=owner.id,
    )
    db.add(row)
    db.flush()
    return row


def _create_sop_fixture(
    db: Session,
    fixture: KnowledgeFixture,
    dataset: Dataset,
    category_id: int,
    owner: User,
    reviewer: User,
) -> Sop:
    data = fixture.data
    status = SopStatus(fixture.status)
    reviewed = status in (SopStatus.published, SopStatus.superseded, SopStatus.deprecated)
    doc_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"opssop-eval/{dataset.dataset_version}/{data['doc_key']}"))
    row = Sop(
        doc_id=doc_id,
        version=fixture.version or 1,
        title=data["title"],
        category_id=category_id,
        tags=[*FIXTURE_TAGS, *data.get("tags", [])],
        applicable_env=data.get("applicable_env", "评测环境"),
        symptom=data.get("symptom", ""),
        preconditions=data.get("preconditions", ""),
        check_steps=data.get("check_steps", ""),
        handling_steps=data.get("handling_steps", ""),
        recovery_verification=data.get("recovery_verification", ""),
        rollback_steps=data.get("rollback_steps", ""),
        notes=data.get("notes", "合成评测夹具"),
        source_case_id=None,
        maintainer_id=owner.id,
        status=status,
        review_comment=("合成评测夹具:预置状态,不是真实审核流程" if reviewed else ""),
        reviewed_by=reviewer.id if reviewed else None,
        reviewed_at=utcnow() if reviewed else None,
        created_by=owner.id,
        published_at=(
            utcnow()
            if status in (SopStatus.published, SopStatus.superseded, SopStatus.deprecated)
            else None
        ),
    )
    db.add(row)
    db.flush()
    return row


def _create_analysis_fault(
    db: Session, sample: Sample, owner: User
) -> Fault:
    data = sample.input
    fault = Fault(
        title=data.get("title", f"评测输入:{sample.sample_id}"),
        description=data.get("description", ""),
        environment=data.get("environment", "eval"),
        service=data.get("service", "eval-svc"),
        component_version=data.get("component_version", "1.0"),
        recent_changes=data.get("recent_changes", ""),
        error_logs=data.get("error_logs", ""),
        attempted_actions=data.get("attempted_actions", ""),
        category_id=None,
        tags=[*FIXTURE_TAGS, "analysis-input"],
        status=FaultStatus.open,
        created_by=owner.id,
    )
    db.add(fault)
    db.flush()
    for index, record in enumerate(data.get("records") or []):
        kind = record.get("kind", "check_result")
        if kind not in (RecordKind.check_result.value, RecordKind.handling_note.value):
            raise FixtureError(f"{sample.sample_id}: records[{index}] kind 非法 {kind!r}")
        db.add(
            FaultRecord(
                fault_id=fault.id,
                kind=RecordKind(kind),
                content=str(record.get("content", "")),
                created_by=owner.id,
            )
        )
    db.flush()
    return fault


def prepare_fixtures(
    db: Session, dataset: Dataset, manifest: dict
) -> FixtureSet:
    """按稳定键准备本轮夹具;重复准备不重复、不覆盖。"""
    users = _ensure_users(db)
    categories = _ensure_categories(db)
    owner = users["eval_owner"]
    reviewer = users["eval_reviewer"]

    fixture_map: dict[str, dict] = dict((manifest.get("data") or {}).get("fixture_map") or {})
    analysis_faults: dict[str, int] = dict((manifest.get("data") or {}).get("analysis_faults") or {})

    for fixture in dataset.knowledge:
        if fixture.key in fixture_map:
            info = fixture_map[fixture.key]
            entity = _find_entity(db, info.get("entity_type", ""), int(info.get("entity_id", 0)))
            if entity is not None:
                continue
            # 映射存在但实体已缺失:重新创建并更新映射(本轮库内精确修复)
        category = categories.get(fixture.data["category"])
        if category is None:
            raise FixtureError(f"{fixture.key}: 缺少分类 {fixture.data['category']!r}")
        if fixture.kind == "case":
            row = _create_case_fixture(db, fixture, category.id, owner, reviewer)
            fixture_map[fixture.key] = {
                "entity_type": "case",
                "entity_id": row.id,
                "version": None,
            }
        elif fixture.kind == "sop":
            row = _create_sop_fixture(db, fixture, dataset, category.id, owner, reviewer)
            fixture_map[fixture.key] = {
                "entity_type": "sop",
                "entity_id": row.id,
                "version": row.version,
                "doc_id": row.doc_id,
            }
        else:
            raise FixtureError(f"{fixture.key}: 未知 kind {fixture.kind!r}")

    analysis_tasks = {"analysis", "analysis_failure", "analysis_cache", "analysis_permission"}
    for sample in dataset.samples:
        if not (set(sample.tasks) & analysis_tasks):
            continue
        existing_id = analysis_faults.get(sample.sample_id)
        if existing_id is not None:
            existing = db.get(Fault, int(existing_id))
            if existing is not None:
                continue
        fault = _create_analysis_fault(db, sample, owner)
        analysis_faults[sample.sample_id] = fault.id

    manifest_data = {
        **(manifest.get("data") or {}),
        "fixture_map": fixture_map,
        "analysis_faults": analysis_faults,
    }
    # 与夹具同事务写入 manifest 映射:任一步失败不留下半成品映射
    db.execute(
        text(
            "UPDATE eval_run_manifest SET data = CAST(:data AS jsonb) WHERE run_id = :run_id"
        ),
        {
            "data": json.dumps(manifest_data, ensure_ascii=False),
            "run_id": manifest["run_id"],
        },
    )
    db.commit()

    return FixtureSet(
        users=users,
        fixture_map=fixture_map,
        analysis_faults=analysis_faults,
        manifest_data=manifest_data,
    )
