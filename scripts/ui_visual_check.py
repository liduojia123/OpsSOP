# -*- coding: utf-8 -*-
"""OpsSOP 前端视觉与交互动效检查(Playwright + 系统 Chrome)。

只读取并操作目标环境中的演示数据;除一次可复原的 SOP 保存外不写入业务数据。
检查项、截图与录屏输出到 UI_ARTIFACT_DIR(默认 scripts/ui-artifacts/<run_id>)。

用法:
  export E2E_BASE_URL=http://127.0.0.1:28881        # 本地前端(代理 /api)
  export UI_OPERATOR_PASSWORD=...                   # 演示账号口令,不内置
  export UI_ADMIN_PASSWORD=...
  python scripts/ui_visual_check.py

边界:
- 视觉与动效检查,不替代后端权限/状态流转测试;
- 空状态与失败状态通过拦截接口响应构造,仅用于界面状态检查,不代表真实数据;
- 录屏为 Chromium 实时录制的 webm,不评估模型质量或云端部署。
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:28881").rstrip("/")
OP_USER = os.environ.get("UI_OPERATOR_USER", "operator")
OP_PASS = os.environ.get("UI_OPERATOR_PASSWORD", "")
ADM_USER = os.environ.get("UI_ADMIN_USER", "admin")
ADM_PASS = os.environ.get("UI_ADMIN_PASSWORD", "")
RV_USER = os.environ.get("UI_REVIEWER_USER", "reviewer")
RV_PASS = os.environ.get("UI_REVIEWER_PASSWORD", "")

RUN_ID = uuid.uuid4().hex[:8]
ARTIFACT_DIR = Path(
    os.environ.get(
        "UI_ARTIFACT_DIR",
        str(Path(__file__).resolve().parent / "ui-artifacts" / RUN_ID),
    )
)
SHOT_DIR = ARTIFACT_DIR / "screenshots"
MOTION_DIR = ARTIFACT_DIR / "motion"
RESULT_FILE = ARTIFACT_DIR / "result.json"

DESKTOP = {"width": 1440, "height": 1000}
NARROW = {"width": 390, "height": 844}

CHECKS: list[dict] = []
SHOTS: list[str] = []
VIDEOS: list[str] = []
CONSOLE_ERRORS: list[str] = []
PAGE_ERRORS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    CHECKS.append({"name": name, "status": "pass" if cond else "fail", "detail": detail if not cond else ""})
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if not cond and detail else ""))
    return cond


def info(name: str, detail: str) -> None:
    CHECKS.append({"name": name, "status": "info", "detail": detail})
    print(f"  INFO  {name}: {detail}")


def shot(page, name: str, full: bool = False) -> None:
    path = SHOT_DIR / f"{name}.png"
    page.screenshot(path=str(path), full_page=full)
    SHOTS.append(str(path.relative_to(ARTIFACT_DIR)).replace("\\", "/"))
    print(f"  shot  {name}")


def attach_diagnostics(page) -> None:
    def on_console(msg):
        if msg.type == "error" and "favicon" not in msg.text:
            CONSOLE_ERRORS.append(msg.text[:300])

    def on_error(exc):
        PAGE_ERRORS.append(str(exc)[:300])

    page.on("console", on_console)
    page.on("pageerror", on_error)


def login(page, user: str, password: str, target: str = "/login") -> None:
    page.goto(BASE + target)
    page.wait_for_timeout(450)
    page.get_by_placeholder("请输入用户名").fill(user)
    page.get_by_placeholder("请输入密码").fill(password)
    page.get_by_role("button", name="登 录").click()
    page.wait_for_selector(".ops-nav-link", timeout=20000)
    page.wait_for_timeout(1200)


def token_of(page) -> str:
    return page.evaluate("localStorage.getItem('opssop_token')") or ""


def parse_seconds(value: str) -> float:
    """把 '0.2s' / '200ms' 转为秒。"""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    out = 0.0
    for part in parts:
        match = re.match(r"([\d.]+)(m?s)$", part)
        if not match:
            continue
        num = float(match.group(1))
        out = max(out, num / 1000 if match.group(2) == "ms" else num)
    return out


def computed(locator, prop: str) -> str:
    return locator.evaluate("(el, p) => getComputedStyle(el).getPropertyValue(p)", prop)


def dismiss_messages(page) -> None:
    page.evaluate("() => document.querySelectorAll('.el-message').forEach((el) => el.remove())")


def run_checks(browser) -> None:
    print("== 交互与动效检查 ==")
    ctx = browser.new_context(viewport=DESKTOP)
    page = ctx.new_page()
    attach_diagnostics(page)
    login(page, OP_USER, OP_PASS)

    # 1. 工作台错峰入场
    page.goto(BASE + "/")
    page.wait_for_selector(".ops-reveal")
    page.wait_for_timeout(60)
    reveals = page.evaluate(
        """() => Array.from(document.querySelectorAll('.ops-reveal')).map((el) => {
            const s = getComputedStyle(el);
            return { delay: s.animationDelay, duration: s.animationDuration, name: s.animationName };
        })"""
    )
    ends = [parse_seconds(r["delay"]) + parse_seconds(r["duration"]) for r in reveals]
    staggered = len({round(parse_seconds(r["delay"]), 3) for r in reveals}) > 1
    check("工作台错峰入场元素存在", len(reveals) >= 8, f"count={len(reveals)}")
    check("工作台入场总时长不超过 400ms", bool(ends) and max(ends) <= 0.401, f"max_end={max(ends) if ends else 'n/a'}s")
    check("工作台入场存在错峰", staggered, "所有元素延迟相同")

    # 2. 导航悬停与页面切换
    nav = page.locator(".ops-nav-link", has_text="故障处理").first
    bg_before = computed(nav, "background-color")
    nav.hover()
    page.wait_for_timeout(220)
    bg_after = computed(nav, "background-color")
    check("导航悬停背景变化", bg_before != bg_after, f"{bg_before} -> {bg_after}")
    check("导航过渡时长 120–200ms", 0.1 <= parse_seconds(computed(nav, "transition-duration")) <= 0.21,
          computed(nav, "transition-duration"))

    page.evaluate(
        """() => {
            window.__pageTransitions = [];
            const main = document.querySelector('.ops-main');
            new MutationObserver((records) => {
                for (const r of records) {
                    for (const n of r.addedNodes) {
                        if (n.nodeType === 1 && typeof n.className === 'string' && n.className.includes('ops-page-enter')) {
                            window.__pageTransitions.push(n.className);
                        }
                    }
                    if (r.type === 'attributes' && r.attributeName === 'class') {
                        const c = r.target.className;
                        if (typeof c === 'string' && c.includes('ops-page-leave')) window.__pageTransitions.push(c);
                    }
                }
            }).observe(main, { attributes: true, subtree: true, attributeFilter: ['class'], childList: true });
        }"""
    )
    page.click(".ops-nav-link:has-text('案例库')")
    page.wait_for_timeout(700)
    seen = page.evaluate("window.__pageTransitions")
    check("路由切换使用进出场过渡", any("leave" in c for c in seen) and any("enter" in c for c in seen), str(seen)[:200])
    check("路由切换后到达目标页", page.url.endswith("/cases"), page.url)

    # 内容区独立滚动:切换页面后从顶部开始
    page.goto(BASE + "/faults")
    page.wait_for_selector(".ops-table-panel")
    page.locator(".ops-main").evaluate("el => { el.scrollTop = el.scrollHeight; }")
    page.wait_for_timeout(120)
    scrolled = page.locator(".ops-main").evaluate("el => el.scrollTop")
    page.click(".ops-nav-link:has-text('案例库')")
    page.wait_for_timeout(800)
    restored = page.locator(".ops-main").evaluate("el => el.scrollTop")
    check("切换页面后内容区从顶部开始", scrolled > 0 and restored == 0, f"{scrolled} -> {restored}")

    # 3. 知识条目悬停：箭头右移 3px
    page.goto(BASE + "/")
    page.wait_for_selector(".ops-sop-row")
    row = page.locator(".ops-sop-row").first
    arrow = row.locator(".ops-row-arrow")
    before = arrow.bounding_box()["x"]
    row.hover()
    page.wait_for_timeout(320)
    after = arrow.bounding_box()["x"]
    check("知识条目悬停箭头右移约 3px", 2.5 <= (after - before) <= 3.5, f"delta={after - before:.2f}px")
    row_bg = computed(row, "background-color")
    check("知识条目悬停背景变暖", row_bg not in ("rgb(255, 254, 250)", "rgba(0, 0, 0, 0)"), row_bg)

    # 4. 按钮与输入反馈
    page.goto(BASE + "/faults")
    page.wait_for_selector(".ops-filter-bar")
    query_btn = page.get_by_role("button", name="查询")
    qbg = computed(query_btn, "background-color")
    query_btn.hover()
    page.wait_for_timeout(180)
    check("按钮悬停背景变化", computed(query_btn, "background-color") != qbg,
          f"{qbg} -> {computed(query_btn, 'background-color')}")
    query_btn.hover()
    page.mouse.down()
    active_filter = computed(query_btn, "filter")
    page.mouse.up()
    check("按钮按下反馈", active_filter != "none", active_filter)

    keyword = page.get_by_placeholder("标题/描述/日志")
    keyword.focus()
    page.wait_for_timeout(180)
    wrapper = keyword.locator("xpath=..")
    shadow = wrapper.evaluate("el => getComputedStyle(el).boxShadow")
    check("输入框焦点环使用品牌色", "201, 75, 37" in shadow, shadow)
    check("输入框焦点过渡存在", parse_seconds(wrapper.evaluate("el => getComputedStyle(el).transitionDuration")) > 0.05,
          wrapper.evaluate("el => getComputedStyle(el).transitionDuration"))

    disabled_color = page.evaluate(
        """() => {
            const btn = document.querySelector('.ops-page-actions .el-button');
            btn.classList.add('is-disabled');
            const s = getComputedStyle(btn);
            const out = [s.opacity, s.cursor];
            btn.classList.remove('is-disabled');
            return out;
        }"""
    )
    check("禁用按钮有可辨识样式", float(disabled_color[0]) < 0.8, str(disabled_color))

    # 5. 弹窗动画与键盘关闭
    page.get_by_role("button", name="新建故障").click()
    overlay = page.locator(".el-overlay-dialog")
    overlay.wait_for(state="attached")
    dialog_anim = overlay.evaluate("el => [getComputedStyle(el).animationName, getComputedStyle(el).animationDuration]")
    check("弹窗使用淡入缩放动画", "dialog-fade-in" in dialog_anim[0], str(dialog_anim))
    check("弹窗动画时长 200–260ms", 0.19 <= parse_seconds(dialog_anim[1]) <= 0.27, str(dialog_anim))
    page.get_by_placeholder("简要描述故障现象").fill("动效检查:关闭弹窗后不应提交")
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    check("弹窗支持 Esc 关闭", page.locator(".el-dialog").is_hidden())
    check("弹窗关闭不提交表单", page.locator(".el-dialog").count() >= 0 and page.url.endswith("/faults"))

    # 6. 抽屉动画与键盘关闭
    page.goto(BASE + "/cases")
    page.wait_for_selector("table a")
    page.locator("table a").first.click()
    drawer = page.locator(".el-drawer")
    drawer.wait_for(state="visible")
    drawer_dur = drawer.evaluate("el => getComputedStyle(el).transitionDuration")
    check("抽屉过渡时长 200–260ms", 0.19 <= parse_seconds(drawer_dur) <= 0.27, drawer_dur)
    page.keyboard.press("Escape")
    page.wait_for_timeout(450)
    check("抽屉支持 Esc 关闭", not page.locator(".el-drawer").is_visible())

    # 7. AI 助手：真实加载、结果淡入、输入保留、过期标记
    page.goto(BASE + "/faults/16")
    page.wait_for_selector(".assistant-panel")
    desc = page.locator(".fault-evidence-form textarea").first
    desc.fill("动效检查保留输入:服务异常,缺少日志。")
    page.get_by_role("button", name="立即分析").click()
    loading_seen = False
    try:
        page.wait_for_selector("text=分析中", timeout=2500)
        loading_seen = True
    except Exception:  # noqa: BLE001 - 快速返回时可能错过加载态
        pass
    check("AI 分析展示真实加载状态", loading_seen)
    page.wait_for_selector(".assistant-report-body", timeout=30000)
    report_anim = page.locator(".assistant-report-body").evaluate("el => getComputedStyle(el).animationName")
    check("AI 结果返回后轻微淡入", report_anim == "ops-result-in", report_anim)
    check("演示结果标识可见", page.locator(".ops-note.is-demo").first.is_visible())
    check("分析请求不覆盖左侧输入", desc.input_value().startswith("动效检查保留输入"))
    desc.fill("动效检查保留输入:服务异常,缺少日志。追加变更")
    page.wait_for_timeout(500)
    stale_visible = page.locator(".el-alert--warning", has_text="输入已变更").first.is_visible()
    check("输入变更后旧报告标记过期", stale_visible)

    # 8. 减少动态效果
    rm_ctx = browser.new_context(viewport=DESKTOP, reduced_motion="reduce")
    rm_page = rm_ctx.new_page()
    login(rm_page, OP_USER, OP_PASS)
    rm_page.goto(BASE + "/")
    rm_page.wait_for_selector(".ops-reveal")
    rm_anim = computed(rm_page.locator(".ops-reveal").first, "animation-name")
    rm_nav = rm_page.locator(".ops-nav-link").first
    check("减少动态时关闭错峰入场", rm_anim == "none", rm_anim)
    check("减少动态时过渡接近 0", parse_seconds(computed(rm_nav, "transition-duration")) <= 0.001,
          computed(rm_nav, "transition-duration"))
    rm_card = rm_page.locator(".ops-sop-row").first
    if rm_card.count():
        rm_arrow = rm_card.locator(".ops-row-arrow")
        x1 = rm_arrow.bounding_box()["x"]
        rm_card.hover()
        rm_page.wait_for_timeout(250)
        x2 = rm_arrow.bounding_box()["x"]
        check("减少动态时取消箭头位移", abs(x2 - x1) < 0.5, f"delta={x2 - x1:.2f}px")
    rm_ctx.close()

    # 9. 连续导航不中断
    for label in ["故障处理", "案例库", "SOP 知识库", "工作台"]:
        page.click(f".ops-nav-link:has-text('{label}')")
        page.wait_for_timeout(45)
    page.wait_for_timeout(900)
    check("连续切换导航后停在最后一次目标", page.url.rstrip("/") == BASE, page.url)
    check("连续导航后页面内容可用", page.locator(".ops-metric-card").count() == 4)

    # 10. 保存防重复提交(可复原)
    page.goto(BASE + "/sops/10")
    page.wait_for_selector(".sop-edit-form")
    token = token_of(page)
    original = page.request.get(
        BASE + f"/api/sops/10", headers={"Authorization": f"Bearer {token}"}
    ).json()
    original_symptom = original.get("symptom", "")
    symptom_area = page.locator(".sop-edit-form textarea").first
    symptom_area.fill(original_symptom + "\n[动效检查临时文本]")
    patches = []
    page.on("request", lambda r: patches.append(r.url) if r.method == "PATCH" and "/api/sops/10" in r.url else None)
    page.get_by_role("button", name="保存").click()
    page.get_by_role("button", name="保存").dispatch_event("click")
    page.wait_for_timeout(1400)
    check("保存时重复点击只提交一次", len(patches) == 1, f"PATCH count={len(patches)}")
    current_sop = page.request.get(
        BASE + "/api/sops/10", headers={"Authorization": f"Bearer {token}"}
    ).json()
    if current_sop.get("symptom") == original_symptom + "\n[动效检查临时文本]":
        restored = page.request.patch(
            BASE + "/api/sops/10",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "X-Expected-Updated-At": current_sop["updated_at"]},
            data=json.dumps({"symptom": original_symptom}, ensure_ascii=False),
        )
        check("视觉检查恢复原始内容", restored.status == 200, f"HTTP {restored.status}")
    else:
        check("视觉检查恢复原始内容", current_sop.get("symptom") == original_symptom, "内容已变化，未覆盖")

    # 11. 未登录来源直达与只读边界
    direct = browser.new_context(viewport=DESKTOP)
    dpage = direct.new_page()
    attach_diagnostics(dpage)
    dpage.goto(BASE + "/cases/11")
    dpage.wait_for_url("**/login?redirect=**", timeout=15000)
    check("未登录直达来源时保留回跳地址", "/login?redirect=%2Fcases%2F11" in dpage.url or "redirect=/cases/11" in dpage.url, dpage.url)
    login(dpage, OP_USER, OP_PASS, target="/cases/11")
    check("登录后回到原来源页面", dpage.url.endswith("/cases/11"), dpage.url)
    check("来源直达页保持只读", dpage.locator("text=只读知识来源").first.is_visible()
          and dpage.get_by_role("button", name="修改根因").count() == 0)
    dpage.goto(BASE + "/cases/999999")
    dpage.wait_for_selector('[data-testid="case-detail-error"]')
    check("不存在的来源展示错误状态", "不存在" in dpage.locator('[data-testid="case-detail-error"]').inner_text())
    direct.close()

    # 12. 空状态、列表失败、AI 失败(拦截接口构造)
    page.goto(BASE + "/")
    page.wait_for_selector(".ops-metric-card")
    page.route(
        "**/api/workbench",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "my_open_faults": [],
                    "pending_case_reviews": [],
                    "pending_sop_reviews": [],
                    "recent_sops": [],
                    "published_sop_count": 0,
                    "category_stats": [
                        {"category_id": 1, "category_name": "Kubernetes", "count": 0},
                        {"category_id": 2, "category_name": "Linux", "count": 0},
                        {"category_id": 3, "category_name": "网络与 DNS", "count": 0},
                        {"category_id": 4, "category_name": "数据库与缓存", "count": 0},
                    ],
                    "my_returned_cases": [],
                    "my_returned_sops": [],
                },
                ensure_ascii=False,
            ),
        ),
    )
    page.reload()
    page.wait_for_selector("text=暂无已发布 SOP")
    page.wait_for_timeout(900)
    check("工作台空状态使用紧凑提示", page.locator(".ops-empty").count() >= 3)
    shot(page, "90-state-workbench-empty")
    page.unroute("**/api/workbench")

    page.route("**/api/sops*", lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail":"模拟失败"}'))
    page.goto(BASE + "/sops")
    page.wait_for_selector(".ops-load-error")
    page.wait_for_timeout(400)
    check("列表失败展示可重试的错误提示", page.locator(".ops-load-error .el-alert").is_visible())
    check("列表失败时不显示空表冒充结果", page.locator(".ops-table-panel").count() == 0)
    shot(page, "91-state-list-error")
    page.unroute("**/api/sops*")

    page.goto(BASE + "/faults/16")
    page.wait_for_selector(".assistant-panel")
    page.route("**/api/faults/16/analyze", lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail":"模拟模型失败"}'))
    page.locator(".fault-evidence-form textarea").first.fill("模型失败时保留的输入")
    page.get_by_role("button", name="立即分析").click()
    page.wait_for_selector("text=分析失败", timeout=10000)
    check("AI 失败展示原因与重试入口", page.get_by_role("button", name="重试").first.is_visible())
    check("AI 失败不清空左侧输入", page.locator(".fault-evidence-form textarea").first.input_value() == "模型失败时保留的输入")
    shot(page, "92-state-assistant-failure")
    page.unroute("**/api/faults/16/analyze")

    # 13. 横向溢出检查
    for path, label in [("/", "工作台"), ("/faults", "故障列表"), ("/faults/16", "故障详情"), ("/cases", "案例库"),
                        ("/cases/11", "案例详情"), ("/sops", "SOP 列表"), ("/sops/13", "SOP 详情")]:
        page.goto(BASE + path)
        page.wait_for_timeout(700)
        overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
        check(f"桌面无横向溢出:{label}", overflow <= 1, f"overflow={overflow}px")

    for size, prefix in [(NARROW, "窄屏")]:
        page.set_viewport_size(size)
        for path, label in [("/", "工作台"), ("/faults", "故障列表"), ("/faults/16", "故障详情"), ("/cases", "案例库"),
                            ("/cases/11", "案例详情"), ("/sops", "SOP 列表"), ("/sops/13", "SOP 详情")]:
            page.goto(BASE + path)
            page.wait_for_timeout(700)
            overflow = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
            check(f"{prefix}无横向溢出:{label}", overflow <= 1, f"overflow={overflow}px")
    page.set_viewport_size(DESKTOP)
    ctx.close()


def run_screenshots(browser) -> None:
    print("== 页面截图 ==")
    ctx = browser.new_context(viewport=DESKTOP)
    page = ctx.new_page()
    attach_diagnostics(page)

    page.goto(BASE + "/login")
    page.wait_for_timeout(900)
    shot(page, "01-login")

    login(page, OP_USER, OP_PASS)
    dismiss_messages(page)
    page.goto(BASE + "/")
    page.wait_for_selector(".ops-metric-card")
    page.wait_for_timeout(700)
    shot(page, "02-workbench")

    page.goto(BASE + "/faults")
    page.wait_for_timeout(800)
    shot(page, "03-fault-list")

    page.get_by_role("button", name="新建故障").click()
    page.wait_for_timeout(500)
    shot(page, "04-fault-create-dialog")
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)

    page.goto(BASE + "/faults/16")
    page.wait_for_timeout(900)
    shot(page, "05-fault-detail")
    page.get_by_role("button", name="立即分析").click()
    page.wait_for_selector(".assistant-report-body", timeout=30000)
    page.wait_for_timeout(500)
    shot(page, "06-fault-analysis")

    page.goto(BASE + "/faults/15")
    page.wait_for_timeout(900)
    page.locator(".fault-evidence-column").evaluate("el => { el.scrollTop = el.scrollHeight; }")
    page.wait_for_timeout(400)
    shot(page, "07-fault-resolved")

    page.goto(BASE + "/cases")
    page.wait_for_timeout(800)
    shot(page, "08-case-list")

    page.locator("table a").first.click()
    page.wait_for_timeout(600)
    shot(page, "09-case-drawer")
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)

    page.goto(BASE + "/cases/15")
    page.wait_for_timeout(800)
    shot(page, "10-case-source-detail")

    page.goto(BASE + "/cases/11")
    page.wait_for_timeout(800)
    shot(page, "11-case-source-returned")

    page.goto(BASE + "/sops")
    page.wait_for_timeout(800)
    shot(page, "12-sop-list")

    page.goto(BASE + "/sops/13")
    page.wait_for_timeout(800)
    shot(page, "13-sop-published")

    page.goto(BASE + "/sops/10")
    page.wait_for_timeout(800)
    shot(page, "14-sop-draft-edit")

    page.goto(BASE + "/sops/11")
    page.wait_for_timeout(800)
    shot(page, "15-sop-submitted")

    # 窄屏
    page.set_viewport_size(NARROW)
    page.goto(BASE + "/login")
    page.wait_for_timeout(700)
    shot(page, "30-narrow-login")
    login(page, OP_USER, OP_PASS)
    dismiss_messages(page)
    page.goto(BASE + "/")
    page.wait_for_timeout(700)
    shot(page, "31-narrow-workbench")
    page.goto(BASE + "/faults")
    page.wait_for_timeout(700)
    shot(page, "32-narrow-fault-list")
    page.get_by_role("button", name="新建故障").click()
    page.wait_for_timeout(500)
    shot(page, "33-narrow-fault-create")
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    page.goto(BASE + "/faults/16")
    page.wait_for_timeout(900)
    shot(page, "34-narrow-fault-detail")
    page.goto(BASE + "/cases")
    page.wait_for_timeout(700)
    page.locator("table a").first.click()
    page.wait_for_timeout(600)
    shot(page, "35-narrow-case-drawer")
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    page.goto(BASE + "/sops/13")
    page.wait_for_timeout(800)
    shot(page, "36-narrow-sop-detail")
    ctx.close()

    # 管理员
    adm_ctx = browser.new_context(viewport=DESKTOP)
    ap = adm_ctx.new_page()
    attach_diagnostics(ap)
    login(ap, ADM_USER, ADM_PASS)
    dismiss_messages(ap)
    ap.goto(BASE + "/admin")
    ap.wait_for_timeout(800)
    shot(ap, "16-admin-users")
    ap.get_by_role("tab", name="分类管理").click()
    ap.wait_for_timeout(600)
    shot(ap, "17-admin-categories")
    ap.get_by_role("tab", name="账号管理").click()
    ap.wait_for_timeout(500)
    ap.get_by_role("button", name="新建账号").click()
    ap.wait_for_timeout(500)
    shot(ap, "18-admin-user-dialog")
    adm_ctx.close()

    # 审核视角:待审核案例抽屉与待发布 SOP
    if RV_PASS:
        rv_ctx = browser.new_context(viewport=DESKTOP)
        rp = rv_ctx.new_page()
        attach_diagnostics(rp)
        login(rp, RV_USER, RV_PASS)
        dismiss_messages(rp)
        rp.goto(BASE + "/cases")
        rp.wait_for_timeout(700)
        rp.get_by_placeholder("标题/现象/根因").fill("PENDING")
        rp.get_by_role("button", name="查询").click()
        rp.wait_for_timeout(700)
        rp.locator("table a").first.click()
        rp.wait_for_timeout(700)
        rp.locator(".el-drawer__body").evaluate("el => { el.scrollTop = el.scrollHeight; }")
        rp.wait_for_timeout(400)
        shot(rp, "19-review-case-drawer")
        rp.goto(BASE + "/sops/11")
        rp.wait_for_timeout(800)
        shot(rp, "20-review-sop-submitted")
        rv_ctx.close()
    else:
        info("审核视角截图", "未提供 UI_REVIEWER_PASSWORD,跳过")

    # 窄屏管理员
    adm_narrow = browser.new_context(viewport=NARROW)
    anp = adm_narrow.new_page()
    login(anp, ADM_USER, ADM_PASS)
    anp.goto(BASE + "/admin")
    anp.wait_for_timeout(800)
    shot(anp, "37-narrow-admin")
    adm_narrow.close()


def run_videos(browser) -> None:
    print("== 动效录屏 ==")
    MOTION_DIR.mkdir(parents=True, exist_ok=True)
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 820},
        record_video_dir=str(MOTION_DIR),
        record_video_size={"width": 1280, "height": 820},
    )
    page = ctx.new_page()
    page.goto(BASE + "/login")
    page.wait_for_timeout(500)
    page.get_by_placeholder("请输入用户名").fill(OP_USER)
    page.get_by_placeholder("请输入密码").fill(OP_PASS)
    page.get_by_role("button", name="登 录").click()
    page.wait_for_selector(".ops-nav-link", timeout=20000)
    page.wait_for_timeout(900)
    page.goto(BASE + "/")
    page.wait_for_selector(".ops-metric-card")
    page.wait_for_timeout(900)
    page.locator(".ops-sop-row").first.hover()
    page.wait_for_timeout(700)
    page.locator(".ops-nav-link", has_text="案例库").hover()
    page.wait_for_timeout(700)
    page.locator(".ops-nav-link", has_text="案例库").click()
    page.wait_for_timeout(900)
    page.locator("table a").first.click()
    page.wait_for_timeout(1000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    page.goto(BASE + "/faults/16")
    page.wait_for_selector(".assistant-panel")
    page.wait_for_timeout(700)
    page.get_by_role("button", name="立即分析").click()
    page.wait_for_selector(".assistant-report-body", timeout=30000)
    page.wait_for_timeout(1200)
    page.locator(".ops-nav-link", has_text="工作台").click()
    page.wait_for_timeout(900)
    page.goto(BASE + "/faults")
    page.wait_for_timeout(500)
    page.get_by_role("button", name="新建故障").click()
    page.wait_for_timeout(900)
    page.keyboard.press("Escape")
    page.wait_for_timeout(700)
    video = page.video.path()
    ctx.close()
    target = MOTION_DIR / "ops-interaction-motion.webm"
    Path(video).replace(target)
    VIDEOS.append(str(target.relative_to(ARTIFACT_DIR)).replace("\\", "/"))
    print(f"  video  {target.name}")


def write_gallery() -> None:
    cards = []
    for rel in SHOTS:
        title = Path(rel).stem.replace("-", " ")
        cards.append(
            f'<figure><a href="{rel}" target="_blank"><img src="{rel}" alt="{title}" loading="lazy"></a>'
            f"<figcaption>{title}</figcaption></figure>"
        )
    videos = "".join(
        f'<figure class="video"><video controls preload="metadata" src="{v}"></video>'
        f'<figcaption>动效录屏 · <a href="{v}" target="_blank">{Path(v).name}</a></figcaption></figure>'
        for v in VIDEOS
    )
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>OpsSOP 前端重写截图</title>
<style>
body {{ margin: 0; padding: 32px; background: #f2f0e9; color: #232326;
  font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }}
h1 {{ font-size: 24px; letter-spacing: -.02em; }}
p {{ color: #77746e; font-size: 13px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 20px; margin-top: 24px; }}
figure {{ margin: 0; background: #fffefa; border: 1px solid #dedbd2; border-radius: 7px; overflow: hidden; }}
.video {{ max-width: 880px; margin: 0 0 24px; }}
img {{ display: block; width: 100%; height: auto; }}
figcaption {{ padding: 10px 12px; font-size: 12px; color: #77746e; border-top: 1px solid #e9e4d9; }}
video {{ width: 100%; }}
</style></head><body>
<h1>OpsSOP · 前端重写与交互动效</h1>
<p>桌面 1440×1000 / 窄屏 390×844；动效录屏为 Chromium 实际录制（webm）。</p>
{videos}
<div class="grid">{''.join(cards)}</div>
</body></html>"""
    (ARTIFACT_DIR / "gallery.html").write_text(html, encoding="utf-8")


def main() -> int:
    if not OP_PASS or not ADM_PASS:
        print("缺少 UI_OPERATOR_PASSWORD 或 UI_ADMIN_PASSWORD", file=sys.stderr)
        return 2
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        try:
            run_checks(browser)
            run_screenshots(browser)
            try:
                run_videos(browser)
            except Exception as exc:  # noqa: BLE001 - 录屏失败不阻断静态检查结果
                info("动效录屏", f"录屏未完成:{exc}")
        finally:
            browser.close()

    write_gallery()
    fails = [c for c in CHECKS if c["status"] == "fail"]
    payload = {
        "run_id": RUN_ID,
        "base_url": BASE,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "pass": len([c for c in CHECKS if c["status"] == "pass"]),
            "fail": len(fails),
            "info": len([c for c in CHECKS if c["status"] == "info"]),
            "total": len([c for c in CHECKS if c["status"] != "info"]),
        },
        "checks": CHECKS,
        "screenshots": SHOTS,
        "videos": VIDEOS,
        "console_errors": CONSOLE_ERRORS[-10:],
        "page_errors": PAGE_ERRORS[-10:],
        "limitations": [
            "空状态、列表失败与 AI 失败通过拦截接口响应构造,仅用于界面状态检查。",
            "动效检查基于 Chromium 计算样式与录屏,不代表其他浏览器的一致性。",
            "未执行后端权限/状态流转测试、真实模型调用或云端部署验证。",
        ],
    }
    RESULT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果: {payload['summary']}  文件: {RESULT_FILE}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())








