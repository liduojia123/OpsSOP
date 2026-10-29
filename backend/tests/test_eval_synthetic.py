"""第四阶段第二批:合成适配器协议、原始引用观察与故障行为。"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.ai.llm import ChatMessage, LLMRequest
from app.ai.schema import AnalysisReport
from app.eval.datasets import load_dataset
from app.eval.synthetic import (
    SyntheticChatAdapter,
    SyntheticEmbeddingAdapter,
    call_metadata,
    parse_evidence_ids,
    parse_prompt_refs,
)

DATASET_DIR = Path(__file__).resolve().parents[1] / "eval_data" / "v1"


def _prompt(sample_id: str) -> str:
    return (
        f"[EVAL:{sample_id}] synthetic prompt\n"
        "[desc] (description) OOMKilled memory limit\n"
        "[case:1] OOM case (source_type=case, source_id=1, version=null)\n"
        "[sop:2] OOM SOP (source_type=sop, source_id=2, version=1)\n"
    )


def _request(sample_id: str) -> LLMRequest:
    return LLMRequest(messages=[ChatMessage(role="user", content=_prompt(sample_id))])


def test_parse_prompt_refs_and_evidence():
    prompt = _prompt("S001")
    refs = parse_prompt_refs(prompt)
    assert refs == [
        {"ref_id": "case:1", "source_type": "case", "source_id": 1, "version": None},
        {"ref_id": "sop:2", "source_type": "sop", "source_id": 2, "version": 1},
    ]
    assert parse_evidence_ids(prompt) == ["desc"]


def test_valid_synthetic_output_is_schema_valid_and_uses_prompt_refs():
    ds = load_dataset(DATASET_DIR)
    adapter = SyntheticChatAdapter(ds)
    result = adapter.chat(_request("S001"))
    payload = json.loads(result.text)
    report = AnalysisReport.model_validate(payload)
    assert report.suggested_category == "Kubernetes"
    assert {r.ref_id for r in report.references} == {"case:1", "sop:2"}
    assert report.observed_facts[0].evidence_ids == ["desc"]
    assert result.usage == {"prompt_tokens": 12, "completion_tokens": 20, "total_tokens": 32}


def test_bad_json_and_schema_bad_behaviors_are_observable():
    ds = load_dataset(DATASET_DIR)
    bad = SyntheticChatAdapter(ds)
    first = bad.chat(_request("S032"))
    second = bad.chat(_request("S032"))
    assert first.text and second.text
    with pytest.raises(json.JSONDecodeError):
        json.loads(first.text)
    assert bad.calls[0].behavior == "bad_json"

    schema = SyntheticChatAdapter(ds)
    out = schema.chat(_request("S033"))
    with pytest.raises(ValidationError):
        AnalysisReport.model_validate(json.loads(out.text))
    assert schema.calls[0].behavior == "schema_bad"


def test_schema_repair_returns_invalid_then_valid():
    ds = load_dataset(DATASET_DIR)
    adapter = SyntheticChatAdapter(ds)
    first = adapter.chat(_request("S034"))
    second = adapter.chat(_request("S034"))
    with pytest.raises(ValidationError):
        AnalysisReport.model_validate(json.loads(first.text))
    valid = AnalysisReport.model_validate(json.loads(second.text))
    assert valid.suggested_category == "待分类"
    assert [c.call_index for c in adapter.calls] == [0, 1]
    # 修复请求即使最后一条消息是 feedback,也必须从第一条 user 消息识别样本
    repair_request = LLMRequest(
        messages=[
            ChatMessage(role="user", content=_prompt("S034")),
            ChatMessage(role="assistant", content=first.text),
            ChatMessage(role="user", content="请修正 Schema"),
        ]
    )
    third = adapter.chat(repair_request)
    assert AnalysisReport.model_validate(json.loads(third.text))
    assert adapter.calls[-1].sample_id == "S034"


def test_invalid_reference_and_source_invalidation_callbacks():
    ds = load_dataset(DATASET_DIR)
    adapter = SyntheticChatAdapter(ds)
    out = adapter.chat(_request("S035"))
    payload = json.loads(out.text)
    assert any(r["ref_id"] == "case:999999" for r in payload["references"])

    called = []
    adapter.before_return = lambda sid, refs: called.append((sid, refs))
    adapter.chat(_request("S037"))
    assert called and called[-1][0] == "S037"
    assert any(r["source_type"] == "sop" for r in called[-1][1])


def test_synthetic_embedding_and_metadata_do_not_expose_raw_text():
    adapter = SyntheticEmbeddingAdapter()
    result = adapter.embed(["OOMKilled 内存", "Redis 连接池"])
    assert result.dims == len(result.vectors[0])
    assert result.usage is None
    assert result.vectors[0] != result.vectors[1]

    chat = SyntheticChatAdapter(load_dataset(DATASET_DIR))
    chat.chat(_request("S001"))
    metadata = call_metadata(chat.calls)
    assert metadata[0]["sample_id"] == "S001"
    assert "raw_text" not in metadata[0]
    assert len(metadata[0]["raw_sha256"]) == 64
