"""Exercise the complete file organizer using synthetic files and fake native tags."""
import argparse
import json
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

import gtd
import gtd_dashboard as web
import gtd_files as files


def run(artifacts=None, browser_executable=None):
    with tempfile.TemporaryDirectory(prefix="gtd-file-ui-") as temporary:
        base = Path(temporary).resolve()
        state = gtd.State(base / "state")
        workspace = base / "course"
        workspace.mkdir()
        for name in ("运筹学作业.pdf", "已完成练习.pdf"):
            (workspace / name).write_text("Synthetic fixture only")
        writes = []
        data=files.read(state);data["initialized"]=True;files.write(state,data)
        (files.folder(state)/"library-paused-browser-fixture.json").write_text("fixture old scan")
        tags = [{"name": name, "color": color} for name, color in [
            ("红色", 6), ("黄色", 5), ("橙色", 7), ("绿色", 2), ("灰色", 1)]]
        def fake_launch(local):
            data = files.read(local)
            records = [{"path": str(path), "name": path.name, "directory": path.is_dir(), "package": False,
                        "identity": path.name, "signature": path.name + ":v1", "size": 1,
                        "tags": [], "tag_colors": {}} for path in map(lambda scope:Path(scope["path"]),data["scopes"])]
            files.merge_scan(local, {"items": records, "tags": tags, "errors": []})
            for item in files.snapshot(local)["items"]:
                files.edit(local, {"item_id": item["id"], "category": "important"})
            data = files.read(local)
            data["job"] = {"state": "done", "summary": "合成建议准备完成"}
            files.write(local, data)
        def fake_native(_state, command, payload):
            assert command == "files-apply"
            writes.append(payload)
            return {"ok": True, "items": [{
                "path": item["path"], "ok": True,
                "after": [tag for tag in item["before"] if tag not in item["remove"]] + item["add"],
                "tag_colors": {tag: item["colors"][tag] for tag in item["add"]}}
                for item in payload["items"]]}
        server = web.Server(("127.0.0.1", 0), state.base, "fixture-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors = []
        with patch.object(files, "launch", side_effect=fake_launch), patch.object(gtd, "native", side_effect=fake_native), sync_playwright() as playwright:
            browser = playwright.chromium.launch(**({"executable_path": browser_executable} if browser_executable else {}))
            page = browser.new_page(viewport={"width": 1240, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/#access=fixture-token&view=files")
            expect(page.get_by_role("heading", name="从 Codex 项目开始整理。")).to_be_visible()
            origin=f"http://127.0.0.1:{server.server_port}"
            for route,payload in [("/api/files/scan",{}),
                                  ("/api/files/register",{"paths":[str(workspace)],"recursive":False})]:
                rejected=page.request.post(origin+route,data=payload,headers={"Origin":origin})
                assert rejected.status==410,rejected.text()
                assert "废弃" in rejected.json()["error"]
            assert not page.locator("#files-recursive").count()
            page.get_by_role("button", name="＋ 添加文件或文件夹").click()
            expect(page.get_by_text("仅整理所选文件或文件夹本身，不扫描子目录。")).to_be_visible()
            page.locator("textarea[name=paths]").fill("\n".join(str(p) for p in workspace.iterdir()))
            page.get_by_role("button", name="加入并准备建议").click()
            expect(page.locator("[data-file-item]")).to_have_count(1)  # Default is the shared swipe deck.
            expect(page.locator(".file-reason")).to_contain_text("手动选择")
            page.locator("[data-file-category]").select_option("done")
            expect(page.locator(".file-proposed")).to_contain_text("绿色")
            page.locator("[data-file-select]").check()
            page.get_by_role("button", name="预览 1 项 →").click()
            expect(page.get_by_role("heading", name="确认添加这批 Finder 标签")).to_be_visible()
            assert not writes
            page.get_by_role("button", name="确认应用 1 项").click()
            expect(page.locator("#confirm-dialog")).not_to_be_visible()
            expect(page.get_by_text("标签已写入 Finder，并已核对。")).to_be_visible()
            assert len(writes) == 1 and writes[0]["items"][0]["add"] == ["绿色"]
            page.locator("[data-file-filter=applied]").click()
            expect(page.locator(".file-current")).to_contain_text("绿色")
            page.locator("#files-tag-filter").select_option("绿色")
            expect(page.locator("[data-file-item]")).to_have_count(1)
            page.locator(".file-history summary").click()
            page.get_by_role("button", name="撤销本次添加").click()
            expect(page.get_by_text("已撤销本次添加，其他标签保留。")).to_be_visible()
            assert len(writes) == 2 and writes[-1]["undo"]
            page.locator("[data-file-filter=all]").click()
            page.locator("#files-tag-filter").select_option("")
            page.get_by_role("button", name="应用设置").click()
            page.get_by_role("button", name="颜色与程度设置").click()
            expect(page.locator("select[name=done] option")).to_have_count(2)  # Blank plus green.
            page.get_by_role("button", name="取消", exact=True).click()
            page.get_by_role("button", name="应用设置").click()
            page.get_by_role("button", name="资料整理与缓存").click()
            expect(page.get_by_role("heading",name="资料整理与缓存")).to_be_visible()
            expect(page.get_by_text("library-paused-browser-fixture.json",exact=False)).to_be_visible()
            page.get_by_role("button",name="清理所选缓存").click()
            expect(page.locator("#confirm-dialog")).not_to_be_visible()
            assert not (files.folder(state)/"library-paused-browser-fixture.json").exists()
            page.get_by_role("button",name="暂停整理",exact=True).click()
            expect(page.get_by_role("button",name="恢复整理",exact=True)).to_be_visible()
            assert (files.folder(state)/"paused").exists()
            page.get_by_role("button",name="恢复整理",exact=True).click()
            expect(page.get_by_role("button",name="暂停整理",exact=True)).to_be_visible()
            assert not (files.folder(state)/"paused").exists()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            if artifacts:
                directory = Path(artifacts)
                directory.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(directory / "files-mobile.png"), full_page=True)
                page.set_viewport_size({"width": 1240, "height": 900})
                page.screenshot(path=str(directory / "files-desktop.png"), full_page=True)
            # Paging and search must reach records absent from the current 40-item page.
            with files.open_store(state) as store:
                seed=store.items(limit=1)[0]
                for index in range(50):
                    store.put({**seed,"id":f"page{index:04}","path":str(base/f"page-{index}"),
                               "name":f"pagination-project-{index:04}","status":"suggested","present":True})
            page.reload()
            expect(page.locator(".file-pages")).to_contain_text("第 1 / 2 页")
            page.get_by_role("button",name="下一页",exact=True).click()
            expect(page.locator(".file-pages")).to_contain_text("第 2 / 2 页")
            page.locator("#search").fill("pagination-project-0049")
            expect(page.locator(".file-card h2")).to_have_text("pagination-project-0049")
            expect(page.locator(".file-pages")).to_contain_text("共 1 项")
            assert not errors, errors
            browser.close()
        server.shutdown()
        server.server_close()
        state.db.close()
        return {"ok": True, "fake_file_writes": len(writes), "real_file_tag_writes": 0,
                "real_reminder_writes": 0, "paged_search": True, "browser_errors": errors}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts")
    parser.add_argument("--browser")
    args = parser.parse_args()
    print(json.dumps(run(args.artifacts, args.browser), ensure_ascii=False))
