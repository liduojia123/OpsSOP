"""版本化评测集加载与校验。

数据集拆成四个公开的合成文件:
- knowledge.json: 评测知识夹具(稳定键,不含数据库 ID);
- samples.json: 查询/故障输入与任务声明;
- labels.json: 合成参考标签(provisional,非真人 gold);
- synthetic_behaviours.json: 合成模型行为(仅协议/指标机制验证)。

manifest.json 保存数据集版本、标签来源/审核状态和文件清单。运行器计算
内容哈希并写入报告;数据集本身不包含真实业务日志、个人路径或真实模型输出。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from app.seed import SYSTEM_CATEGORIES

SCHEMA_VERSION = "opssop-eval-dataset/1"
TASKS = {"retrieval", "analysis", "analysis_failure", "analysis_cache", "analysis_permission"}
BEHAVIOURS = {
    "valid",
    "timeout",
    "rate_limited",
    "bad_json",
    "schema_bad",
    "schema_repair_then_valid",
    "invalid_reference",
    "partial_usage",
    "source_invalidation",
}
KNOWLEDGE_STATUSES = {
    "case": {"approved", "pending_review", "returned"},
    "sop": {"draft", "submitted", "returned", "published", "superseded", "deprecated"},
}
_CATEGORY_NAMES = {name for name, _ in SYSTEM_CATEGORIES}


class DatasetError(Exception):
    """数据集缺失、版本不匹配或内容校验失败。"""


@dataclass(frozen=True)
class KnowledgeFixture:
    key: str
    kind: str
    status: str
    data: dict
    version: int | None = None


@dataclass(frozen=True)
class Sample:
    sample_id: str
    group: str
    tasks: tuple[str, ...]
    retrieval_query: str | None
    input: dict


@dataclass(frozen=True)
class Label:
    sample_id: str
    expected_category: str | None
    allowed_categories: tuple[str, ...]
    abstain: bool
    gold_knowledge_keys: tuple[str, ...]
    no_answer: bool
    label_note: str
    label_source: str
    label_status: str
    human_reviewed: bool
    expected_analysis_status: str | None
    expected_permission: str | None
    expected_behavior: str | None
    raw: dict = field(repr=False)


@dataclass
class Dataset:
    path: Path
    schema_version: str
    dataset_version: str
    label_status: str
    label_source: str
    human_reviewed: bool
    description: str
    samples: list[Sample]
    knowledge: list[KnowledgeFixture]
    labels: dict[str, Label]
    behaviours: dict[str, str]
    file_hashes: dict[str, str]
    dataset_hash: str
    knowledge_hash: str
    behaviours_hash: str

    def sample(self, sample_id: str) -> Sample:
        for row in self.samples:
            if row.sample_id == sample_id:
                return row
        raise KeyError(sample_id)

    def samples_for(self, task: str) -> list[Sample]:
        return [s for s in self.samples if task in s.tasks]

    def knowledge_map(self) -> dict[str, KnowledgeFixture]:
        return {k.key: k for k in self.knowledge}


def _read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh), path.read_bytes()
    except FileNotFoundError as exc:
        raise DatasetError(f"缺少评测集文件: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise DatasetError(f"评测集文件不是合法 JSON: {path.name}") from exc


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _check_synthetic_text(value, where: str) -> None:
    """公开合成数据不得含个人目录/真实业务路径。"""
    if isinstance(value, str):
        lowered = value.lower()
        for marker in ("c:\\users\\", "c:/users/", "/home/", "\\users\\", "desktop\\mimo"):
            if marker in lowered:
                raise DatasetError(f"{where} 含疑似个人路径,拒绝作为公开评测集")
    elif isinstance(value, dict):
        for k, v in value.items():
            _check_synthetic_text(v, f"{where}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _check_synthetic_text(v, f"{where}[{i}]")


def load_dataset(path: str | Path) -> Dataset:
    """加载并严格校验版本化数据集;任何不一致都在运行前失败。"""
    directory = Path(path)
    if directory.is_file():
        directory = directory.parent
    manifest, manifest_raw = _read_json(directory / "manifest.json")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise DatasetError(
            f"数据集 schema 版本不匹配: {manifest.get('schema_version')!r} != {SCHEMA_VERSION!r}"
        )
    version = str(manifest.get("dataset_version") or "").strip()
    if not version:
        raise DatasetError("manifest 缺少 dataset_version")

    raw_files: dict[str, bytes] = {"manifest.json": manifest_raw}
    parsed: dict[str, object] = {"manifest.json": manifest}
    for name in ("knowledge.json", "samples.json", "labels.json", "synthetic_behaviours.json"):
        parsed[name], raw_files[name] = _read_json(directory / name)

    file_hashes = {name: _sha256(raw) for name, raw in sorted(raw_files.items())}
    dataset_hash = _sha256(
        "\n".join(f"{name}:{digest}" for name, digest in file_hashes.items()).encode("utf-8")
        + f"\nversion:{version}".encode("utf-8")
    )
    knowledge_hash = file_hashes["knowledge.json"]
    behaviours_hash = file_hashes["synthetic_behaviours.json"]

    knowledge_rows = parsed["knowledge.json"]
    sample_rows = parsed["samples.json"]
    label_rows = parsed["labels.json"]
    behaviour_doc = parsed["synthetic_behaviours.json"]
    if not isinstance(knowledge_rows, list) or not isinstance(sample_rows, list) or not isinstance(label_rows, list):
        raise DatasetError("knowledge/samples/labels 必须是数组")
    if not isinstance(behaviour_doc, dict) or not isinstance(behaviour_doc.get("behaviours"), dict):
        raise DatasetError("synthetic_behaviours.json 必须包含 behaviours 对象")

    knowledge: list[KnowledgeFixture] = []
    seen_keys: set[str] = set()
    doc_versions: dict[str, set[int]] = {}
    published_docs: set[str] = set()
    for i, row in enumerate(knowledge_rows):
        if not isinstance(row, dict):
            raise DatasetError(f"knowledge[{i}] 不是对象")
        key = str(row.get("key") or "").strip()
        kind = str(row.get("kind") or "").strip()
        status = str(row.get("status") or "").strip()
        if not key or key in seen_keys:
            raise DatasetError(f"知识键为空或重复: {key!r}")
        seen_keys.add(key)
        if kind not in KNOWLEDGE_STATUSES:
            raise DatasetError(f"{key}: kind 必须是 case|sop")
        if status not in KNOWLEDGE_STATUSES[kind]:
            raise DatasetError(f"{key}: 非法状态 {status!r}")
        category = row.get("category")
        if category not in _CATEGORY_NAMES:
            raise DatasetError(f"{key}: 分类 {category!r} 不在系统分类中")
        if not str(row.get("title") or "").strip():
            raise DatasetError(f"{key}: 缺少 title")
        version_value = None
        if kind == "sop":
            doc_key = str(row.get("doc_key") or "").strip()
            if not doc_key:
                raise DatasetError(f"{key}: SOP 缺少 doc_key")
            try:
                version_value = int(row.get("version"))
            except (TypeError, ValueError) as exc:
                raise DatasetError(f"{key}: SOP version 必须是正整数") from exc
            if version_value <= 0:
                raise DatasetError(f"{key}: SOP version 必须是正整数")
            if version_value in doc_versions.setdefault(doc_key, set()):
                raise DatasetError(f"{doc_key}: 版本 {version_value} 重复")
            doc_versions[doc_key].add(version_value)
            if status == "published":
                if doc_key in published_docs:
                    raise DatasetError(f"{doc_key}: 同一文档只能有一个 published 版本")
                published_docs.add(doc_key)
        _check_synthetic_text(row, key)
        knowledge.append(KnowledgeFixture(key=key, kind=kind, status=status, data=dict(row), version=version_value))

    label_map: dict[str, Label] = {}
    for i, row in enumerate(label_rows):
        if not isinstance(row, dict):
            raise DatasetError(f"labels[{i}] 不是对象")
        sid = str(row.get("sample_id") or "").strip()
        if not sid or sid in label_map:
            raise DatasetError(f"标签样本 ID 为空或重复: {sid!r}")
        allowed = tuple(str(x) for x in (row.get("allowed_categories") or []))
        expected = row.get("expected_category")
        if expected is not None and expected not in _CATEGORY_NAMES:
            raise DatasetError(f"{sid}: expected_category 不在系统分类中")
        for cat in allowed:
            if cat not in _CATEGORY_NAMES:
                raise DatasetError(f"{sid}: allowed_categories 含非法分类 {cat!r}")
        if bool(row.get("abstain")) and (expected is not None or allowed):
            raise DatasetError(f"{sid}: abstain 样本不能同时声明确定分类")
        if not row.get("label_status"):
            raise DatasetError(f"{sid}: 缺少 label_status")
        if row.get("human_reviewed") is True:
            # 本数据集明确是合成参考标签;若未来有人复核,必须换版本并更新 manifest。
            raise DatasetError(f"{sid}: 当前版本不支持 human_reviewed=true")
        label_map[sid] = Label(
            sample_id=sid,
            expected_category=expected,
            allowed_categories=allowed,
            abstain=bool(row.get("abstain")),
            gold_knowledge_keys=tuple(str(x) for x in (row.get("gold_knowledge_keys") or [])),
            no_answer=bool(row.get("no_answer")),
            label_note=str(row.get("label_note") or ""),
            label_source=str(row.get("label_source") or ""),
            label_status=str(row.get("label_status") or ""),
            human_reviewed=False,
            expected_analysis_status=(str(row["expected_analysis_status"]) if row.get("expected_analysis_status") else None),
            expected_permission=(str(row["expected_permission"]) if row.get("expected_permission") else None),
            expected_behavior=(str(row["expected_behavior"]) if row.get("expected_behavior") else None),
            raw=dict(row),
        )

    samples: list[Sample] = []
    seen_ids: set[str] = set()
    for i, row in enumerate(sample_rows):
        if not isinstance(row, dict):
            raise DatasetError(f"samples[{i}] 不是对象")
        sid = str(row.get("sample_id") or "").strip()
        if not sid or sid in seen_ids:
            raise DatasetError(f"样本 ID 为空或重复: {sid!r}")
        seen_ids.add(sid)
        tasks = tuple(str(t) for t in (row.get("tasks") or []))
        if not tasks or any(t not in TASKS for t in tasks):
            raise DatasetError(f"{sid}: tasks 非法 {tasks!r}")
        query = row.get("retrieval_query")
        input_data = row.get("input")
        if "retrieval" in tasks and not str(query or "").strip():
            raise DatasetError(f"{sid}: retrieval 缺少 retrieval_query")
        if any(t in tasks for t in ("analysis", "analysis_failure", "analysis_cache", "analysis_permission")):
            if not isinstance(input_data, dict) or not str(input_data.get("title") or "").strip():
                raise DatasetError(f"{sid}: analysis 缺少 input.title")
        if sid not in label_map:
            raise DatasetError(f"{sid}: 缺少参考标签")
        label = label_map[sid]
        for gold in label.gold_knowledge_keys:
            if gold not in seen_keys:
                raise DatasetError(f"{sid}: gold 知识键不存在 {gold!r}")
        if "retrieval" in tasks:
            if label.no_answer != (len(label.gold_knowledge_keys) == 0):
                raise DatasetError(f"{sid}: no_answer 与 gold 不一致")
        if "analysis" in tasks:
            if label.expected_permission:
                raise DatasetError(f"{sid}: 普通 analysis 不应声明 expected_permission")
            if not label.abstain and not label.allowed_categories:
                raise DatasetError(f"{sid}: 普通 analysis 缺少 allowed_categories")
            if label.abstain and label.gold_knowledge_keys:
                raise DatasetError(f"{sid}: abstain 样本不应声明 gold")
        _check_synthetic_text(row, sid)
        samples.append(
            Sample(
                sample_id=sid,
                group=str(row.get("group") or ""),
                tasks=tasks,
                retrieval_query=(str(query) if query is not None else None),
                input=dict(input_data or {}),
            )
        )

    missing_labels = seen_ids - set(label_map)
    orphan_labels = set(label_map) - seen_ids
    if missing_labels or orphan_labels:
        raise DatasetError(f"标签与样本不一致: missing={sorted(missing_labels)} orphan={sorted(orphan_labels)}")
    if int(manifest.get("sample_count", -1)) != len(samples):
        raise DatasetError("manifest.sample_count 与实际样本数不一致")
    if int(manifest.get("knowledge_count", -1)) != len(knowledge):
        raise DatasetError("manifest.knowledge_count 与实际知识数不一致")

    behaviours: dict[str, str] = {}
    for sid, behaviour in behaviour_doc["behaviours"].items():
        sid = str(sid)
        behaviour = str(behaviour)
        if sid not in seen_ids:
            raise DatasetError(f"synthetic_behaviours 指向未知样本 {sid}")
        if behaviour not in BEHAVIOURS:
            raise DatasetError(f"{sid}: 未知合成行为 {behaviour!r}")
        behaviours[sid] = behaviour
    for sample_row in samples:
        if "analysis_failure" in sample_row.tasks:
            behaviour = behaviours.get(sample_row.sample_id, "valid")
            if behaviour == "valid":
                raise DatasetError(f"{sample_row.sample_id}: failure 样本缺少合成故障行为")
        if "analysis_cache" in sample_row.tasks:
            behaviours.setdefault(sample_row.sample_id, "valid")

    return Dataset(
        path=directory,
        schema_version=str(manifest["schema_version"]),
        dataset_version=version,
        label_status=str(manifest.get("label_status") or ""),
        label_source=str(manifest.get("label_source") or ""),
        human_reviewed=bool(manifest.get("human_reviewed")),
        description=str(manifest.get("description") or ""),
        samples=samples,
        knowledge=knowledge,
        labels=label_map,
        behaviours=behaviours,
        file_hashes=file_hashes,
        dataset_hash=dataset_hash,
        knowledge_hash=knowledge_hash,
        behaviours_hash=behaviours_hash,
    )


def resolve_gold(
    dataset: Dataset, fixture_map: dict[str, dict]
) -> dict[str, list[dict]]:
    """把 gold 稳定键映射为实际来源类型/ID/版本;缺失夹具在运行前失败。"""
    resolved: dict[str, list[dict]] = {}
    for sample in dataset.samples:
        label = dataset.labels[sample.sample_id]
        rows = []
        for key in label.gold_knowledge_keys:
            fixture = fixture_map.get(key)
            if fixture is None:
                raise DatasetError(
                    f"{sample.sample_id}: gold 夹具 {key!r} 尚未准备;这是配置/准备错误,"
                    "不能当作无答案样本"
                )
            rows.append(
                {
                    "key": key,
                    "source_type": fixture["entity_type"],
                    "source_id": int(fixture["entity_id"]),
                    "version": fixture.get("version"),
                }
            )
        resolved[sample.sample_id] = rows
    return resolved
