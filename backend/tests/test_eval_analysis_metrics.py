"""第四阶段第二批:分析分类/弃答分母的手算回归。"""

from app.eval.analysis_runner import _metrics_for_quality


def _row(**overrides):
    base = {
        "is_quality": True,
        "abstain_expected": False,
        "category_correct": False,
        "valid_category": False,
        "business_status": "success",
        "http_ok": True,
        "suggested_category": None,
        "reference_mismatch": None,
        "category_bucket": "Kubernetes",
    }
    base.update(overrides)
    return base


def test_classification_denominator_includes_failures_and_no_suggestion():
    rows = [
        _row(category_correct=True, valid_category=True, suggested_category="Kubernetes"),
        _row(
            category_correct=False,
            valid_category=True,
            suggested_category="Linux",
            reference_mismatch=True,
        ),
        _row(category_correct=False, valid_category=False, suggested_category=None),
        _row(
            category_correct=False,
            valid_category=False,
            suggested_category="非法分类",
            reference_mismatch=True,
        ),
        _row(
            category_correct=False,
            http_ok=False,
            business_status=None,
            suggested_category=None,
        ),
        _row(
            category_correct=False,
            business_status="failed",
            suggested_category=None,
        ),
        _row(
            category_correct=True,
            business_status="incomplete",
            valid_category=True,
            suggested_category="Kubernetes",
        ),
        _row(
            abstain_expected=True,
            abstain_correct=True,
            suggested_category=None,
            category_bucket="abstain",
        ),
        _row(
            abstain_expected=True,
            abstain_correct=False,
            business_status="failed",
            suggested_category=None,
            category_bucket="abstain",
        ),
    ]
    m = _metrics_for_quality(rows)
    assert m["planned"] == 9
    assert m["non_abstain_planned"] == 7
    assert m["abstain_planned"] == 2
    # 正确:第 1 行 + 第 7 行(incomplete 但有有效分类);失败/无建议/非法分类不从分母删除
    assert m["accuracy"]["numerator"] == 2
    assert m["accuracy"]["denominator"] == 7
    assert m["accuracy"]["value"] == 0.2857
    assert m["suggestion_coverage"]["numerator"] == 3  # 第 1/2/7 行有有效建议
    assert m["suggestion_coverage"]["denominator"] == 7
    assert m["success_without_category"] == 1  # 第 3 行
    assert m["failed"] == 2  # 第 5/6 行
    assert m["incomplete"] == 1
    assert m["abstain_accuracy"]["numerator"] == 1
    assert m["abstain_accuracy"]["denominator"] == 2
    # 有非空建议的样本:第 1/2/4/7 行;其中 2/4 与允许集合不一致
    assert m["reference_label_mismatch"]["numerator"] == 2
    assert m["reference_label_mismatch"]["denominator"] == 4
    assert m["human_modification"]["status"] == "not_available"
    assert m["per_category"]["Kubernetes"] == {"planned": 7, "correct": 2}
