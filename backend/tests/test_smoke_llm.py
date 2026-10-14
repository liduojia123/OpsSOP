"""smoke_llm 通过判据回归(云端冒烟增量复核 2026-10-14)。

覆盖复核合成复现的三种伪通过场景:
1. AI_MODE=demo:必须明确拒绝且 0 次调用,不得计真实联调通过;
2. AI_SUPPORTS_JSON_MODE=0:JSON 项记「跳过 / 未验证」,不计入通过;
3. JSON 模式返回 [1,2] / 围栏 / 杂文:不是裸 JSON 对象,判失败。
输出需区分调用模式、已执行项目与未验证项目;不打印密钥。
全部使用假适配器,不发真实请求、不依赖数据库内容。
"""

import json

from app.ai.llm import LLMRequest, LLMResult
from app.config import Settings
from scripts.smoke_llm import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIPPED,
    check_real_mode,
    format_report,
    judge_json_raw,
    run_smoke,
)


class _FakeAdapter:
    """按队列返回预设响应;记录收到的请求(断言 json_mode 传递)。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[LLMRequest] = []

    def chat(self, request: LLMRequest) -> LLMResult:
        self.calls.append(request)
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResult(text=item, usage=None, model="fake-model", latency_ms=1)

    def describe(self) -> dict:
        return {"mode": "fake"}


def _settings(**kw) -> Settings:
    base = dict(
        ai_mode="cloud",
        ai_base_url="https://api.example.com/v1",
        ai_model="fake-model",
        ai_supports_json_mode=True,
    )
    base.update(kw)
    return Settings(**base)


# ---- 场景 1:demo/disabled 明确拒绝,0 次调用 ----


def test_smoke_rejects_demo_mode_zero_calls():
    adapter = _FakeAdapter(["不该被调用"])
    result = run_smoke(Settings(ai_mode="demo", ai_model="demo"), adapter)
    assert result["ok"] is False
    assert adapter.calls == []  # 真实远程调用为 0,模拟响应也不许走
    assert result["items"][0]["status"] == STATUS_FAIL
    assert "demo" in result["items"][0]["detail"]
    report = format_report(result)
    assert "普通调用" not in report and "JSON 模式" not in report  # 未发起任何调用项
    assert "失败" in report


def test_smoke_mode_guard_allows_only_cloud_and_local():
    assert check_real_mode(Settings(ai_mode="cloud", ai_base_url="https://x", ai_model="m")) is None
    assert check_real_mode(Settings(ai_mode="local", ai_base_url="http://127.0.0.1:1", ai_model="m")) is None
    for mode in ("demo", "disabled"):
        assert check_real_mode(Settings(ai_mode=mode)) is not None


# ---- 场景 2:JSON 能力关闭 → 跳过/未验证,不计入通过 ----


def test_smoke_json_capability_off_skips_not_pass():
    adapter = _FakeAdapter(["正常"])  # 只允许一次普通调用
    result = run_smoke(_settings(ai_supports_json_mode=False), adapter)
    plain, js = result["items"]
    assert plain["status"] == STATUS_PASS
    assert js["status"] == STATUS_SKIPPED
    assert "未验证" in js["detail"] or "跳过" in js["detail"]
    assert len(adapter.calls) == 1  # JSON 项不发请求
    assert adapter.calls[0].json_mode is False
    report = format_report(result)
    assert "未验证" in report  # 输出明确区分未验证项目
    assert "JSON 模式调用=通过" not in report  # 不得宣称 JSON 模式通过


# ---- 场景 3:JSON 模式响应必须是裸 JSON 对象 ----


def test_smoke_json_list_response_fails():
    adapter = _FakeAdapter(["正常", "[1,2]"])
    result = run_smoke(_settings(), adapter)
    js = result["items"][1]
    assert js["status"] == STATUS_FAIL
    assert "list" in js["detail"] or "对象" in js["detail"]
    assert result["ok"] is False
    assert adapter.calls[1].json_mode is True  # JSON 项带 json_mode 请求


def test_smoke_json_fenced_or_prose_fails():
    """不剥离围栏:```json 围栏与前后杂文都证明服务未返回裸 JSON 对象。"""
    for bad in ['```json\n{"ok": true}\n```', '结果如下 {"ok": true} 完毕', '{"ok": true} extra']:
        adapter = _FakeAdapter(["正常", bad])
        result = run_smoke(_settings(), adapter)
        assert result["items"][1]["status"] == STATUS_FAIL, bad
        assert result["ok"] is False


def test_smoke_json_bare_object_passes_and_scalar_fails():
    assert judge_json_raw('{"ok": true, "lang": "zh"}') == (
        True,
        "原始响应为裸 JSON 对象",
    )
    ok, detail = judge_json_raw("[1,2]")
    assert ok is False and "对象" in detail
    ok, _ = judge_json_raw('"just a string"')
    assert ok is False
    ok, _ = judge_json_raw("42")
    assert ok is False


def test_smoke_success_path_report_shape():
    """通过路径:输出含调用模式/已执行/usage 未返回标注,不虚构用量。"""
    adapter = _FakeAdapter(["正常", '{"ok": true}'])
    result = run_smoke(_settings(), adapter)
    assert [i["status"] for i in result["items"]] == [STATUS_PASS, STATUS_PASS]
    assert result["ok"] is True
    report = format_report(result)
    assert "调用模式: cloud" in report
    assert "[已执行] 1." in report and "[已执行] 2." in report
    assert "usage=未返回" in report  # 服务未返回 usage 时如实显示未返回
    assert "fake-model" in report
    # 报告不包含任何密钥字段
    assert "api_key" not in report.lower() or "has_api_key" not in report


def test_smoke_report_never_contains_key_material():
    """format_report 只输出约定字段;密钥不进入报告文本。"""
    result = run_smoke(
        _settings(ai_api_key="sk-SyntheticKeyDoNotLeak"),
        _FakeAdapter(["正常", '{"ok": true}']),
    )
    report = format_report(result)
    assert "sk-SyntheticKeyDoNotLeak" not in report
    assert "sk-SyntheticKeyDoNotLeak" not in json.dumps(
        [{k: v for k, v in i.items() if k != "raw_reply"} for i in result["items"]],
        ensure_ascii=False,
    )
