"""第四阶段第二批:版本化评测集加载、稳定键与哈希测试。"""

from pathlib import Path

import pytest

from app.eval.datasets import DatasetError, load_dataset, resolve_gold

DATASET_DIR = Path(__file__).resolve().parents[1] / "eval_data" / "v1"


def test_dataset_version_counts_and_provisional_labels():
    ds = load_dataset(DATASET_DIR)
    assert ds.dataset_version == "2026-10-22.1"
    assert len(ds.samples) == 39
    assert len(ds.knowledge) == 25
    assert ds.label_status == "provisional"
    assert ds.human_reviewed is False
    assert len(ds.dataset_hash) == 64
    assert all(len(h) == 64 for h in ds.file_hashes.values())
    assert ds.samples_for("retrieval") and ds.samples_for("analysis")


def test_gold_keys_reference_existing_stable_knowledge():
    ds = load_dataset(DATASET_DIR)
    keys = {k.key for k in ds.knowledge}
    for sample in ds.samples:
        for gold in ds.labels[sample.sample_id].gold_knowledge_keys:
            assert gold in keys


def test_dataset_hash_is_stable_across_loads():
    a = load_dataset(DATASET_DIR)
    b = load_dataset(DATASET_DIR)
    assert a.dataset_hash == b.dataset_hash
    assert a.knowledge_hash == b.knowledge_hash
    assert a.behaviours_hash == b.behaviours_hash


def test_resolve_gold_requires_fixture_for_every_nonempty_gold():
    ds = load_dataset(DATASET_DIR)
    fixture_map = {
        k.key: {
            "entity_type": k.kind,
            "entity_id": index + 1,
            "version": k.version,
        }
        for index, k in enumerate(ds.knowledge)
    }
    resolved = resolve_gold(ds, fixture_map)
    assert set(resolved) == {s.sample_id for s in ds.samples}

    missing_key = next(iter(fixture_map))
    del fixture_map[missing_key]
    with pytest.raises(DatasetError, match="尚未准备|配置/准备错误"):
        resolve_gold(ds, fixture_map)


def test_dataset_contains_no_obvious_personal_paths():
    ds = load_dataset(DATASET_DIR)
    raw = (DATASET_DIR / "knowledge.json").read_text(encoding="utf-8")
    raw += (DATASET_DIR / "samples.json").read_text(encoding="utf-8")
    lowered = raw.lower()
    assert "c:\\users\\" not in lowered
    assert "/home/" not in lowered
