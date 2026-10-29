"""第四阶段第二批复审回归:引用分母、敏感元数据与最终引用有效性。

覆盖独立复审 P2 #1/#2/#6 的核心口径:
- 畸形引用必须进入分母,valid + invalid == occurrences;
- 不可信 ref_id 不原样进入报告;
- final 引用按数据库状态/版本计数,不能只凭清洗后总数推断全部有效;
- 没有有效 SOP 引用时缓存来源探测不适用,不能记为通过。
"""

import json
from types import SimpleNamespace

import pytest

from app.eval.analysis_runner import (
    _cache_source_probe,
    _final_ref_summary,
    _raw_ref_summary,
)
from app.models import CaseStatus, SopStatus


def _ref(ref_id="case:1", source_type="case", source_id=1, version=None):
    return {
        "ref_id": ref_id,
        "source_type": source_type,
        "source_id": source_id,
        "version": version,
    }


def test_malformed_reference_enters_denominator():
    valid = _ref()
    result = _raw_ref_summary({"references": [valid, None]}, [valid])
    assert result["occurrences"] == 2
    assert result["valid_occurrences"] == 1
    assert result["invalid_occurrences"] == 1
    assert result["valid_occurrences"] + result["invalid_occurrences"] == result["occurrences"]
    assert result["denominator_consistent"] is True
    assert result["valid_occurrences"] / result["occurrences"] == 0.5


def test_all_malformed_references_are_observable_and_counted():
    result = _raw_ref_summary(
        {"references": [None, 42, "not-an-object", {"source_type": "case"}]},
        [],
    )
    assert result["occurrences"] == 4
    assert result["valid_occurrences"] == 0
    assert result["invalid_occurrences"] == 4
    assert result["denominator_consistent"] is True
    assert result["invalid_reasons"]["malformed_reference"] == 3
    assert result["invalid_reasons"]["unknown_or_missing_ref_id"] == 1


def test_untrusted_unknown_reference_does_not_leak_raw_text():
    secret = "Authorization: Bearer SYNTHETIC-REF-SECRET"
    result = _raw_ref_summary(
        {
            "references": [
                {
                    "ref_id": secret,
                    "source_type": "case",
                    "source_id": 999,
                    "version": None,
                }
            ]
        },
        [],
    )
    rendered = json.dumps(result, ensure_ascii=False)
    assert "SYNTHETIC-REF-SECRET" not in rendered
    assert "Authorization" not in rendered
    assert result["occurrences"] == 1
    assert result["invalid_occurrences"] == 1
    assert result["untrusted_occurrences"] == 1
    assert result["occurrence_refs"][0].startswith("<untrusted:ref:str:len=")


def test_long_and_non_string_untrusted_refs_are_bounded():
    long_value = "x" * 50_000
    result = _raw_ref_summary(
        {
            "references": [
                {"ref_id": long_value, "source_type": "case", "source_id": 1},
                {"ref_id": {"nested": "secret"}, "source_type": "case", "source_id": 1},
            ],
            "hypotheses": [{"reference_ids": [12345, long_value]}],
        },
        [],
    )
    rendered = json.dumps(result, ensure_ascii=False)
    assert "x" * 1000 not in rendered
    assert all(len(ref) < 120 for ref in result["occurrence_refs"])
    assert result["occurrences"] == 4
    assert result["invalid_occurrences"] == 4
    assert result["denominator_consistent"] is True


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeDB:
    """按调用顺序返回列级行,模拟 _source_currently_valid 的 select(...).first()。"""

    def __init__(self, rows):
        self._rows = iter(rows)

    def execute(self, *_args, **_kwargs):
        return _FakeResult(next(self._rows))


def _final_report(refs):
    return {"references": refs, "hypotheses": []}


def test_final_reference_validity_uses_current_database_state():
    report = _final_report([_ref()])
    valid = _final_ref_summary(report, _FakeDB([(CaseStatus.approved,)]))
    assert valid["observable"] is True
    assert valid["occurrences"] == 1
    assert valid["valid_occurrences"] == 1
    assert valid["invalid_occurrences"] == 0
    assert valid["validity"]["numerator"] == 1
    assert valid["validity"]["denominator"] == 1
    assert valid["validity"]["value"] == 1.0

    invalid = _final_ref_summary(report, _FakeDB([(CaseStatus.pending_review,)]))
    assert invalid["valid_occurrences"] == 0
    assert invalid["invalid_occurrences"] == 1
    assert invalid["validity"]["value"] == 0.0
    assert invalid["invalid_reasons"]["source_not_current_valid"] == 1


def test_final_sop_reference_requires_published_and_matching_version():
    report = _final_report(
        [_ref("sop:2#v1", "sop", 2, 1), _ref("sop:3#v2", "sop", 3, 2)]
    )
    summary = _final_ref_summary(
        report,
        _FakeDB([(SopStatus.published, 1), (SopStatus.deprecated, 2)]),
    )
    assert summary["occurrences"] == 2
    assert summary["valid_occurrences"] == 1
    assert summary["invalid_occurrences"] == 1
    assert summary["validity"]["numerator"] == 1
    assert summary["validity"]["denominator"] == 2


@pytest.mark.parametrize(
    "reference",
    [
        {
            "ref_id": "case:1",
            "source_type": "sop",
            "source_id": 1,
            "version": 2,
        },
        {
            "ref_id": "sop:1#v1",
            "source_type": "sop",
            "source_id": 1,
            "version": 2,
        },
    ],
    ids=["type-conflict", "version-conflict"],
)
def test_final_reference_cannot_override_encoded_identity(reference):
    fake_db = SimpleNamespace(
        execute=lambda *_a, **_k: SimpleNamespace(
            first=lambda: (SopStatus.published, 2)
        )
    )
    result = _final_ref_summary(
        {"references": [reference], "hypotheses": []}, fake_db
    )
    assert result["invalid_occurrences"] == 1, result
    assert result["valid_occurrences"] == 0, result
    assert result["invalid_reasons"]


def test_final_reference_with_consistent_explicit_identity_is_valid():
    reference = {
        "ref_id": "sop:1#v2",
        "source_type": "sop",
        "source_id": 1,
        "version": 2,
    }
    fake_db = SimpleNamespace(
        execute=lambda *_a, **_k: SimpleNamespace(
            first=lambda: (SopStatus.published, 2)
        )
    )
    result = _final_ref_summary(
        {"references": [reference], "hypotheses": []}, fake_db
    )
    assert result["valid_occurrences"] == 1, result
    assert result["invalid_occurrences"] == 0, result


def test_final_reference_without_report_is_unobservable_not_100_percent():
    summary = _final_ref_summary(None, None)
    assert summary["observable"] is False
    assert summary["occurrences"] == 0
    assert summary["validity"]["status"] == "not_applicable"
    assert "不可观察" in summary["observation_reason"]


def test_cache_probe_without_sop_reference_is_not_applicable_not_passed():
    first = {
        "references": {
            "final": {
                "reference_details": [
                    {
                        "ref_id": "case:1",
                        "source_type": "case",
                        "source_id": 1,
                        "version": None,
                        "valid": True,
                    }
                ]
            }
        }
    }
    probe = _cache_source_probe(
        None,
        None,
        SimpleNamespace(sample_id="S_TEST"),
        SimpleNamespace(),
        1,
        SimpleNamespace(calls=[]),
        lambda: None,
        first,
    )
    assert probe["status"] == "not_applicable"
    assert probe["applicable"] is False
    assert probe["passed"] is None
    assert probe["api_requests_during_source_probe"] == 0
