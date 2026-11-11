"""Verify carousel navigation and the standalone Toy using synthetic data only."""
import argparse
import datetime as dt
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect

import gtd
import gtd_dashboard as web
from build_toy_demo import build


def swipe(page, locator, direction=-1):
    box = locator.locator(".deck-caption").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + direction * 100, y + 2, steps=8)
    page.mouse.up()


def success_notice(page, text):
    notice = page.locator(".deck-success-notice")
    expect(notice).to_have_count(1)
    expect(notice).to_be_visible()
    expect(notice).to_contain_text(text)
    expect(page.locator(".deck-busy")).to_have_count(0)
    timing = notice.evaluate("""node => {
        const style = getComputedStyle(node);
        return [style.animationDelay, style.animationDuration, style.pointerEvents];
    }""")
    assert timing == ["0.4s", "0.3s", "none"], timing


def verify_all_views(page, state, native, artifacts):
    """Regress the collections omitted from the original card integration."""
    page.set_viewport_size({"width": 1360, "height": 1000})
    with state.db:
        for suffix in ("a", "b"):
            state.db.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?)",
                             ("orphan-" + suffix, None, "合成独立问题 " + suffix,
                              "open", "", None, gtd.now()))
            state.db.execute("INSERT INTO answer_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                             ("old-job-" + suffix, "old-fingerprint-" + suffix,
                              json.dumps(["answered-" + suffix]), "[]", "done",
                              gtd.now(), gtd.now(), json.dumps({"summary": "合成旧答复 " + suffix}),
                              "", "fixture", "high"))
    page.reload()
    inbox = page.locator('[data-task-deck="inbox:all:"]')
    expect(inbox.locator(".deck-counter")).to_have_text("第 1 / 4 张")
    expect(inbox.locator("[data-question]")).to_have_count(1)
    expect(page.locator("[data-filter=questions]")).to_have_text("有问题 3")
    expect(page.locator("#inbox-count")).to_have_text("4")
    history = page.locator("[data-response-history]")
    assert not history.evaluate("node => node.open")
    history.locator("summary").click()
    responses = page.locator('[data-task-deck="inbox:responses:"]')
    responses.get_by_role("button", name="下一张", exact=True).click()
    expect(responses).to_contain_text("合成旧答复 a")
    page.locator("[data-answer=orphan-a]").fill("合成决定 A")
    inbox.get_by_role("button", name="下一张", exact=True).click()
    page.locator("[data-answer=orphan-b]").fill("合成决定 B")
    inbox.get_by_role("button", name="上一张", exact=True).click()
    expect(page.locator("[data-answer=orphan-a]")).to_have_value("合成决定 A")
    page.reload()
    expect(page.locator("[data-answer=orphan-a]")).to_have_value("合成决定 A")
    page.locator("[data-filter=ready]").click()
    expect(page.locator("[data-question]")).to_have_count(0)
    page.locator("[data-filter=questions]").click()
    expect(page.locator("[data-question]")).to_have_count(1)
    page.locator("#search").fill("合成独立问题 b")
    expect(page.locator("[data-answer=orphan-b]")).to_have_value("合成决定 B")
    page.locator("#search").fill("")
    page.locator("[data-filter=all]").click()
    page.screenshot(path=str(artifacts/"cards-orphan-questions.png"), full_page=True)
    # Keep an old result selected, then save a new standalone job. It must
    # immediately become visible without invoking the model worker.
    history.locator("summary").click()
    responses.get_by_role("button", name="下一张", exact=True).click()
    saved = []
    def save(route):
        items = route.request.post_data_json["items"]
        saved.extend(items)
        route.fulfill(json=web.save_answers(state, items))
    page.route("**/api/answers", save)
    held = []
    page.route("**/api/state", lambda route: held.append((route, route.fetch())))
    page.locator("#save-answers").click()
    page.wait_for_timeout(350)
    assert held, "Saving must reach the delayed state refresh"
    responses.get_by_role("button", name="上一张", exact=True).click()
    expect(responses).to_contain_text("合成旧答复 b")
    for route, response in held:
        route.fulfill(response=response)
    page.unroute("**/api/state")
    expect(responses.get_by_role("heading")).to_have_text("答复已保存，正在整理")
    assert {item["key"] for item in saved} == {"orphan-a", "orphan-b"}
    expect(page.locator("[data-question]")).to_have_count(0)
    success_notice(page, "答复已保存")
    page.unroute("**/api/answers")

    for index in range(2):
        for bucket, project in (("project", "合成项目 " + str(index)), ("waiting", "")):
            title = ("合成等待 " if bucket == "waiting" else "合成项目行动 ") + str(index)
            key = gtd.capture(state, title, bucket, project)["candidate_key"]
            target = "target-" + key
            with state.db:
                state.db.execute("UPDATE candidates SET state='accepted',target_id=? WHERE key=?", (target, key))
            native["reminders"].append({"id": target, "title": title, "list": "验证", "completed": False})
        gtd.capture(state, "合成待确认想法 " + str(index), "project")
        with state.db:
            state.db.execute("INSERT INTO batches VALUES(?,?,?,?,?,?)",
                             ("fixture-batch-" + str(index), gtd.now(), "done", None,
                              "合成历史摘要 " + str(index), ""))
    tomorrow = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date() + dt.timedelta(days=1)
    native["events"] = [
        {"id": "recurring-series", "title": "重复日程 " + str(index), "calendar": "验证",
         "start": f"{tomorrow}T{10 + index}:00:00+08:00",
         "end": f"{tomorrow}T{11 + index}:00:00+08:00", "all_day": False}
        for index in range(2)]
    gtd.atomic_json(state.base/"last-snapshot.json", native)
    page.evaluate("load()")
    expect(page.locator("#toast")).to_be_hidden()
    page.locator("nav [data-view=projects]").click()
    for identifier in ("confirmed", "waiting", "ideas"):
        deck = page.locator(f'[data-task-deck="projects:{identifier}:"]')
        expect(deck.locator(".deck-counter")).to_have_text("第 1 / 2 张")
        deck.get_by_role("button", name="下一张", exact=True).click()
        expect(deck.locator(".deck-counter")).to_have_text("第 2 / 2 张")
    page.evaluate("load()")
    expect(page.locator('[data-task-deck="projects:confirmed:"] .deck-counter')).to_have_text("第 2 / 2 张")
    page.screenshot(path=str(artifacts/"cards-projects-desktop.png"), full_page=True)
    page.locator("nav [data-view=today]").click()
    events = page.locator('[data-task-deck="today:events:"]')
    events.get_by_role("button", name="下一张", exact=True).click()
    expect(events).to_contain_text("重复日程 1")
    page.locator("nav [data-view=review]").click()
    steps = page.locator('[data-task-deck="review:steps:"]')
    steps.get_by_role("button", name="下一张", exact=True).click()
    page.locator("[data-check=calendar]").check()
    page.locator("nav [data-view=projects]").click()
    page.locator("nav [data-view=review]").click()
    expect(page.locator("[data-check=calendar]")).to_be_checked()
    events = page.locator('[data-task-deck="review:events:"]')
    events.get_by_role("button", name="下一张", exact=True).click()
    expect(events).to_contain_text("重复日程 1")
    batches = page.locator('[data-task-deck="review:history:"]')
    batches.get_by_role("button", name="下一张", exact=True).click()
    expect(batches.locator(".deck-counter")).to_have_text("第 2 / 2 张")
    page.screenshot(path=str(artifacts/"cards-review-desktop.png"), full_page=True)
    for view in ("inbox", "projects", "review"):
        page.locator(f"nav [data-view={view}]").click()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), view
    page.screenshot(path=str(artifacts/"cards-review-mobile.png"), full_page=True)
    page.locator("#app-settings").click()
    page.locator("input[value=list]").check()
    page.get_by_role("button", name="保存展示设置").click()
    for view in ("inbox", "projects", "review"):
        page.locator(f"nav [data-view={view}]").click()
        expect(page.locator("[data-task-deck]")).to_have_count(0)
    expect(page.locator("[data-check]")).to_have_count(4)
    expect(page.locator("[data-check=calendar]")).to_be_checked()
    page.locator("#app-settings").click()
    page.locator("input[value=cards]").check()
    page.get_by_role("button", name="保存展示设置").click()
    expect(page.locator("[data-task-deck]")).to_have_count(3)


def run(artifacts, browser_executable=None):
    artifacts = Path(artifacts).resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gtd-cards-") as directory:
        base = Path(directory)
        state = gtd.State(base/"private")
        gtd.atomic_json(state.base/"config.json", {"memory_source_enabled": False, "timezone": "Asia/Shanghai"})
        first = gtd.capture(state, "合成候选一：需要判断")["candidate_key"]
        second = gtd.capture(state, "合成候选二：可以推进")["candidate_key"]
        with state.db:
            state.db.execute("UPDATE candidates SET created_at='2030-01-01' WHERE key=?", (first,))
            state.db.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?)",
                             ("q-cards", first, "先验证哪一步？", "open", "", None, gtd.now()))
        native = {"ok": True, "permissions": {"reminders": "fullAccess", "calendar": "fullAccess"},
                  "lists": [], "events": [], "reminders": [
                      {"id": f"r{i}", "title": f"合成正式任务 {i}", "list": "验证",
                       "completed": False, "due": None, "modified_at": "v1"} for i in range(1, 4)]}
        gtd.atomic_json(state.base/"last-snapshot.json", native)
        gtd.atomic_json(state.base/"snapshot-status.json", {"ok": True})
        server = web.Server(("127.0.0.1", 0), state.base, "cards-token")
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        output = build(base/"site"/"toy"/"gtd-demo")
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(base/"site"))
        toy = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        toy_thread = threading.Thread(target=toy.serve_forever, daemon=True)
        toy_thread.start()
        errors = []
        try:
            with patch.object(gtd, "native") as native_calls, sync_playwright() as playwright:
                launch = {"headless": True}
                if browser_executable:
                    launch["executable_path"] = browser_executable
                browser = playwright.chromium.launch(**launch)
                page = browser.new_page(viewport={"width": 1360, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/classic#access=cards-token")
                page.wait_for_load_state("networkidle")
                cards = page.locator("[data-task-deck]")
                expect(cards).to_have_count(1)
                expect(page.locator(".deck-counter")).to_have_text("第 1 / 3 张")
                expect(page.get_by_role("button", name="上一张", exact=True)).to_be_disabled()
                page.get_by_role("button", name="下一张", exact=True).click()
                expect(page.locator(".focus-task-title")).to_have_text("合成正式任务 2")
                cards.focus(); page.keyboard.press("ArrowRight")
                expect(page.locator(".focus-task-title")).to_have_text("合成正式任务 3")
                expect(page.get_by_role("button", name="下一张", exact=True)).to_be_disabled()
                swipe(page, cards, 1)
                expect(page.locator(".focus-task-title")).to_have_text("合成正式任务 2")
                # Mac trackpads emit horizontal wheel events, including inertia.
                cards.dispatch_event("wheel", {"deltaX": 70, "deltaY": 2})
                expect(page.locator(".focus-task-title")).to_have_text("合成正式任务 3")
                cards.dispatch_event("wheel", {"deltaX": 70, "deltaY": 2})
                expect(page.locator(".focus-task-title")).to_have_text("合成正式任务 3")
                cards.focus(); page.keyboard.press("ArrowLeft")
                # A refresh or an inserted item must keep the same stable key.
                native["reminders"].insert(0, {"id": "new", "title": "新合成任务", "list": "验证", "completed": False})
                gtd.atomic_json(state.base/"last-snapshot.json", native)
                page.evaluate("load()")
                expect(page.locator(".focus-task-title")).to_have_text("合成正式任务 2")
                page.screenshot(path=str(artifacts/"cards-today-desktop.png"), full_page=True)
                page.locator("nav [data-view=inbox]").click()
                answer = page.locator("[data-answer=q-cards]")
                answer.fill("先验证草稿不会丢")
                page.keyboard.press("ArrowLeft")
                expect(answer).to_have_value("先验证草稿不会丢")
                page.get_by_role("button", name="下一张", exact=True).click()
                page.locator("[data-select]").check()
                expect(page.locator("#prepare-selected")).to_contain_text("(1)")
                page.get_by_role("button", name="上一张", exact=True).click()
                expect(answer).to_have_value("先验证草稿不会丢")
                page.reload()
                expect(answer).to_have_value("先验证草稿不会丢")
                page.locator("[data-filter=ready]").click()
                expect(page.locator(".candidate h2")).to_have_text("合成候选二：可以推进")
                expect(page.get_by_role("button", name="下一张", exact=True)).to_be_disabled()
                page.locator("[data-filter=all]").click()
                expect(answer).to_have_value("先验证草稿不会丢")
                # Freeze an old state response to reproduce the save-vs-poll race.
                page.locator("#app-settings").click()
                held = []
                def hold(route):
                    held.append((route, route.fetch()))
                page.route("**/api/state", hold)
                page.evaluate("void load()")
                page.wait_for_timeout(100)
                assert held, "Expected an in-flight stale state request"
                page.locator("input[name=task_display][value=list]").check()
                page.get_by_role("button", name="保存展示设置").click()
                expect(page.locator("[data-candidate]")).to_have_count(2)
                held[0][0].fulfill(response=held[0][1])
                page.unroute("**/api/state", hold)
                page.wait_for_timeout(100)
                expect(page.locator("[data-task-deck]")).to_have_count(0)
                page.reload()
                expect(page.locator("[data-candidate]")).to_have_count(2)
                expect(answer).to_have_value("先验证草稿不会丢")
                page.locator("#app-settings").click()
                page.locator("input[name=task_display][value=cards]").check()
                page.get_by_role("button", name="保存展示设置").click()
                expect(page.locator("[data-task-deck]")).to_have_count(1)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(artifacts/"cards-inbox-mobile.png"), full_page=True)
                assert not native_calls.called, "Navigation/settings must not invoke native applications"
                page.locator("nav [data-view=today]").click()
                title = page.locator(".focus-task-title").inner_text()
                page.route("**/api/complete", lambda route: route.fulfill(status=409, json={"error":"合成失败，请重试"}))
                page.locator("[data-complete]").click()
                expect(page.locator(".deck-receipt")).to_have_text("!合成失败，请重试")
                expect(page.locator(".focus-task-title")).to_have_text(title)
                page.unroute("**/api/complete")
                def complete(route):
                    ident = route.request.post_data_json["id"]
                    next(item for item in native["reminders"] if item["id"] == ident)["completed"] = True
                    gtd.atomic_json(state.base/"last-snapshot.json", native)
                    route.fulfill(json={"ok":True})
                page.route("**/api/complete", complete)
                page.locator("[data-complete]").click()
                expect(page.locator(".deck-action-overlay")).to_be_visible()
                expect(page.locator(".deck-busy")).to_have_count(0)
                expect(page.locator(".deck-receipt")).to_contain_text("已完成：" + title)
                assert page.locator(".focus-task-title").inner_text() != title
                success_notice(page, "已完成：" + title)
                # A confirmed write followed by a failed GET must recover the UI
                # and report pending synchronization, rather than replaying it.
                title = page.locator(".focus-task-title").inner_text()
                page.route("**/api/state", lambda route: route.fulfill(status=503, json={"error":"合成刷新失败"}))
                page.locator("[data-complete]").focus()
                page.keyboard.press("Enter")
                expect(page.locator(".deck-receipt")).to_contain_text("页面待同步")
                expect(page.locator(".deck-busy")).to_have_count(0)
                expect(page.locator(".deck-action-out")).to_have_count(0)
                assert not page.locator(".deck-page").evaluate("(node) => node.inert")
                expect(page.locator(".focus-task-title")).to_have_text(title)
                page.unroute("**/api/state")
                page.evaluate("load()")
                expect(page.locator(".focus-task-title")).not_to_have_text(title)

                # Exercise the private client's other feedback paths with
                # synthetic API success replies; no model or native writes.
                page.locator("nav [data-view=inbox]").click()
                answer.fill("合成答复，只验证反馈动画")
                page.route("**/api/answers", lambda route: route.fulfill(json={"ok": True}))
                page.locator("#save-answers").click()
                expect(page.locator(".deck-receipt")).to_contain_text("答复已保存")
                success_notice(page, "答复已保存")
                page.unroute("**/api/answers")
                page.route("**/api/dismiss", lambda route: route.fulfill(json={"ok": True}))
                page.locator("[data-dismiss]").first.click()
                expect(page.locator(".deck-receipt")).to_contain_text("已忽略")
                success_notice(page, "已忽略")
                page.unroute("**/api/dismiss")
                verify_all_views(page, state, native, artifacts)
                assert not native_calls.called

                demo = browser.new_page(viewport={"width": 1200, "height": 960})
                demo.on("pageerror", lambda error: errors.append(str(error)))
                demo.goto(f"http://127.0.0.1:{toy.server_port}/toy/gtd-demo/index.html")
                demo.wait_for_load_state("networkidle")
                expect(demo.locator(".deck-counter")).to_have_text("第 1 / 3 张")
                swipe(demo, demo.locator("[data-task-deck]"))
                expect(demo.locator(".focus-task-title")).to_have_text("放下屏幕，出去散步二十分钟")
                demo.get_by_role("button", name="标记完成", exact=False).click()
                expect(demo.locator(".deck-action-overlay")).to_be_visible()
                demo.screenshot(path=str(artifacts/"toy-action-complete.png"))
                expect(demo.locator("#done-count")).to_have_text("1")
                expect(demo.locator(".deck-receipt")).to_contain_text("已完成：放下屏幕")
                success_notice(demo, "已完成：放下屏幕")
                demo.locator("nav [data-view=inbox]").click()
                textarea = demo.locator("[data-answer]").first
                textarea.fill("查好开放时间，周六上午出发")
                demo.get_by_role("button", name="下一张", exact=True).click()
                demo.get_by_role("button", name="上一张", exact=True).click()
                expect(textarea).to_have_value("查好开放时间，周六上午出发")
                demo.get_by_role("button", name="保存答复", exact=True).click()
                expect(demo.locator(".deck-action-overlay")).to_be_visible()
                expect(demo.locator(".deck-busy")).to_have_count(0)
                expect(demo.locator(".deck-receipt")).to_contain_text("答复已保存")
                success_notice(demo, "答复已保存")
                expect(demo.locator("#today-count")).to_have_text("2")
                textarea.fill("")
                demo.get_by_role("button", name="下一张", exact=True).click()
                demo.get_by_role("button", name="上一张", exact=True).click()
                expect(textarea).to_have_value("")
                demo.get_by_role("button", name="确认下一步", exact=False).click()
                demo.locator("#next-title").fill("周六上午去看展，提前查开放时间")
                demo.get_by_role("button", name="确认加入今天", exact=True).click()
                expect(demo.locator("#today-count")).to_have_text("3")
                demo.locator("#capture-input").fill("合成新想法，周末整理书架")
                demo.locator("#capture-form [type=submit]").click()
                demo.get_by_role("button", name="忽略此线索", exact=True).click()
                expect(demo.locator(".deck-action-overlay")).to_be_visible()
                expect(demo.locator(".deck-busy")).to_have_count(0)
                expect(demo.locator(".deck-receipt")).to_contain_text("已忽略")
                success_notice(demo, "已忽略")
                demo.locator("#capture-input").fill("合成新想法，周末整理书架")
                demo.locator("#capture-form [type=submit]").click()
                demo.locator("#app-settings").click()
                demo.locator("input[value=list]").check()
                demo.get_by_role("button", name="保存展示设置").click()
                demo.get_by_role("heading", name="合成新想法，周末整理书架", exact=True).wait_for()
                demo.reload()
                expect(demo.locator("[data-task-deck]")).to_have_count(0)
                demo.locator("#app-settings").click()
                demo.locator("input[value=cards]").check()
                demo.get_by_role("button", name="保存展示设置").click()
                demo.set_viewport_size({"width": 390, "height": 844})
                assert demo.evaluate("document.documentElement.scrollWidth <= innerWidth")
                demo.screenshot(path=str(artifacts/"toy-mobile.png"), full_page=True)
                demo.set_viewport_size({"width": 1200, "height": 900})
                demo.locator("nav [data-view=today]").click()
                demo.wait_for_timeout(450)
                demo.evaluate("window.scrollTo(0,0)")
                demo.screenshot(path=str(artifacts/"toy-desktop.png"), full_page=True)
                demo.locator("nav [data-view=done]").click()
                demo.get_by_role("button", name="放回今天", exact=False).click()
                expect(demo.locator("#done-count")).to_have_text("0")
                success_notice(demo, "已放回今天")
                demo.evaluate("toast('合成成功提示')")
                expect(demo.locator("#toast")).to_have_text("合成成功提示")
                toast_timing = demo.locator("#toast").evaluate("""node => {
                    const style = getComputedStyle(node);
                    return [style.animationDelay, style.animationDuration];
                }""")
                assert toast_timing == ["0s, 0.4s", "0.28s, 0.3s"], toast_timing
                demo.wait_for_timeout(450)
                demo.evaluate("toast('连续操作的新提示')")
                expect(demo.locator("#toast")).to_have_text("连续操作的新提示")
                restarted = demo.locator("#toast").evaluate("node => node.getAnimations().find(animation => animation.animationName === 'toast-hop').currentTime")
                assert restarted is not None and 0 <= restarted < 200, restarted
                demo.wait_for_timeout(300)
                expect(demo.locator("#toast")).to_be_visible()
                expect(demo.locator("#toast")).to_be_hidden(timeout=2000)
                demo.evaluate("toast('合成错误提示', true)")
                demo.wait_for_timeout(800)
                expect(demo.locator("#toast")).to_be_visible()
                demo.emulate_media(reduced_motion="reduce")
                demo.locator("nav [data-view=today]").click()
                demo.get_by_role("button", name="标记完成", exact=False).click()
                expect(demo.locator(".deck-success-notice")).to_be_visible()
                expect(demo.locator(".deck-busy")).to_have_count(0)
                assert demo.locator(".deck-success-notice").evaluate("node => getComputedStyle(node).animationName") == "none"
                expect(demo.locator(".deck-success-notice")).to_have_count(0, timeout=2000)
                touch_context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
                touch = touch_context.new_page()
                touch.goto(f"http://127.0.0.1:{toy.server_port}/toy/gtd-demo/index.html")
                touch.wait_for_load_state("networkidle")
                region = touch.locator(".deck-caption").bounding_box()
                x, y = region["x"] + region["width"] * .75, region["y"] + region["height"] / 2
                client = touch_context.new_cdp_session(touch)
                client.send("Input.dispatchTouchEvent", {"type":"touchStart", "touchPoints":[{"x":x, "y":y}]})
                for offset in (20, 40, 70, 100):
                    client.send("Input.dispatchTouchEvent", {"type":"touchMove", "touchPoints":[{"x":x-offset, "y":y}]})
                client.send("Input.dispatchTouchEvent", {"type":"touchEnd", "touchPoints":[]})
                expect(touch.locator(".deck-counter")).to_have_text("第 2 / 3 张")
                touch.wait_for_timeout(350)
                touch.evaluate("window.scrollTo(0,0)")
                touch.screenshot(path=str(artifacts/"toy-touch-mobile.png"), full_page=True)
                touch_context.close()
                assert not errors, errors
                browser.close()
            print(json.dumps({"ok": True, "native_calls": 0, "model_calls": 0, "browser_errors": errors,
                              "scenarios": ["default-cards", "buttons", "keyboard", "swipe", "stable-key",
                                            "draft-navigation", "draft-reload", "selection", "filters",
                                            "settings-persistence", "stale-response", "mobile", "touch-swipe", "trackpad",
                                            "toy-subpath", "toy-clarify-confirm-complete", "toy-capture",
                                            "action-feedback", "action-transitions", "action-failure-stays",
                                            "shared-success-timing", "save-dismiss-restore-feedback",
                                            "success-toast-restart", "error-toast-persists", "reduced-motion",
                                            "confirmed-write-refresh-failure", "orphan-question-cards",
                                            "standalone-job-focus", "all-view-cards-and-lists",
                                            "recurring-event-identity", "review-step-retention"],
                              "artifacts": str(artifacts)}, ensure_ascii=False))
        finally:
            server.shutdown(); server.server_close(); serving.join()
            toy.shutdown(); toy.server_close(); toy_thread.join()
            state.db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", default=".test-artifacts")
    parser.add_argument("--browser")
    args = parser.parse_args()
    run(args.artifacts, args.browser)
