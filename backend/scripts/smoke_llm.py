"""模型适配层冒烟测试:验证真实接口连通性、鉴权、模型名与 JSON 模式。

用法(容器内,读取容器环境变量):
    docker exec -i mimo-backend-1 python - < backend/scripts/smoke_llm.py

判据(云端冒烟增量复核 2026-10-14 收紧):
- 真实接口冒烟仅允许 AI_MODE=cloud|local;demo/disabled 明确拒绝且 0 次调用,
  不计入真实联调;
- JSON 能力未开启(AI_SUPPORTS_JSON_MODE=0)时 JSON 项记「跳过 / 未验证」,
  不计入通过;
- JSON 项直接解析原始响应(不剥离 Markdown 围栏),必须是 JSON 对象,
  不接受列表 / 标量 / 围栏 / 前后杂文——只有裸 JSON 对象才算通过。

输出区分调用模式、已执行项目与未验证项目;不打印密钥。
usage 仅在服务真实返回时展示,否则显示「未返回」。
退出码:存在失败项为 1;仅跳过项(未验证)仍为 0,以输出中的状态为准,
不能只看退出码判断 JSON 模式已验证。
"""

import json
import sys

from app.ai.llm import ChatMessage, LLMError, LLMRequest, build_adapter
from app.config import Settings, get_settings

REAL_MODES = ("cloud", "local")

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_SKIPPED = "skipped"

_STATUS_LABEL = {STATUS_PASS: "通过", STATUS_FAIL: "失败", STATUS_SKIPPED: "跳过(未验证)"}
_SCOPE_LABEL = {STATUS_PASS: "已执行", STATUS_FAIL: "已执行", STATUS_SKIPPED: "未验证"}


def check_real_mode(settings: Settings) -> str | None:
    """真实接口冒烟模式守卫;不合法返回错误说明,合法返回 None。"""
    if settings.ai_mode not in REAL_MODES:
        return (
            f"真实接口冒烟仅允许 AI_MODE in {sorted(REAL_MODES)};"
            f"当前为 {settings.ai_mode!r},拒绝运行(demo/disabled 不计入真实联调)"
        )
    return None


def judge_json_raw(raw_text: str) -> tuple[bool, str]:
    """JSON 模式判据:直接解析原始响应,必须是裸 JSON 对象。

    不剥离 Markdown 围栏(围栏不能证明服务返回裸 JSON);不接受列表/标量/杂文。
    """
    try:
        data = json.loads(raw_text.strip())
    except ValueError:
        return False, "原始响应不是合法 JSON(按整段解析,不剥离围栏)"
    if not isinstance(data, dict):
        return False, f"原始响应是 {type(data).__name__},要求 JSON 对象"
    return True, "原始响应为裸 JSON 对象"


def _run_call(adapter, json_mode: bool, content: str, judge_json: bool = False) -> dict:
    name = "JSON 模式调用(json_mode=True)" if judge_json else "普通调用(不带 json_mode)"
    try:
        result = adapter.chat(
            LLMRequest(
                messages=[ChatMessage(role="user", content=content)],
                max_output_tokens=256,
                temperature=0.0,
                json_mode=json_mode,
            )
        )
    except LLMError as exc:
        return {
            "name": name,
            "status": STATUS_FAIL,
            "detail": f"code={exc.code} retryable={exc.retryable} message={exc.message}",
            "calls": 1,
        }
    item = {
        "name": name,
        "status": STATUS_PASS,
        "detail": f"model={result.model} latency={result.latency_ms}ms",
        "calls": 1,
        "usage": result.usage,  # None → 未返回,不虚构
        "reply": result.text.strip().replace("\n", " ")[:200],
        "raw_reply": result.text,
    }
    if judge_json:
        ok, detail = judge_json_raw(result.text)
        item["status"] = STATUS_PASS if ok else STATUS_FAIL
        item["detail"] = detail
    return item


def run_smoke(settings: Settings, adapter) -> dict:
    """执行冒烟并返回结构化结果(items 逐项 status: pass/fail/skipped)。

    模式守卫不通过时 0 次模型调用;不打印任何内容(展示由 format_report 负责)。
    """
    items: list[dict] = []
    result = {
        "mode": settings.ai_mode,
        "model": settings.ai_model or "(未配置)",
        "base_url": settings.ai_base_url or "(未配置)",
        "items": items,
        "ok": False,
    }
    guard = check_real_mode(settings)
    if guard:
        items.append({"name": "模式守卫", "status": STATUS_FAIL, "detail": guard, "calls": 0})
        return result

    items.append(_run_call(adapter, json_mode=False, content="请只回复两个字：正常"))

    if not settings.ai_supports_json_mode:
        items.append(
            {
                "name": "JSON 模式调用(json_mode=True)",
                "status": STATUS_SKIPPED,
                "detail": "AI_SUPPORTS_JSON_MODE=0:JSON 能力未开启,跳过(未验证)",
                "calls": 0,
            }
        )
    else:
        items.append(
            _run_call(
                adapter,
                json_mode=True,
                content='请只返回一个 JSON 对象,不要其他文字: {"ok": true, "lang": "zh"}',
                judge_json=True,
            )
        )

    result["ok"] = all(i["status"] != STATUS_FAIL for i in items) and any(
        i["status"] == STATUS_PASS for i in items
    )
    return result


def format_report(result: dict) -> str:
    """渲染报告:区分调用模式、已执行项目与未验证项目;不含密钥。"""
    lines = [
        f"调用模式: {result['mode']} | 模型: {result['model']} | 端点: {result['base_url']}",
    ]
    for idx, item in enumerate(result["items"], 1):
        scope = _SCOPE_LABEL[item["status"]]
        label = _STATUS_LABEL[item["status"]]
        lines.append(f"[{scope}] {idx}. {item['name']}: {label} — {item['detail']}")
        if "usage" in item:
            usage = item["usage"]
            lines.append(f"    usage={usage if usage is not None else '未返回'}")
        if "reply" in item:
            lines.append(f"    reply={item['reply']}")
    summary = "  ".join(
        f"{i['name'].split('(')[0]}={_STATUS_LABEL[i['status']]}" for i in result["items"]
    )
    verdict = "无失败项" if result["ok"] else "存在失败项"
    lines.append(f"结果: {summary} ({verdict})")
    return "\n".join(lines)


def main() -> int:
    settings = get_settings()
    try:
        settings.validate_ai_config()
    except ValueError as exc:
        print(f"配置错误: {exc}")
        return 2
    guard = check_real_mode(settings)
    if guard:
        # 明确拒绝,不构造/不调用任何适配器
        print(guard)
        return 2
    adapter = build_adapter(settings)
    print("adapter:", json.dumps(adapter.describe(), ensure_ascii=False))
    result = run_smoke(settings, adapter)
    print(format_report(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
