# -*- coding: utf-8 -*-
"""浏览器完整业务闭环验证(Playwright + 系统 Chrome)。

[注意] 会向目标环境写入带 [br:<run_id>] 标记的测试材料,请指向独立测试环境。

用法(凭据不内置,须显式提供):
  export E2E_BASE_URL=http://localhost:8080          # 前端地址
  export E2E_OPERATOR_USER=operator
  export E2E_OPERATOR_PASSWORD=...
  export E2E_REVIEWER_USER=reviewer
  export E2E_REVIEWER_PASSWORD=...
  python scripts/browser_e2e.py

覆盖: 录入 -> 补证 -> 确认解决 -> 案例审核 -> SOP 发布 -> 新版本审核,
并专项验证:
  - 追加记录不清空未保存的表单编辑
  - 确认解决/提交审核前自动保存表单
  - SOP 历史版本切换 URL 与内容一致
  - reviewer 不显示写操作按钮(含未解决故障上的断言,防止恒真覆盖缺口)
  - 状态切换不清空未保存的描述/日志编辑(保留未提交编辑)
"""
import os
import re
import sys
import uuid

from playwright.sync_api import sync_playwright, expect

BASE = os.environ.get("E2E_BASE_URL", "http://localhost:8080")
OP_USER = os.environ.get("E2E_OPERATOR_USER", "operator")
OP_PASS = os.environ.get("E2E_OPERATOR_PASSWORD")
RV_USER = os.environ.get("E2E_REVIEWER_USER", "reviewer")
RV_PASS = os.environ.get("E2E_REVIEWER_PASSWORD")

RUN_ID = uuid.uuid4().hex[:8]
FAILS = []


def require_credentials():
    missing = [n for n, v in [("E2E_OPERATOR_PASSWORD", OP_PASS), ("E2E_REVIEWER_PASSWORD", RV_PASS)] if not v]
    if missing:
        print("缺少环境变量(脚本不内置示例口令): " + ", ".join(missing))
        sys.exit(2)


def check(name, cond, info=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {info}")
        FAILS.append(name)


def field(scope, label):
    """按 el-form-item 标签定位输入控件。"""
    return scope.locator(
        f".el-form-item:has(> .el-form-item__label:text-is('{label}'))"
    ).locator("textarea, input").first


def do_login(page, username, password):
    page.goto(BASE + "/login")
    page.get_by_placeholder("用户名").fill(username)
    page.get_by_placeholder("密码").fill(password)
    page.get_by_role("button", name="登 录").click()
    # 以侧栏导航出现作为登录完成信号(等 URL 容易与 /login 混淆)
    page.wait_for_selector("text=故障处理", timeout=15000)


def main():
    require_credentials()
    marker = f"[br:{RUN_ID}]"
    print(f"目标: {BASE}  运行标记: {marker}")
    print("[注意] 将向目标环境写入带运行标记的测试材料")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 950})
        page = ctx.new_page()

        # ========== 1. 运维登录并录入故障 ==========
        print("== 1. 运维登录 + 录入故障 ==")
        do_login(page, OP_USER, OP_PASS)
        check("登录后进入工作台", "工作台" in page.content())

        page.goto(BASE + "/faults")
        page.get_by_role("button", name="+ 新建故障").click()
        dlg = page.locator(".el-dialog")
        desc_marker = f"{marker} 未保存的描述编辑-不得丢失"
        field(dlg, "标题").fill(f"{marker} 计费服务内存泄漏")
        field(dlg, "详细描述").fill("pod 内存持续上涨被 OOMKilled")
        field(dlg, "服务").fill("billing")
        page.get_by_role("button", name="创建并处理").click()
        page.wait_for_url(re.compile(r".*/faults/\d+"), timeout=10000)
        fault_url = page.url
        check("创建故障并进入详情", "/faults/" in fault_url)

        # ========== 2. 补证:未保存编辑不被追加记录清空(P1 #5) ==========
        print("== 2. 补证 + 未保存编辑保留(P1 #5)==")
        desc = field(page, "详细描述")
        desc.fill(desc_marker)  # 故意不点保存
        rec = page.get_by_placeholder("补充检查结果或处理动作…")
        rec.fill(f"{marker} kubectl top 显示内存接近 limits")
        page.get_by_role("button", name="追加").click()
        page.wait_for_timeout(1200)
        val = field(page, "详细描述").input_value()
        check("追加记录后未保存的描述编辑仍在", desc_marker in val, f"got={val[:80]}")
        check("追加的记录出现在列表", marker in page.content())

        # ========== 3. 确认解决:先保存表单再解决(P1 #5) ==========
        print("== 3. 确认解决前自动保存(P1 #5)==")
        page.get_by_role("button", name="确认解决").click()
        rdlg = page.locator(".el-dialog")
        field(rdlg, "实际根因").fill("limits.memory 设置过低")
        field(rdlg, "处理步骤").fill("调整 limits 并重启部署")
        field(rdlg, "恢复验证").fill("稳定运行 30 分钟无重启")
        page.get_by_role("button", name="确认解决并生成案例").click()
        page.wait_for_timeout(1500)
        body = page.content()
        check("已解决并展示人工确认结果", "人工确认结果" in body and "已解决" in body)
        val = field(page, "详细描述").input_value()
        check("确认解决后描述保留未保存编辑(已自动保存)", desc_marker in val, f"got={val[:80]}")

        # ========== 4. 审核人通过案例 ==========
        print("== 4. 案例审核 ==")
        do_login(page, RV_USER, RV_PASS)
        page.goto(BASE + "/cases")
        page.get_by_text(f"{marker} 计费服务内存泄漏").first.click()
        drawer = page.locator(".el-drawer")
        page.wait_for_timeout(800)
        check("审核人看到待审核案例", "待审核" in drawer.inner_text())
        drawer.get_by_role("button", name="通过").click()
        page.wait_for_timeout(1200)
        check("案例审核通过", "已通过" in page.content())

        # ========== 5. 运维生成 SOP 并提交(提交前自动保存,P1 #5) ==========
        print("== 5. SOP 草稿 -> 提交(提交前自动保存)==")
        do_login(page, OP_USER, OP_PASS)
        page.goto(BASE + "/cases")
        page.get_by_text(f"{marker} 计费服务内存泄漏").first.click()
        drawer = page.locator(".el-drawer")
        page.wait_for_timeout(800)
        drawer.get_by_role("button", name="模板生成草稿").click()
        page.wait_for_url(re.compile(r".*/sops/\d+"), timeout=15000)
        sop_v1_url = page.url
        sop_marker = f"{marker} SOP-未保存-处理方法编辑"
        field(page, "排查步骤").fill("1. kubectl describe pod\n2. 检查 limits")
        field(page, "处理方法").fill(sop_marker)  # 故意不点保存
        field(page, "恢复验证").fill("监控内存稳定")
        page.get_by_role("button", name="提交审核").click()
        page.wait_for_timeout(1500)
        check("SOP 已提交审核", "待审核" in page.content())
        val = field(page, "处理方法").input_value()
        check("提交前自动保存了未保存的处理方法", sop_marker in val, f"got={val[:80]}")

        # ========== 6. 审核人发布 SOP ==========
        print("== 6. SOP 发布 ==")
        do_login(page, RV_USER, RV_PASS)
        page.goto(sop_v1_url)
        page.wait_for_timeout(800)
        page.get_by_role("button", name="发布").click()
        page.wait_for_timeout(1500)
        check("SOP 发布成功", "已发布" in page.content())

        # ========== 7. 新版本 + 历史版本切换(P1 #6 专项) ==========
        print("== 7. 新版本 + 历史版本切换(P1 #6)==")
        do_login(page, OP_USER, OP_PASS)
        page.goto(sop_v1_url)
        page.wait_for_timeout(800)
        # 从 new-version 响应取新 ID,精确等待导航(宽泛正则会先匹配当前 URL)
        with page.expect_response(
            lambda r: "/new-version" in r.url and r.request.method == "POST", timeout=15000
        ) as resp_info:
            page.get_by_role("button", name="修改(生成新版本)").click()
        new_id = resp_info.value.json()["id"]
        page.wait_for_url(f"{BASE}/sops/{new_id}", timeout=15000)
        page.wait_for_timeout(1000)
        sop_v2_url = f"{BASE}/sops/{new_id}"
        check("新版本 URL 已切换", sop_v2_url != sop_v1_url, f"{sop_v2_url} == {sop_v1_url}")
        check("页面显示 v2 草稿", "v2" in page.content() and "草稿" in page.content())

        v2_marker = f"{marker} v2-排查步骤"
        field(page, "排查步骤").fill(v2_marker)
        page.get_by_role("button", name="保存").click()
        page.wait_for_timeout(1200)

        # 切到 v1:URL 与内容必须一致(修复前内容停留在 v2)
        v1_id = sop_v1_url.rsplit("/", 1)[-1]
        v2_id = sop_v2_url.rsplit("/", 1)[-1]
        page.locator(".el-card, .page-card").filter(has_text="版本历史").get_by_text("v1", exact=True).click()
        page.wait_for_url(re.compile(rf".*/sops/{v1_id}$"), timeout=10000)
        page.wait_for_timeout(1200)
        val = field(page, "排查步骤").input_value()
        check("切到 v1:URL 指向 v1", page.url.endswith(f"/sops/{v1_id}"), page.url)
        check("切到 v1:内容为 v1(不含 v2 编辑)", v2_marker not in val and "kubectl" in val, f"got={val[:60]}")
        check("切到 v1:标题栏显示 v1", "v1" in page.content())

        # 切回 v2:内容必须跟随
        page.locator(".el-card, .page-card").filter(has_text="版本历史").get_by_text("v2", exact=True).click()
        page.wait_for_url(re.compile(rf".*/sops/{v2_id}$"), timeout=10000)
        page.wait_for_timeout(1200)
        val = field(page, "排查步骤").input_value()
        check("切回 v2:URL 与内容一致", page.url.endswith(f"/sops/{v2_id}") and v2_marker in val, f"got={val[:60]}")

        # v2 提交审核
        page.get_by_role("button", name="提交审核").click()
        page.wait_for_timeout(1500)
        check("v2 提交审核成功", "待审核" in page.content())

        # ========== 8. 审核人发布 v2,旧版转历史 ==========
        print("== 8. v2 发布,旧版转历史 ==")
        do_login(page, RV_USER, RV_PASS)
        page.goto(sop_v2_url)
        page.wait_for_timeout(800)
        page.get_by_role("button", name="发布").click()
        page.wait_for_timeout(1500)
        check("v2 发布成功", "已发布" in page.content())
        page.goto(sop_v1_url)
        page.wait_for_timeout(1000)
        check("v1 自动转为历史版本", "历史版本" in page.content())

        # ========== 9. reviewer 无写操作按钮(权限一致) ==========
        print("== 9. reviewer 只读视图 ==")
        page.goto(fault_url)
        page.wait_for_timeout(1000)
        n_save = page.get_by_role("button", name="保存", exact=True).count()
        n_append = page.get_by_role("button", name="追加", exact=True).count()
        n_resolve = page.get_by_role("button", name="确认解决", exact=True).count()
        check(
            "reviewer 不显示保存/追加/确认解决按钮(已解决故障)",
            n_save + n_append + n_resolve == 0,
            f"save={n_save} append={n_append} resolve={n_resolve}",
        )

        # 覆盖缺口回归:按钮断言必须在「未解决」故障上也成立——
        # 已解决故障对所有角色都隐藏写按钮,单查已解决无法证明角色权限一致。
        print("== 9b. reviewer 在未解决故障上仍无写按钮(覆盖缺口回归)==")
        do_login(page, OP_USER, OP_PASS)
        page.goto(BASE + "/faults")
        page.get_by_role("button", name="+ 新建故障").click()
        dlg = page.locator(".el-dialog")
        open_title = f"{marker} 未解决故障-权限断言"
        field(dlg, "标题").fill(open_title)
        field(dlg, "详细描述").fill("仅用于 reviewer 按钮断言")
        page.get_by_role("button", name="创建并处理").click()
        page.wait_for_url(re.compile(r".*/faults/\d+"), timeout=10000)
        open_fault_url = page.url
        # 等待详情页实际加载完成(标题出现),避免在加载中统计按钮造成假失败/假通过
        expect(page.locator("h1")).to_have_text(open_title, timeout=10000)
        # 运维本人在未解决故障上应看到写按钮(对照组,防止断言恒真)
        n_save = page.get_by_role("button", name="保存", exact=True).count()
        check("对照: 运维在未解决故障上可见保存按钮", n_save > 0, f"save={n_save}")

        do_login(page, RV_USER, RV_PASS)
        page.goto(open_fault_url)
        expect(page.locator("h1")).to_have_text(open_title, timeout=10000)
        n_save = page.get_by_role("button", name="保存", exact=True).count()
        n_append = page.get_by_role("button", name="追加", exact=True).count()
        n_resolve = page.get_by_role("button", name="确认解决", exact=True).count()
        n_toggle = page.get_by_role("button", name="开始处理", exact=True).count()
        check(
            "reviewer 不显示保存/追加/确认解决按钮(未解决故障)",
            n_save + n_append + n_resolve + n_toggle == 0,
            f"save={n_save} append={n_append} resolve={n_resolve} toggle={n_toggle}",
        )

        # ========== 10. 状态切换不清空未保存编辑(保留未提交编辑) ==========
        print("== 10. 状态切换保留未保存编辑 ==")
        do_login(page, OP_USER, OP_PASS)
        page.goto(open_fault_url)
        page.wait_for_timeout(800)
        toggle_marker = f"{marker} 状态切换前未保存的描述"
        field(page, "详细描述").fill(toggle_marker)  # 故意不点保存
        page.get_by_role("button", name="转为草稿").click()
        page.wait_for_timeout(1200)
        val = field(page, "详细描述").input_value()
        check("转为草稿后未保存的描述编辑仍在", toggle_marker in val, f"got={val[:80]}")
        page.get_by_role("button", name="开始处理").click()
        page.wait_for_timeout(1200)
        val = field(page, "详细描述").input_value()
        check("转回处理中后未保存的描述编辑仍在", toggle_marker in val, f"got={val[:80]}")

        browser.close()

    print()
    if FAILS:
        print(f"结果: {len(FAILS)} 项失败 -> {FAILS}")
        sys.exit(1)
    print(f"结果: 浏览器完整闭环全部通过([br:{RUN_ID}] 材料保留在目标环境)")


if __name__ == "__main__":
    main()
