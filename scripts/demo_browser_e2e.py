# -*- coding: utf-8 -*-
"""demo 模式完整浏览器业务闭环(Playwright + 系统 Chrome)。

[注意] 会向目标环境写入带 [demo-e2e:<run_id>] 标记的真实业务材料(故障/案例/SOP),
请只指向隔离的演示环境。脚本不直接改库、不跳过业务流程审核/发布步骤。

前置(必须显式执行):
  1. 目标环境使用 AI_MODE=demo、EMBEDDING_MODE=disabled;
  2. 已执行后端演示内容初始化:
       docker compose exec backend python scripts/seed_demo_content.py
     或 python backend/scripts/seed_demo_content.py
  3. 提供已存在的运维/审核账号口令;可选提供管理员口令用于跨账号权限断言。

用法:
  export E2E_BASE_URL=http://localhost:8080
  export E2E_OPERATOR_USER=operator
  export E2E_OPERATOR_PASSWORD=...
  export E2E_REVIEWER_USER=reviewer
  export E2E_REVIEWER_PASSWORD=...
  # 可选:
  export E2E_ADMIN_USER=admin
  export E2E_ADMIN_PASSWORD=...
  python scripts/demo_browser_e2e.py

覆盖: 创建故障 → demo 分析(含一次失败不丢输入)→ 查看案例/SOP 来源并保留未保存输入
→ 补证再分析(新 ID/input_hash/记录证据)→ 人工确认解决 → 审核通过案例
→ AI 生成草稿(仍为 draft)→ 人工补全提交 → 审核发布 → 正式关键词检索定位新 SOP。

输出:
- 机器可读 JSON:scripts/e2e-artifacts/<run_id>/result.json(可用 E2E_RESULT_DIR 覆盖);
- 失败时保存截图与安全诊断(页面 URL/标题/控制台错误;不输出口令、Bearer 或完整输入);
- 存在失败项时退出码为 1;配置/前置不满足时为 2。

边界(如实标注):
- 模型输出全部来自 demo 适配器,不代表真实模型质量;
- EMBEDDING_MODE=disabled,检索方式为关键词检索,不是语义检索效果验证;
- 不验证 Linux 云部署、备份恢复、真实云模型或真实 embedding。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover - 环境问题
    print(
        "缺少 Playwright:请在运行脚本的环境安装 playwright 并准备系统 Chrome。\n"
        "  pip install playwright\n"
        "  python -m playwright install chrome  # 或使用已安装的 Google Chrome",
        file=sys.stderr,
    )
    raise SystemExit(2)


BASE = os.environ.get("E2E_BASE_URL", "http://localhost:8080").rstrip("/")
OP_USER = os.environ.get("E2E_OPERATOR_USER", "operator")
OP_PASS = os.environ.get("E2E_OPERATOR_PASSWORD")
RV_USER = os.environ.get("E2E_REVIEWER_USER", "reviewer")
RV_PASS = os.environ.get("E2E_REVIEWER_PASSWORD")
ADM_USER = os.environ.get("E2E_ADMIN_USER", "admin")
ADM_PASS = os.environ.get("E2E_ADMIN_PASSWORD")

RUN_ID = uuid.uuid4().hex[:10]
MARKER = f"[demo-e2e:{RUN_ID}]"
STARTED_AT = datetime.now(timezone.utc).isoformat()
ARTIFACT_DIR = Path(
    os.environ.get(
        "E2E_RESULT_DIR",
        str(Path(__file__).resolve().parent / "e2e-artifacts" / RUN_ID),
    )
)
RESULT_FILE = ARTIFACT_DIR / "result.json"

CHECKS: list[dict] = []
CONSOLE_ERRORS: list[str] = []
PAGE_ERRORS: list[str] = []
CREATED: dict[str, object] = {}
ENVIRONMENT: dict[str, object] = {}
SCREENSHOTS: list[str] = []


class E2EStop(Exception):
    """关键步骤失败,停止后续流程(仍输出已完成的检查与诊断)。"""


def require_credentials() -> None:
    missing = [
        name
        for name, value in [
            ("E2E_OPERATOR_PASSWORD", OP_PASS),
            ("E2E_REVIEWER_PASSWORD", RV_PASS),
        ]
        if not value
    ]
    if missing:
        print("缺少环境变量(脚本不内置口令): " + ", ".join(missing), file=sys.stderr)
        raise SystemExit(2)
    if not re.match(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", BASE) and os.environ.get(
        "E2E_ALLOW_REMOTE"
    ) != "1":
        print(
            f"拒绝向非本机地址写入演示材料: {BASE}。"
            "如确认是隔离演示环境,请显式设置 E2E_ALLOW_REMOTE=1。",
            file=sys.stderr,
        )
        raise SystemExit(2)


def check(name: str, cond: bool, detail: str = "") -> bool:
    status = "pass" if cond else "fail"
    CHECKS.append({"name": name, "status": status, "detail": detail if not cond else ""})
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if not cond and detail else ""))
    return cond


def skip(name: str, reason: str) -> None:
    CHECKS.append({"name": name, "status": "skipped", "detail": reason})
    print(f"  SKIP  {name}  {reason}")


def require(name: str, cond: bool, detail: str = "") -> None:
    if not check(name, cond, detail):
        raise E2EStop(name)


def field(scope, label: str):
    """按 el-form-item 标签定位输入控件。"""
    return scope.locator(
        f".el-form-item:has(> .el-form-item__label:text-is('{label}'))"
    ).locator("textarea, input").first


def login_form(page, username: str, password: str) -> None:
    page.get_by_placeholder("用户名").fill(username)
    page.get_by_placeholder("密码").fill(password)
    page.get_by_role("button", name="登 录").click()


def do_login(page, username: str, password: str) -> None:
    page.goto(BASE + "/login")
    login_form(page, username, password)
    page.wait_for_selector("text=故障处理", timeout=15000)


def get_token(page) -> str:
    token = page.evaluate("localStorage.getItem('opssop_token')")
    if not token:
        raise E2EStop("登录后未取得会话令牌")
    return token


def api(page, method: str, path: str, token: str | None, *, json_body=None, params=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body, ensure_ascii=False)
    resp = page.request.fetch(
        BASE + path, method=method, headers=headers, data=data, params=params
    )
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = None
    return resp, body


def api_ok(page, method: str, path: str, token: str | None, *, json_body=None, params=None):
    resp, body = api(page, method, path, token, json_body=json_body, params=params)
    if not resp.ok:
        raise E2EStop(f"{method} {path} 返回 HTTP {resp.status}: {body}")
    return body


def search_api(page, token: str, query: str) -> dict:
    return api_ok(
        page,
        "GET",
        "/api/search",
        token,
        params={"q": query, "keyword_only": "true"},
    )


def add_console_diagnostics(page) -> None:
    def on_console(msg):
        if msg.type == "error":
            text = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer ***", msg.text)
            CONSOLE_ERRORS.append(text[:300])

    def on_page_error(exc):
        PAGE_ERRORS.append(str(exc)[:300])

    page.on("console", on_console)
    page.on("pageerror", on_page_error)


def screenshot(page, label: str) -> str:
    try:
        path = ARTIFACT_DIR / f"{label}-{RUN_ID}.png"
        page.screenshot(path=str(path), full_page=True)
        SCREENSHOTS.append(str(path))
        return str(path)
    except Exception:  # noqa: BLE001
        return ""


def safe_diag(page) -> dict:
    try:
        return {
            "url": page.url,
            "title": page.title(),
            "console_errors": CONSOLE_ERRORS[-5:],
            "page_errors": PAGE_ERRORS[-5:],
        }
    except Exception:  # noqa: BLE001
        return {"console_errors": CONSOLE_ERRORS[-5:], "page_errors": PAGE_ERRORS[-5:]}


def write_result() -> dict:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    fails = [c for c in CHECKS if c["status"] == "fail"]
    skipped = [c for c in CHECKS if c["status"] == "skipped"]
    payload = {
        "run_id": RUN_ID,
        "marker": MARKER,
        "base_url": BASE,
        "started_at": STARTED_AT,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "environment": ENVIRONMENT,
        "summary": {
            "pass": len([c for c in CHECKS if c["status"] == "pass"]),
            "fail": len(fails),
            "skipped": len(skipped),
            "total": len(CHECKS),
        },
        "checks": CHECKS,
        "created": CREATED,
        "screenshots": SCREENSHOTS,
        "result_file": str(RESULT_FILE),
        "leftovers": {
            "note": "本轮创建的业务材料带运行标记保留在目标环境,便于人工复验;脚本不直接删库。",
            **{k: v for k, v in CREATED.items() if k.endswith("_id")},
        },
        "limitations": [
            "模型输出为 AI_MODE=demo 的模拟数据,不代表真实模型能力或质量。",
            "EMBEDDING_MODE=disabled:检索为关键词检索,不是语义/混合检索效果验证。",
            "本脚本不覆盖云部署、HTTPS、备份恢复、真实云模型与真实 embedding 效果。",
            "演示 seed 内容不是独立评测集。",
        ],
    }
    RESULT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def expect_popup_link(context, link):
    with context.expect_page(timeout=15000) as page_info:
        link.click()
    popup = page_info.value
    popup.wait_for_load_state("domcontentloaded")
    return popup


def main() -> int:
    require_credentials()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"目标: {BASE}")
    print(f"运行标记: {MARKER}")
    print(f"结果目录: {ARTIFACT_DIR}")
    print("[注意] 将向目标环境写入带运行标记的真实业务材料;不会直接改库跳过流程")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 950})
        page = context.new_page()
        add_console_diagnostics(page)
        main_page = page

        try:
            # ===== 0. 登录与前置检查 =====
            print("== 0. 登录与前置检查 ==")
            do_login(page, OP_USER, OP_PASS)
            token = get_token(page)

            ai_status = api_ok(page, "GET", "/api/system/ai-status", token)
            ENVIRONMENT.update(
                {
                    "ai_mode": ai_status.get("ai_mode"),
                    "embedding_mode": ai_status.get("embedding_mode"),
                    "model_config_version": ai_status.get("model_config_version"),
                }
            )
            require(
                "前置: AI_MODE=demo",
                ai_status.get("ai_mode") == "demo",
                f"当前 ai_mode={ai_status.get('ai_mode')!r};本脚本要求使用 demo",
            )
            require(
                "前置: EMBEDDING_MODE=disabled",
                ai_status.get("embedding_mode") == "disabled",
                f"当前 embedding_mode={ai_status.get('embedding_mode')!r};本脚本使用关键词检索",
            )

            pre_hits = search_api(page, token, "DEMO-OOMKILLED").get("hits", [])
            pre_case = next((h for h in pre_hits if h.get("source_type") == "case"), None)
            pre_sop = next((h for h in pre_hits if h.get("source_type") == "sop"), None)
            require(
                "前置: 演示 seed 已执行且含可定位案例/SOP",
                pre_case is not None and pre_sop is not None,
                "未找到 DEMO-OOMKILLED 正式知识;请先运行 backend/scripts/seed_demo_content.py",
            )
            check(
                "前置: 演示 seed 命中链接与版本字段有效",
                pre_case.get("link") == f"/cases/{pre_case.get('source_id')}"
                and pre_sop.get("link") == f"/sops/{pre_sop.get('source_id')}"
                and pre_sop.get("version") == 1,
                f"case={pre_case} sop={pre_sop}",
            )

            # ===== 1. 创建本轮唯一故障 =====
            print("== 1. 创建本轮唯一故障 ==")
            page.goto(BASE + "/faults")
            page.get_by_role("button", name="+ 新建故障").click()
            dlg = page.locator(".el-dialog")
            title = f"{MARKER} demo 闭环 checkout OOMKilled"
            field(dlg, "标题").fill(title)
            field(dlg, "详细描述").fill(
                f"{MARKER} checkout 服务 Pod 被 OOMKilled,重启后再次发生"
            )
            field(dlg, "服务").fill("checkout")
            field(dlg, "报错日志").fill(
                f"{MARKER} Reason: OOMKilled, Exit Code 137, DEMO-OOMKILLED"
            )
            page.get_by_role("button", name="创建并处理").click()
            page.wait_for_url(re.compile(r".*/faults/\d+"), timeout=15000)
            fault_url = page.url
            fault_id = int(fault_url.rsplit("/", 1)[-1])
            CREATED["fault_id"] = fault_id
            require("创建故障并进入详情", "/faults/" in fault_url, page.url)
            expect(page.get_by_text("AI 排查助手")).to_be_visible(timeout=15000)

            # 关闭自动分析,后续用显式按钮触发,避免与失败注入用例竞争
            switch = page.locator(".el-switch").first
            switch_input = switch.locator("input[role='switch']")
            if switch_input.count() and switch_input.get_attribute("aria-checked") == "true":
                switch.click()
                expect(switch_input).to_have_attribute("aria-checked", "false", timeout=5000)
            check(
                "已切换到手动分析,避免自动触发干扰",
                switch_input.count() > 0 and switch_input.get_attribute("aria-checked") == "false",
                "自动分析开关状态未知",
            )

            # ===== 2. 失败不丢输入 + 首次 demo 分析 =====
            print("== 2. 分析失败不丢输入 + demo 分析 ==")
            # 先记录已保存值,再输入与其完全不同的未保存值;仅检查 MARKER 子串会
            # 在“表单被重置为数据库已保存内容”时也通过。
            saved_desc = field(page, "详细描述").input_value()
            saved_logs = field(page, "报错日志").input_value()
            unsaved_desc = f"UNSAVED-DESC-{RUN_ID}-分析失败后必须保留"
            unsaved_logs = f"UNSAVED-LOG-{RUN_ID}-分析失败后必须保留"
            field(page, "详细描述").fill(unsaved_desc)
            field(page, "报错日志").fill(unsaved_logs)

            abort_count = {"n": 0}

            def abort_analyze(route):
                abort_count["n"] += 1
                route.abort()

            page.route("**/api/faults/*/analyze", abort_analyze)
            with page.expect_event(
                "requestfailed",
                predicate=lambda req: "/analyze" in req.url,
                timeout=15000,
            ):
                page.get_by_role("button", name="立即分析").click()
            expect(page.get_by_text(re.compile("分析失败")).first).to_be_visible(timeout=15000)
            # 等待失败处理完成(按钮从 loading 恢复)后再比较,避免过早断言
            expect(page.get_by_role("button", name="立即分析")).to_be_enabled(timeout=15000)
            check("分析失败请求只中止一次", abort_count["n"] == 1, str(abort_count["n"]))
            check(
                "分析失败后未保存描述精确保留",
                field(page, "详细描述").input_value() == unsaved_desc,
                field(page, "详细描述").input_value()[:80],
            )
            check(
                "分析失败后未保存日志精确保留",
                field(page, "报错日志").input_value() == unsaved_logs,
                field(page, "报错日志").input_value()[:80],
            )
            fault_after_analyze_fail = api_ok(page, "GET", f"/api/faults/{fault_id}", token)
            check(
                "分析失败后数据库仍为已保存值(未保存编辑未被写入)",
                fault_after_analyze_fail["description"] == saved_desc
                and fault_after_analyze_fail["error_logs"] == saved_logs,
                str(
                    {
                        "db_description": fault_after_analyze_fail["description"][:60],
                        "db_error_logs": fault_after_analyze_fail["error_logs"][:60],
                    }
                ),
            )
            page.unroute("**/api/faults/*/analyze")

            with page.expect_response(
                lambda r: "/analyze" in r.url and r.request.method == "POST", timeout=30000
            ) as resp_info:
                page.get_by_role("button", name="立即分析").click()
            analysis1 = resp_info.value.json()["analysis"]
            CREATED["analysis_id_1"] = analysis1["id"]
            require("demo 分析实际成功", analysis1.get("status") == "success", str(analysis1))
            check("demo 模拟标记可见", analysis1.get("is_demo") is True)
            check("demo 零模型调用(call_count=0)", analysis1.get("call_count") == 0)
            check("demo usage 未知(null),不虚构 token", analysis1.get("usage") is None)
            expect(page.get_by_text("演示数据 / 模拟分析").first).to_be_visible(timeout=10000)
            expect(page.get_by_text(re.compile("调用次数:0"))).to_be_visible(timeout=10000)
            expect(page.get_by_text("用量未知")).to_be_visible(timeout=10000)

            refs = (analysis1.get("report") or {}).get("references") or []
            ref_case = next((r for r in refs if r.get("source_type") == "case"), None)
            ref_sop = next((r for r in refs if r.get("source_type") == "sop"), None)
            require(
                "分析报告引用包含可定位案例与 SOP",
                ref_case is not None and ref_sop is not None,
                f"references={refs}",
            )
            check(
                "引用来源 ID 为受控正整数(前端据此构造地址)",
                all(
                    isinstance(r.get("source_id"), int) and r["source_id"] > 0
                    for r in refs
                ),
                str(refs),
            )

            # ===== 3. 从助手查看案例与 SOP,保留未保存输入 =====
            print("== 3. 来源定位 + 未保存输入保留 ==")
            unsaved = f"{MARKER} 未保存的已尝试操作-查看来源后仍在"
            field(page, "已尝试操作").fill(unsaved)

            case_link = page.locator(
                '[data-testid="reference-source-link"]', has_text="查看案例"
            ).first
            require("报告引用提供查看案例入口", case_link.count() > 0)
            popup = expect_popup_link(context, case_link)
            expect(popup).to_have_url(re.compile(r"/cases/\d+"), timeout=15000)
            expect(popup.get_by_text("案例详情")).to_be_visible(timeout=15000)
            expect(popup.get_by_text("已审核通过的正式诊断依据")).to_be_visible(timeout=10000)
            popup.reload()
            expect(popup.get_by_text("已审核通过的正式诊断依据")).to_be_visible(timeout=10000)
            popup_case_url = popup.url
            check("案例来源可刷新且状态提示正确", "/cases/" in popup_case_url, popup_case_url)
            popup.close()

            sop_link = page.locator(
                '[data-testid="reference-source-link"]', has_text="查看 SOP"
            ).first
            require("报告引用提供查看 SOP 入口", sop_link.count() > 0)
            popup = expect_popup_link(context, sop_link)
            expect(popup).to_have_url(re.compile(r"/sops/\d+"), timeout=15000)
            expect(popup.get_by_text("当前有效版本(已发布)")).to_be_visible(timeout=10000)
            popup.close()

            check(
                "查看来源后未保存的故障输入仍在",
                field(page, "已尝试操作").input_value() == unsaved,
            )

            # ===== 4. 补证后再分析:新 ID/input_hash/记录证据 =====
            print("== 4. 补证后再分析 ==")
            rec = page.get_by_placeholder("补充检查结果或处理动作…")
            record_text = f"{MARKER} 检查结果: DEMO-OOMKILLED memory 512Mi/512Mi"
            rec.fill(record_text)

            # 补证失败注入:请求中止后不得清空输入或覆盖未保存的故障编辑
            rec_abort_count = {"n": 0}

            def abort_records(route):
                rec_abort_count["n"] += 1
                route.abort()

            page.route("**/api/faults/*/records", abort_records)
            with page.expect_event(
                "requestfailed",
                predicate=lambda req: "/records" in req.url,
                timeout=15000,
            ):
                page.get_by_role("button", name="追加").click()
            # 等待 catch/finally 完成:追加按钮恢复、错误提示出现后再比较
            expect(page.get_by_role("button", name="追加")).to_be_enabled(timeout=15000)
            expect(page.locator(".el-message").first).to_be_visible(timeout=10000)
            check("补证失败请求只中止一次", rec_abort_count["n"] == 1, str(rec_abort_count["n"]))
            check(
                "补证失败后记录输入精确保留",
                rec.input_value() == record_text,
                rec.input_value()[:80],
            )
            check(
                "补证失败后未保存的已尝试操作精确保留",
                field(page, "已尝试操作").input_value() == unsaved,
                field(page, "已尝试操作").input_value()[:80],
            )
            fault_after_record_fail = api_ok(page, "GET", f"/api/faults/{fault_id}", token)
            check(
                "补证失败未新增记录",
                len(fault_after_record_fail["records"]) == 0,
                str(fault_after_record_fail["records"]),
            )
            check(
                "补证失败后未保存的表单编辑未写入数据库",
                fault_after_record_fail["description"] == saved_desc
                and fault_after_record_fail["error_logs"] == saved_logs
                and fault_after_record_fail["attempted_actions"] == "",
            )
            page.unroute("**/api/faults/*/records")

            with page.expect_response(
                lambda r: "/records" in r.url and r.request.method == "POST", timeout=15000
            ) as rec_info:
                page.get_by_role("button", name="追加").click()
            record_id = rec_info.value.json()["id"]
            CREATED["record_id"] = record_id
            expect(page.get_by_text("DEMO-OOMKILLED memory 512Mi/512Mi")).to_be_visible(
                timeout=10000
            )

            with page.expect_response(
                lambda r: "/analyze" in r.url and r.request.method == "POST", timeout=30000
            ) as resp_info:
                page.get_by_role("button", name="立即分析").click()
            analysis2 = resp_info.value.json()["analysis"]
            CREATED["analysis_id_2"] = analysis2["id"]
            require("补证后产生新的分析记录", analysis2["id"] != analysis1["id"])
            check("补证后 input_hash 变化", analysis2["input_hash"] != analysis1["input_hash"])
            facts = (analysis2.get("report") or {}).get("observed_facts") or []
            check(
                "新增记录证据 ID 出现在分析事实中",
                any(f"rec:{record_id}" in (f.get("evidence_ids") or []) for f in facts),
                str(facts),
            )
            check("补证分析仍为 demo 且零调用", analysis2.get("is_demo") is True and analysis2.get("call_count") == 0)

            # ===== 5. 人工确认解决并生成待审核案例 =====
            print("== 5. 人工确认解决 ==")
            manual_root = f"{MARKER} 人工确认根因:limits.memory 过低"
            manual_steps = f"{MARKER} 人工处理:调整 limits 并滚动重启"
            manual_recovery = f"{MARKER} 人工验证:30 分钟无重启"
            page.get_by_role("button", name="确认解决").click()
            rdlg = page.locator(".el-dialog")
            field(rdlg, "实际根因").fill(manual_root)
            field(rdlg, "处理步骤").fill(manual_steps)
            field(rdlg, "恢复验证").fill(manual_recovery)
            with page.expect_response(
                lambda r: "/resolve" in r.url and r.request.method == "POST", timeout=20000
            ) as resolve_info:
                page.get_by_role("button", name="确认解决并生成案例").click()
            require("确认解决请求成功", resolve_info.value.status == 200)
            expect(page.get_by_text("人工确认结果")).to_be_visible(timeout=15000)

            cases = api_ok(page, "GET", "/api/cases", token, params={"q": MARKER})
            require("人工确认后生成且仅生成一个本轮案例", len(cases) == 1, str(cases))
            case = cases[0]
            case_id = case["id"]
            CREATED["case_id"] = case_id
            check("案例处于待审核状态", case["status"] == "pending_review", case["status"])
            check("实际根因为人工填写内容", case["confirmed_root_cause"] == manual_root)
            check(
                "AI 候选原因未自动进入实际根因",
                "【模拟分析】" not in case["confirmed_root_cause"],
                case["confirmed_root_cause"],
            )
            check(
                "人工处理步骤与恢复验证未被 AI 覆盖",
                case["handling_steps"] == manual_steps
                and case["recovery_verification"] == manual_recovery,
            )

            # ===== 6. 审核账号实际通过案例 =====
            print("== 6. 审核案例 ==")
            do_login(page, RV_USER, RV_PASS)
            reviewer_token = get_token(page)
            page.goto(BASE + "/cases")
            page.get_by_placeholder("标题/现象/根因").fill(MARKER)
            page.get_by_role("button", name="查询").click()
            expect(page.get_by_text(title).first).to_be_visible(timeout=15000)
            page.get_by_text(title).first.click()
            drawer = page.locator(".el-drawer")
            expect(drawer.get_by_text("待审核").first).to_be_visible(timeout=10000)
            with page.expect_response(
                lambda r: "/approve" in r.url and "/cases/" in r.url and r.request.method == "POST",
                timeout=15000,
            ) as approve_info:
                drawer.get_by_role("button", name="通过").click()
            require("案例审核通过请求成功", approve_info.value.status == 200)
            expect(page.get_by_text("已通过").first).to_be_visible(timeout=15000)

            page.goto(BASE + f"/cases/{case_id}")
            expect(page.get_by_text("已审核通过的正式诊断依据")).to_be_visible(timeout=15000)
            check("审核人可直接打开已通过案例来源", "/cases/" in page.url, page.url)

            # ===== 7. 运维主动 AI 生成 SOP 草稿(仍为 draft) =====
            print("== 7. AI 生成 SOP 草稿 ==")
            do_login(page, OP_USER, OP_PASS)
            token = get_token(page)
            page.goto(BASE + "/cases")
            page.get_by_placeholder("标题/现象/根因").fill(MARKER)
            page.get_by_role("button", name="查询").click()
            expect(page.get_by_text(title).first).to_be_visible(timeout=15000)
            page.get_by_text(title).first.click()
            drawer = page.locator(".el-drawer")
            ai_btn = drawer.get_by_role("button", name="AI 生成草稿")
            expect(ai_btn).to_be_visible(timeout=10000)
            expect(ai_btn).to_be_enabled(timeout=10000)

            # 生成失败注入:不跳转、不创建草稿、案例上下文仍在
            page.route("**/api/cases/*/generate-sop-ai", lambda route: route.abort())
            with page.expect_event(
                "requestfailed",
                predicate=lambda req: "/generate-sop-ai" in req.url,
                timeout=15000,
            ):
                ai_btn.click()
            expect(drawer.get_by_text(re.compile("AI 生成失败")).first).to_be_visible(timeout=15000)
            check(
                "生成失败不跳转且案例上下文仍在",
                "/sops/" not in page.url and drawer.get_by_text(title).first.is_visible(),
                page.url,
            )
            page.unroute("**/api/cases/*/generate-sop-ai")

            ai_btn = drawer.get_by_role("button", name="AI 生成草稿")
            expect(ai_btn).to_be_enabled(timeout=10000)
            with page.expect_response(
                lambda r: "/generate-sop-ai" in r.url and r.request.method == "POST",
                timeout=30000,
            ) as gen_info:
                ai_btn.click()
            gen = gen_info.value.json()
            run = gen["run"]
            require("AI 生成业务成功", run.get("status") == "success", str(run))
            check("AI 生成仍为 demo 模拟", run.get("is_demo") is True)
            check("AI 生成零模型调用(call_count=0)", run.get("call_count") == 0)
            check("AI 生成 usage 未知(null)", run.get("usage") is None)
            require("AI 生成返回 draft", bool(gen.get("sop")), str(gen))
            sop_id = gen["sop"]["id"]
            CREATED["sop_id"] = sop_id
            check("AI 生成结果是草稿而非自动提交/发布", gen["sop"]["status"] == "draft", gen["sop"]["status"])
            page.wait_for_url(f"{BASE}/sops/{sop_id}", timeout=15000)
            expect(page.get_by_text("草稿").first).to_be_visible(timeout=10000)
            sop_now = api_ok(page, "GET", f"/api/sops/{sop_id}", token)
            check("刷新后 SOP 仍为草稿", sop_now["status"] == "draft", sop_now["status"])
            check("SOP 来源案例正确", sop_now["source_case_id"] == case_id)

            # ===== 8. 人工补全、提交审核、审核发布 =====
            print("== 8. 人工补全并发布 SOP ==")
            field(page, "排查步骤").fill(f"{MARKER} 人工核对后的排查步骤:检查 limits 与内存曲线")
            field(page, "处理方法").fill(f"{MARKER} 人工核对后的处理方法:调整 limits 并滚动重启")
            field(page, "恢复验证").fill(f"{MARKER} 人工核对后的恢复验证:30 分钟无重启")
            with page.expect_response(
                lambda r: "/submit" in r.url and r.request.method == "POST", timeout=20000
            ) as submit_info:
                page.get_by_role("button", name="提交审核").click()
            require("SOP 提交审核请求成功", submit_info.value.status == 200)
            expect(page.get_by_text("待审核").first).to_be_visible(timeout=15000)
            sop_after_submit = api_ok(page, "GET", f"/api/sops/{sop_id}", token)
            check("提交后状态为待审核", sop_after_submit["status"] == "submitted", sop_after_submit["status"])

            do_login(page, RV_USER, RV_PASS)
            reviewer_token = get_token(page)
            page.goto(BASE + f"/sops/{sop_id}")
            expect(page.get_by_text("待审核").first).to_be_visible(timeout=15000)
            with page.expect_response(
                lambda r: "/approve" in r.url and "/sops/" in r.url and r.request.method == "POST",
                timeout=15000,
            ) as publish_info:
                page.get_by_role("button", name="发布").click()
            require("SOP 发布请求成功", publish_info.value.status == 200)
            expect(page.get_by_text("当前有效版本(已发布)")).to_be_visible(timeout=15000)
            sop_published = api_ok(page, "GET", f"/api/sops/{sop_id}", reviewer_token)
            check("SOP 已发布且版本为 v1", sop_published["status"] == "published" and sop_published["version"] == 1)
            CREATED["sop_version"] = sop_published["version"]
            CREATED["sop_doc_id"] = sop_published["doc_id"]

            # ===== 9. 正式检索定位本轮新 SOP =====
            print("== 9. 正式检索定位 ==")
            do_login(page, OP_USER, OP_PASS)
            token = get_token(page)
            page.goto(BASE + f"/faults/{fault_id}")
            expect(page.get_by_text("知识检索")).to_be_visible(timeout=15000)
            search_input = page.get_by_placeholder("错误码 / 组件 / 现象关键词")
            search_input.fill(MARKER)
            with page.expect_response(
                lambda r: "/api/search" in r.url and r.request.method == "GET", timeout=15000
            ) as search_info:
                page.get_by_role("button", name="检索", exact=True).click()
            search_data = search_info.value.json()
            check(
                "检索方式如实为关键词",
                search_data.get("retrieval_mode") == "keyword",
                str(search_data.get("retrieval_mode")),
            )
            new_hit = next(
                (
                    h
                    for h in search_data.get("hits", [])
                    if h.get("source_type") == "sop" and h.get("source_id") == sop_id
                ),
                None,
            )
            require("正式检索返回本轮新发布 SOP", new_hit is not None, str(search_data.get("hits")))
            check(
                "检索定位到正确记录与版本",
                new_hit.get("version") == 1 and new_hit.get("link") == f"/sops/{sop_id}",
                str(new_hit),
            )
            expect(page.get_by_text("关键词检索").first).to_be_visible(timeout=10000)
            hit_link = page.locator(
                '[data-testid="search-hit-source-link"]', has_text="查看 SOP"
            ).first
            require("检索结果提供查看 SOP 入口", hit_link.count() > 0)
            hit_card = hit_link.locator("xpath=ancestor::div[contains(@class,'p-2')][1]")
            hit_card_text = hit_card.inner_text()
            check(
                "检索结果卡片显示本轮 SOP 标题",
                title in hit_card_text,
                hit_card_text[:120],
            )
            popup = expect_popup_link(context, hit_link)
            expect(popup).to_have_url(re.compile(rf"/sops/{sop_id}$"), timeout=15000)
            expect(popup.get_by_text("当前有效版本(已发布)")).to_be_visible(timeout=10000)
            check("检索入口定位到正确 SOP 页面", popup.url.endswith(f"/sops/{sop_id}"), popup.url)
            popup.close()

            # ===== 10. 负面来源与版本链(正式检索排除) =====
            print("== 10. 负面来源与版本链 ==")
            # 先确认夹具存在且状态正确,避免演示库不完整时 hits=[] 假通过
            neg_cases = api_ok(page, "GET", "/api/cases", token, params={"q": "DEMO-NEG-PENDING-CASE"})
            require(
                "负面夹具存在:待审核案例",
                any(
                    c["status"] == "pending_review" and "DEMO-NEG-PENDING-CASE" in c["title"]
                    for c in neg_cases
                ),
                str(neg_cases),
            )
            neg_cases = api_ok(page, "GET", "/api/cases", token, params={"q": "DEMO-NEG-RETURNED-CASE"})
            require(
                "负面夹具存在:已退回案例",
                any(
                    c["status"] == "returned" and "DEMO-NEG-RETURNED-CASE" in c["title"]
                    for c in neg_cases
                ),
                str(neg_cases),
            )
            for term, status in [
                ("DEMO-NEG-DRAFT-SOP", "draft"),
                ("DEMO-NEG-SUBMITTED-SOP", "submitted"),
                ("DEMO-NEG-DEPRECATED-SOP", "deprecated"),
            ]:
                rows = api_ok(page, "GET", "/api/sops", token, params={"q": term})
                require(
                    f"负面夹具存在:{status} SOP",
                    any(s["status"] == status and term in s["title"] for s in rows),
                    str(rows),
                )
            superseded_rows = api_ok(
                page, "GET", "/api/sops", token, params={"q": "DEMO-NEG-SUPERSEDED-SOP"}
            )
            require(
                "负面夹具存在:历史版本 SOP",
                any(
                    s["status"] == "superseded"
                    and s["version"] == 1
                    and "DEMO-NEG-SUPERSEDED-SOP" in s["title"]
                    for s in superseded_rows
                ),
                str(superseded_rows),
            )
            current_rows = api_ok(
                page, "GET", "/api/sops", token, params={"q": "DEMO-NEG-SUPERSEDED-CURRENT"}
            )
            require(
                "负面夹具存在:当前有效版本 v2",
                any(
                    s["status"] == "published"
                    and s["version"] == 2
                    and "DEMO-NEG-SUPERSEDED-CURRENT" in s["title"]
                    for s in current_rows
                ),
                str(current_rows),
            )
            for term in ("DEMO-NOMATCH-7K2P", "DEMO-INJECTION-Q9"):
                faults = api_ok(page, "GET", "/api/faults", token, params={"q": term})
                require(f"边界夹具存在:{term}", any(term in f["title"] for f in faults), str(faults))

            for term, label in [
                ("DEMO-NEG-PENDING-CASE", "待审核案例"),
                ("DEMO-NEG-RETURNED-CASE", "已退回案例"),
                ("DEMO-NEG-DRAFT-SOP", "草稿 SOP"),
                ("DEMO-NEG-SUBMITTED-SOP", "待审核 SOP"),
                ("DEMO-NEG-SUPERSEDED-SOP", "历史版本 SOP"),
                ("DEMO-NEG-DEPRECATED-SOP", "已废弃 SOP"),
                ("DEMO-NOMATCH-7K2P", "知识库无匹配"),
                ("DEMO-INJECTION-Q9", "提示词注入来源"),
            ]:
                hits = search_api(page, token, term).get("hits", [])
                check(f"负面来源不进入正式检索: {label}", hits == [], f"{term} -> {hits}")

            current_hits = search_api(page, token, "DEMO-NEG-SUPERSEDED-CURRENT").get("hits", [])
            current_sop = next((h for h in current_hits if h.get("source_type") == "sop"), None)
            require("历史文档仅返回当前有效版本", current_sop is not None and current_sop.get("version") == 2, str(current_hits))
            history = api_ok(page, "GET", f"/api/sops/{current_sop['source_id']}/history", token)
            v1 = next((h for h in history if h.get("version") == 1), None)
            require("历史版本仍可直达但标为历史", v1 is not None, str(history))
            hist_page = context.new_page()
            hist_page.goto(BASE + f"/sops/{v1['id']}")
            expect(hist_page.get_by_text("历史版本").first).to_be_visible(timeout=15000)
            check("历史版本页面保留正确状态提示", hist_page.url.endswith(f"/sops/{v1['id']}"), hist_page.url)
            hist_page.close()

            # ===== 11. 错误状态与登录回跳 =====
            print("== 11. 错误状态与登录回跳 ==")
            err_page = context.new_page()
            err_page.goto(BASE + "/cases/999999")
            expect(err_page.get_by_text("案例不存在或已被删除")).to_be_visible(timeout=15000)
            check("案例 404 状态清晰", "案例不存在或已被删除" in err_page.content())
            err_page.goto(BASE + "/cases/not-a-number")
            expect(err_page.get_by_text("来源地址无效")).to_be_visible(timeout=15000)
            check("案例非法 ID 状态清晰", "来源地址无效" in err_page.content())
            err_page.goto(BASE + "/sops/999999")
            expect(err_page.get_by_text("SOP 不存在或已被删除")).to_be_visible(timeout=15000)
            check("SOP 404 状态清晰", "SOP 不存在或已被删除" in err_page.content())
            err_page.goto(BASE + "/sops/not-a-number")
            expect(err_page.get_by_text("来源地址无效")).to_be_visible(timeout=15000)
            check("SOP 非法 ID 状态清晰", "来源地址无效" in err_page.content())
            err_page.close()

            login_ctx = browser.new_context()
            login_page = login_ctx.new_page()
            login_page.goto(BASE + f"/cases/{case_id}")
            expect(login_page).to_have_url(re.compile(r"/login\?redirect="), timeout=15000)
            login_form(login_page, OP_USER, OP_PASS)
            expect(login_page).to_have_url(re.compile(rf"/cases/{case_id}$"), timeout=15000)
            expect(login_page.get_by_text("已审核通过的正式诊断依据")).to_be_visible(timeout=15000)
            check("未登录直达来源后登录回到同一记录", login_page.url.endswith(f"/cases/{case_id}"), login_page.url)
            login_ctx.close()

            # ===== 12. 可选:跨账号权限边界(需管理员口令) =====
            print("== 12. 跨账号权限边界(可选) ==")
            if not ADM_PASS:
                skip(
                    "已审核他人案例可读/私有原始故障 403",
                    "未提供 E2E_ADMIN_PASSWORD;由后端 test_source_location.py 覆盖",
                )
            else:
                admin_ctx = browser.new_context()
                admin_page = admin_ctx.new_page()
                do_login(admin_page, ADM_USER, ADM_PASS)
                admin_token = get_token(admin_page)
                temp_name = f"e2e_op_{RUN_ID}"
                temp_pass = secrets.token_urlsafe(18)
                temp_user = None
                try:
                    resp, body = api(
                        admin_page,
                        "POST",
                        "/api/users",
                        admin_token,
                        json_body={
                            "username": temp_name,
                            "password": temp_pass,
                            "display_name": f"E2E 临时运维 {RUN_ID}",
                            "role": "operator",
                        },
                    )
                    require("创建本轮临时运维账号", resp.status == 201, f"HTTP {resp.status}: {body}")
                    temp_user = body
                    temp_ctx = browser.new_context()
                    temp_page = temp_ctx.new_page()
                    try:
                        do_login(temp_page, temp_name, temp_pass)
                        temp_token = get_token(temp_page)
                        r_case, _ = api(temp_page, "GET", f"/api/cases/{case_id}", temp_token)
                        check("他人已审核案例可读", r_case.status == 200, f"HTTP {r_case.status}")
                        r_fault, _ = api(temp_page, "GET", f"/api/faults/{fault_id}", temp_token)
                        check("未授权原始故障不可读(403)", r_fault.status == 403, f"HTTP {r_fault.status}")
                        pending = api_ok(
                            admin_page,
                            "GET",
                            "/api/cases",
                            admin_token,
                            params={"q": "DEMO-NEG-PENDING-CASE"},
                        )
                        if pending:
                            r_pending, _ = api(
                                temp_page, "GET", f"/api/cases/{pending[0]['id']}", temp_token
                            )
                            check(
                                "他人待审核案例不可读(403)",
                                r_pending.status == 403,
                                f"HTTP {r_pending.status}",
                            )
                        else:
                            skip("他人待审核案例不可读(403)", "演示 seed 中未找到待审核案例")
                    finally:
                        temp_ctx.close()
                finally:
                    if temp_user:
                        cleanup_resp, _ = api(
                            admin_page,
                            "PATCH",
                            f"/api/users/{temp_user['id']}",
                            admin_token,
                            json_body={"is_active": False},
                        )
                        check(
                            "本轮临时运维账号已停用(仅清理本轮资源)",
                            cleanup_resp.status == 200,
                            f"HTTP {cleanup_resp.status}",
                        )
                    admin_ctx.close()

            # ===== 完成 =====
            fails = [c for c in CHECKS if c["status"] == "fail"]
            if fails:
                screenshot(main_page, "final-failure")
            write_result()
            print()
            print(
                f"结果: pass={len([c for c in CHECKS if c['status'] == 'pass'])} "
                f"fail={len(fails)} skipped={len([c for c in CHECKS if c['status'] == 'skipped'])}"
            )
            print(f"JSON: {RESULT_FILE}")
            return 1 if fails else 0

        except E2EStop as exc:
            screenshot(main_page, "stopped")
            CHECKS.append(
                {
                    "name": f"流程中止: {exc}",
                    "status": "fail",
                    "detail": json.dumps(safe_diag(main_page), ensure_ascii=False),
                }
            )
            write_result()
            print(f"流程中止: {exc}", file=sys.stderr)
            print(f"JSON: {RESULT_FILE}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001
            shot = screenshot(main_page, "unexpected")
            CHECKS.append(
                {
                    "name": "脚本异常",
                    "status": "fail",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            write_result()
            print(f"脚本异常: {type(exc).__name__}: {exc}", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)
            print(f"截图: {shot}", file=sys.stderr)
            print(f"JSON: {RESULT_FILE}", file=sys.stderr)
            return 1
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
