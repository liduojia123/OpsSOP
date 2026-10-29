"""数据库核验快照:只输出计数、状态分布和内容摘要,不输出业务全文/口令哈希。"""

from __future__ import annotations

import hashlib
import json

from .common import OpsError
from .dockerops import ComposeStack

TABLES = [
    "users",
    "categories",
    "faults",
    "fault_records",
    "cases",
    "sops",
    "sop_feedback",
    "audit_events",
    "ai_analyses",
    "sop_ai_generation_runs",
    "embedding_index_state",
    "embedding_index_units",
    "demo_seed_records",
]


def _counts_sql() -> str:
    counts = ", ".join(
        f"'{table}', (SELECT count(*) FROM {table})" for table in TABLES
    )
    return f"SELECT json_build_object({counts})::text"


def _status_sql() -> str:
    return """
        SELECT json_build_object(
          'users_by_role', (SELECT json_object_agg(role, n) FROM
             (SELECT role::text AS role, count(*) AS n FROM users GROUP BY role) s),
          'cases_by_status', (SELECT json_object_agg(status, n) FROM
             (SELECT status::text AS status, count(*) AS n FROM cases GROUP BY status) s),
          'sops_by_status', (SELECT json_object_agg(status, n) FROM
             (SELECT status::text AS status, count(*) AS n FROM sops GROUP BY status) s),
          'faults_by_status', (SELECT json_object_agg(status, n) FROM
             (SELECT status::text AS status, count(*) AS n FROM faults GROUP BY status) s)
        )::text
    """


def _business_sql() -> str:
    """用于可比对的稳定业务摘要;只返回规范化 JSON 的哈希。"""
    return """
        SELECT json_build_object(
          'cases', (SELECT json_agg(json_build_object(
              'id', id, 'fault_id', fault_id, 'title', title,
              'status', status::text, 'root', confirmed_root_cause,
              'handling', handling_steps, 'recovery', recovery_verification)
            ORDER BY id) FROM cases),
          'sops', (SELECT json_agg(json_build_object(
              'id', id, 'doc_id', doc_id, 'version', version, 'title', title,
              'status', status::text, 'source_case_id', source_case_id,
              'handling', handling_steps)
            ORDER BY doc_id, version) FROM sops),
          'audit', (SELECT json_agg(json_build_object(
              'id', id, 'action', action, 'target_type', target_type,
              'target_id', target_id, 'actor_id', actor_id)
            ORDER BY id) FROM audit_events),
          'feedback', (SELECT json_agg(json_build_object(
              'id', id, 'sop_id', sop_id, 'kind', kind::text, 'comment', comment)
            ORDER BY id) FROM sop_feedback)
        )::text
    """


def _canonical_digest(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def take_snapshot(stack: ComposeStack, *, database: str | None = None) -> dict:
    """采集当前数据库的可核对快照;失败即抛 OpsError,不返回半成品。"""
    counts = stack.psql_json(_counts_sql(), database=database)
    statuses = stack.psql_json(_status_sql(), database=database)
    business = stack.psql_json(_business_sql(), database=database)
    meta = stack.psql_json(
        """
        SELECT json_build_object(
          'database', current_database(),
          'server_version', current_setting('server_version'),
          'alembic_version', (SELECT version_num FROM alembic_version),
          'vector_version', (SELECT extversion FROM pg_extension WHERE extname='vector')
        )::text
        """,
        database=database,
    )
    for key in ("database", "alembic_version"):
        if not meta.get(key):
            raise OpsError(f"数据库快照缺少 {key}", stage="snapshot")
    return {
        "database": meta["database"],
        "server_version": meta["server_version"],
        "alembic_version": meta["alembic_version"],
        "vector_version": meta.get("vector_version"),
        "counts": counts,
        "statuses": statuses,
        "business_digest": _canonical_digest(business),
        "table_order": TABLES,
    }


def compare_snapshots(source: dict, restored: dict) -> dict:
    """比较关键计数/状态/业务摘要;返回可报告的差异列表。"""
    differences: list[dict] = []
    for key in ("alembic_version", "vector_version"):
        if source.get(key) != restored.get(key):
            differences.append(
                {"field": key, "source": source.get(key), "restored": restored.get(key)}
            )
    for table in TABLES:
        before = (source.get("counts") or {}).get(table)
        after = (restored.get("counts") or {}).get(table)
        if before != after:
            differences.append(
                {"field": f"counts.{table}", "source": before, "restored": after}
            )
    for key in ("users_by_role", "cases_by_status", "sops_by_status", "faults_by_status"):
        before = (source.get("statuses") or {}).get(key)
        after = (restored.get("statuses") or {}).get(key)
        if before != after:
            differences.append(
                {"field": f"statuses.{key}", "source": before, "restored": after}
            )
    if source.get("business_digest") != restored.get("business_digest"):
        differences.append(
            {
                "field": "business_digest",
                "source": source.get("business_digest"),
                "restored": restored.get("business_digest"),
            }
        )
    return {"equal": not differences, "differences": differences}
