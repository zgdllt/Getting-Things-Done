import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import gtd
import gtd_files as files
import gtd_projects as projects
from gtd_file_store import open_store


class FileOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "course"
        self.root.mkdir()
        self.file = self.root / "assignment.pdf"
        self.file.write_text("PRIVATE CONTENT MUST NOT ENTER THE MODEL")
        self.state = gtd.State(self.base / "state")
        self.tags = [{"name": name, "color": color} for name, color in [
            ("红色", 6), ("黄色", 5), ("橙色", 7), ("绿色", 2), ("灰色", 1), ("原有自定义", 4)]]
        with patch.object(files, "launch"):
            files.request(self.state, [str(self.root)])
        # Registering the folder never implicitly registers its children.
        with patch.object(files, "launch"):
            files.request(self.state, [str(self.file)])
        self.record = {"path": str(self.file), "name": self.file.name, "directory": False,
                       "package": False, "identity": "dev:ino", "signature": "dev:ino:1:42",
                       "size": 42, "tags": ["原有自定义"], "tag_colors": {"原有自定义": 4}}
        files.merge_scan(self.state, {"items": [self.record], "tags": self.tags, "errors": []})
        self.ident = self.all_items()[0]["id"]

    def all_items(self):
        with open_store(self.state) as store:
            return store.items()

    def put(self, item):
        with open_store(self.state) as store:
            store.put(item)

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def plan(self):
        files.edit(self.state, {"item_id": self.ident, "category": "important_urgent"})
        return files.prepare(self.state, [self.ident])["plan"]

    def test_register_metadata_scope_and_no_file_mutation(self):
        self.assertEqual(self.file.read_text(), "PRIVATE CONTENT MUST NOT ENTER THE MODEL")
        data = files.read(self.state)
        supplied = files.context(self.state, data, self.all_items())
        self.assertNotIn("PRIVATE CONTENT", json.dumps(supplied))
        self.assertTrue(files.in_scope(str(self.file), data["scopes"]))
        self.assertFalse(files.in_scope(str(self.base / "outside"), data["scopes"]))
        self.assertFalse(files.in_scope(str(self.root / "unselected.pdf"), data["scopes"]))
        self.assertFalse(self.state.rows("SELECT * FROM operations"))
        self.assertEqual(data["mapping"]["done"], "绿色")

    def test_user_pause_blocks_launch_requests_and_worker(self):
        (files.folder(self.state) / "paused").write_text("user paused")
        with patch.object(files.subprocess, "Popen") as spawn, patch.object(gtd, "native") as native:
            files.launch(self.state)
            files.worker(self.state)
            with self.assertRaisesRegex(ValueError, "暂停"):
                files.request(self.state)
        spawn.assert_not_called()
        native.assert_not_called()

    def test_existing_colors_preserved_and_green_reserved(self):
        data = files.read(self.state)
        mapping = {**data["mapping"], "important": "绿色", "done": ""}
        with self.assertRaisesRegex(ValueError, "绿色"):
            files.edit(self.state, {"mapping": mapping})
        plan = self.plan()
        self.assertEqual(plan["items"][0]["before_colors"], {"原有自定义": 4})
        self.assertEqual(plan["items"][0]["add"], ["红色"])

    def test_model_cannot_invent_paths_or_unmapped_categories(self):
        data = files.read(self.state)
        supplied = files.context(self.state, data, self.all_items())
        result = {"summary": "fixture", "suggestions": [{
            "item_id": self.ident, "signature": self.record["signature"],
            "category": "undecided", "reason": "缺少截止日期和目标背景"}]}
        self.assertEqual(files.validate(result, supplied, data["mapping"]), result)
        result["suggestions"][0]["item_id"] = "outside"
        with self.assertRaisesRegex(ValueError, "范围之外"):
            files.validate(result, supplied, data["mapping"])

    def test_model_result_does_not_overwrite_user_decision(self):
        item = self.all_items()[0]
        item.update(status="pending", category="undecided", reason="")
        self.put(item)
        def model(*_args):
            files.edit(self.state, {"item_id": self.ident, "category": "done"})
            return {"summary": "旧建议", "suggestions": [{
                "item_id": self.ident, "signature": self.record["signature"],
                "category": "important_urgent", "reason": "旧推断"}]}
        with patch.object(gtd, "extractor_command", return_value=["fixture"]), patch.object(gtd, "run_extractor", side_effect=model):
            files.suggest(self.state)
        item = self.all_items()[0]
        self.assertEqual(item["category"], "done")
        self.assertEqual(item["reason"], "你已手动选择此标签。")

    def test_preview_invalidated_by_later_decision(self):
        plan = self.plan()
        files.edit(self.state, {"item_id": self.ident, "category": "done"})
        with patch.object(gtd, "native") as native, self.assertRaisesRegex(ValueError, "变化"):
            files.commit(self.state, plan["id"])
        native.assert_not_called()

    def test_confirm_repeated_commit_and_undo_only_added_tag(self):
        plan = self.plan()
        calls = []
        def native(_state, command, payload):
            self.assertEqual(command, "files-apply")
            calls.append(payload)
            after = ["原有自定义"] if payload["undo"] else ["原有自定义", "红色"]
            return {"ok": True, "items": [{"path": str(self.file), "ok": True,
                     "after": after, "tag_colors": {tag: 4 if tag == "原有自定义" else 6 for tag in after}}]}
        with patch.object(gtd, "native", side_effect=native):
            files.commit(self.state, plan["id"])
            files.commit(self.state, plan["id"])
            files.commit(self.state, plan["id"], undo=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1]["items"][0]["remove"], ["红色"])
        self.assertEqual(calls[-1]["items"][0]["remove_colors"], {"红色": 6})
        self.assertEqual(self.all_items()[0]["tags"], ["原有自定义"])
        self.assertFalse(self.state.rows("SELECT * FROM operations"))

    def test_ambiguous_native_result_cannot_be_blindly_retried(self):
        plan = self.plan()
        with patch.object(gtd, "native", side_effect=TimeoutError("fixture")):
            with self.assertRaises(TimeoutError):
                files.commit(self.state, plan["id"])
        with patch.object(gtd, "native") as native, self.assertRaisesRegex(ValueError, "尚未核实"):
            files.commit(self.state, plan["id"])
        native.assert_not_called()
        self.assertEqual(files.snapshot(self.state)["operations"][0]["state"], "applying")

    def test_undone_plan_cannot_reapply_from_late_confirm_request(self):
        plan = self.plan()
        plan["state"] = "undone"
        gtd.atomic_json(files.folder(self.state) / (plan["id"] + ".json"), plan)
        with patch.object(gtd, "native") as native, self.assertRaisesRegex(ValueError, "已经撤销"):
            files.commit(self.state, plan["id"])
        native.assert_not_called()

    def test_scan_during_new_scope_request_keeps_unscanned_revision(self):
        revision = files.read(self.state)["requested"]
        with patch.object(files, "launch"):
            files.request(self.state)
        data = files.merge_scan(self.state, {"items": [self.record], "tags": self.tags, "errors": []}, revision)
        self.assertLess(data["scanned"], data["requested"])

    def test_refresh_reconsiders_model_suggestions_and_keeps_manual_choices(self):
        item = self.all_items()[0]
        item.update(status="suggested", category="important", reason="旧模型建议")
        self.put(item)
        with patch.object(files, "launch"):
            files.request(self.state)
        self.assertEqual(self.all_items()[0]["status"], "pending")
        files.edit(self.state, {"item_id": self.ident, "category": "done"})
        with patch.object(files, "launch"):
            files.request(self.state)
        item = self.all_items()[0]
        self.assertEqual((item["status"], item["category"]), ("suggested", "done"))

    def test_partial_undo_retry_skips_already_undone_files(self):
        plan = self.plan()
        other = {**plan["items"][0], "path": str(self.root / "other.pdf"), "id": "other"}
        plan["items"].append(other)
        plan.update(state="undo_partial", results=[{"path": item["path"], "ok": True} for item in plan["items"]],
                    undo_results=[{"path": str(self.file), "ok": True}, {"path": other["path"], "ok": False}])
        gtd.atomic_json(files.folder(self.state) / (plan["id"] + ".json"), plan)
        with patch.object(gtd, "native", return_value={"ok": True, "items": [
            {"path": other["path"], "ok": True, "after": []}]}) as native:
            files.commit(self.state, plan["id"], undo=True)
        self.assertEqual([item["path"] for item in native.call_args.args[2]["items"]], [other["path"]])

    def test_old_recursive_request_is_rejected_before_scanning(self):
        import gtd_dashboard
        with patch.object(files, "launch") as spawn:
            with self.assertRaises(files.DeprecatedFilesProtocol):
                gtd_dashboard.action(self.state,"/api/files/register",{"paths":[str(self.root)],"recursive":False})
            with self.assertRaises(files.DeprecatedFilesProtocol):
                gtd_dashboard.action(self.state,"/api/files/scan",{})
        spawn.assert_not_called()

    def test_unknown_activity_is_gray_and_recent_activity_is_not(self):
        now = time.time()
        self.assertTrue(files.activity({}, 30, now)["gray"])
        self.assertFalse(files.activity({"last_conversation": now-60}, 30, now)["gray"])
        self.assertFalse(files.activity({"last_file_activity": now-60}, 30, now)["gray"])
        idle = {"observed_since": now-40*86400,"last_file_activity": now-35*86400,
                "last_conversation": now-35*86400,"conversation_known": True}
        self.assertTrue(files.activity(idle, 30, now)["idle"])

    def test_existing_green_overrides_stale_priority(self):
        files.edit(self.state, {"item_id": self.ident, "category": "important"})
        record = {**self.record,"tags":["绿色"],"tag_colors":{"绿色":2}}
        files.merge_scan(self.state, {"items":[record],"tags":self.tags,"errors":[]})
        self.assertEqual(self.all_items()[0]["category"],"done")
        with self.assertRaisesRegex(ValueError, "绿色完成"):
            files.edit(self.state, {"item_id":self.ident,"category":"important"})

    def test_changed_green_catalog_cannot_be_applied_as_completed(self):
        files.edit(self.state, {"item_id": self.ident, "category": "done"})
        tags = [{**tag,"color":3} if tag["name"]=="绿色" else tag for tag in self.tags]
        files.merge_scan(self.state, {"items":[self.record],"tags":tags,"errors":[]})
        self.assertEqual(files.read(self.state)["mapping"]["done"],"")
        with self.assertRaises(ValueError):
            files.prepare(self.state, [self.ident])

    def test_cache_clean_preserves_source_tags_scopes_and_undo(self):
        plan = self.plan()
        plan.update(state="done",results=[{"path":str(self.file),"ok":True}])
        with open_store(self.state) as store:
            store.operation(plan)
        receipt = files.folder(self.state)/(plan["id"]+".json")
        gtd.atomic_json(receipt,plan)
        archive = files.folder(self.state)/"library-paused-fixture.json"
        archive.write_text("synthetic cache")
        before = files.read(self.state)["scopes"]
        files.clear_cache(self.state,[archive.name],reset_index=True)
        self.assertFalse(archive.exists())
        self.assertEqual(self.file.read_text(),"PRIVATE CONTENT MUST NOT ENTER THE MODEL")
        self.assertEqual(files.read(self.state)["scopes"],before)
        self.assertTrue(receipt.exists())
        result=files.snapshot(self.state)
        self.assertEqual(result["item_count"],0)
        self.assertEqual(result["operations"][0]["id"],plan["id"])
        self.assertTrue(result["paused"])

    def test_index_paginates_large_collection_and_searches_all_pages(self):
        seed = self.all_items()[0]
        with open_store(self.state) as store:
            for index in range(5000):
                item={**seed,"id":f"fixture{index:06}","path":f"/fixture/{index}",
                      "name":f"project-{index}","category":"important","status":"suggested"}
                store.put(item)
        page=files.snapshot(self.state,{"filter":"all","page":50,"limit":40})
        self.assertEqual(len(page["items"]),40)
        self.assertEqual(page["total"],5001)
        self.assertLess(len(json.dumps(page)),80000)
        self.assertEqual(files.snapshot(self.state,{"filter":"all","q":"project-4999"})["total"],1)

    def test_stop_kills_model_descendants_after_leader_exits(self):
        script=self.base/"gtd_files.py"
        script.write_text("""import os,signal,subprocess,sys,time
p=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print(\"ready\",flush=True);time.sleep(60)'],stdout=subprocess.PIPE,text=True)
p.stdout.readline()
open(sys.argv[2],'w').write(str(p.pid))
time.sleep(60)
""")
        child_pid=self.base/"child.pid"
        process=subprocess.Popen([sys.executable,str(script),str(self.state.base),str(child_pid)],start_new_session=True)
        try:
            for _ in range(100):
                if child_pid.exists():break
                time.sleep(.02)
            self.assertTrue(child_pid.exists())
            data=files.read(self.state);data["worker_pid"]=process.pid;files.write(self.state,data)
            files.stop(self.state)
            process.wait(timeout=5)
            output=subprocess.run(["/bin/ps","-p",child_pid.read_text(),"-o","stat="],capture_output=True,text=True).stdout.strip()
            self.assertTrue(not output or output.startswith("Z"),output)
        finally:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.wait(timeout=5)

    def test_project_discovery_deduplicates_and_reads_metadata_only(self):
        home=self.base/"codex";home.mkdir()
        gtd.atomic_json(home/".codex-global-state.json",{"local-projects":{
            "a":{"name":"Course","rootPaths":[str(self.root)]},
            "b":{"name":"Duplicate","rootPaths":[str(self.root)]}}})
        db=sqlite3.connect(home/"state_5.sqlite")
        db.executescript("""CREATE TABLE projects(id,name,metadata);
            CREATE TABLE project_roots(project_id,path);
            CREATE TABLE threads(id,cwd,project_id,updated_at_ms,updated_at,recency_at_ms,recency_at);""")
        db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?)",("t",str(self.root),"a",1700000000000,0,1700000000000,0))
        db.commit();db.close()
        result=projects.discover(home)
        self.assertEqual(len(result["projects"]),1)
        self.assertEqual(result["projects"][0]["last_conversation"],1700000000)
        self.assertTrue(result["projects"][0]["conversation_known"])

    def test_large_legacy_cache_is_quarantined_without_losing_undo(self):
        local=gtd.State(self.base/"legacy-state")
        try:
            root=files.folder(local)
            # Invalid JSON also proves the large scan is never parsed into memory.
            with (root/"library.json").open("w") as output:
                for _ in range(650):
                    output.write("x"*8192)
            plan=self.plan()
            plan.update(state="done",results=[{"path":str(self.file),"ok":True}])
            receipt=root/(plan["id"]+".json");gtd.atomic_json(receipt,plan)
            data=files.snapshot(local)
            self.assertTrue(data["paused"])
            self.assertEqual(data["operations"][0]["id"],plan["id"])
            files.clear_cache(local,[item["name"] for item in data["cache"]["archives"]])
            self.assertTrue(receipt.exists())
            self.assertEqual(files.snapshot(local)["operations"][0]["id"],plan["id"])
        finally:
            local.db.close()

    def test_observer_startup_failure_is_not_treated_as_running(self):
        bridge=self.base/"failed-bridge"
        bridge.write_text("#!"+sys.executable+"\nprint('{\"ok\":false,\"error\":\"fixture unsupported\"}',flush=True)\n")
        bridge.chmod(0o755)
        with patch.object(gtd,"native_binary",return_value=bridge), patch.object(files,"suggest") as model:
            with self.assertRaisesRegex(RuntimeError,"观察启动失败"):
                files.worker(self.state)
        model.assert_not_called()
