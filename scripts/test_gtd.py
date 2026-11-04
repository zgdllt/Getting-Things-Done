"""Isolated behavioral tests; never reads live history or native applications."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import tomllib
import types
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location("personal_gtd", Path(__file__).with_name("gtd.py"))
gtd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gtd)


class GTDTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = gtd.State(self.root/"state")
        self.history = self.root/"history"
        (self.history/"segments"/"first").mkdir(parents=True)
        self.events = self.history/"segments"/"first"/"events.jsonl"

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def event(self, identity=1, text="明天回复导师"):
        return {"id": identity, "kind": "keyboard.text_input",
                "timestamp": "2026-11-04T00:10:00+08:00",
                "app": {"name":"Synthetic"}, "window":{"title":"Fixture"},
                "keyboard":{"text":text}}

    def pending(self):
        return self.state.rows("SELECT * FROM batches WHERE state='pending'")

    def analysis(self, event_key, title="回复导师", key="reply"):
        return {"summary":"合成事项", "candidates":[{"key":key,"title":title,"bucket":"next",
                "project":None,"suggested_due":None,"event_keys":[event_key]}],"questions":[]}

    def test_partial_line_is_collected_exactly_once(self):
        raw = json.dumps(self.event(), ensure_ascii=False)
        self.events.write_text(raw[:20])
        self.assertEqual(gtd.spool(self.state,self.history)["records"],0)
        with self.events.open("a") as f:
            f.write(raw[20:]+"\n")
        self.assertEqual(gtd.spool(self.state,self.history)["records"],1)
        self.assertEqual(gtd.spool(self.state,self.history)["records"],0)
        self.events.unlink()
        self.assertEqual(len(json.loads(self.pending()[0]["payload"])),1)

    def test_same_inode_rewrite_is_not_silently_skipped(self):
        old = json.dumps(self.event(1,"OLD"))
        new = json.dumps(self.event(2,"NEW"))
        self.assertEqual(len(old),len(new))
        self.events.write_text(old+"\n")
        gtd.spool(self.state,self.history)
        inode = self.events.stat().st_ino
        self.events.write_text(new+"\n")
        self.assertEqual(inode,self.events.stat().st_ino)
        self.assertEqual(gtd.spool(self.state,self.history)["records"],1)

    def test_restart_catches_new_segment_created_while_stopped(self):
        self.events.write_text(json.dumps(self.event())+"\n")
        completed = subprocess.CompletedProcess([],0,"","")
        home = self.root/"home"
        with patch.object(gtd,"native_binary",return_value=Path("/bin/true")), \
             patch.object(gtd.subprocess,"run",return_value=completed), \
             patch.object(Path,"home",return_value=home):
            gtd.install_hook(self.state,self.history)
            self.assertFalse(self.pending())
            newer = self.history/"segments"/"second"
            newer.mkdir()
            (newer/"events.jsonl").write_text(json.dumps(self.event(2))+"\n")
            gtd.install_hook(self.state,self.history)
        self.assertEqual(len(self.pending()),1)

    def test_cleared_candidates_keep_user_decision_and_raise_changes(self):
        self.events.write_text(json.dumps(self.event())+"\n")
        gtd.spool(self.state,self.history)
        batch = self.pending()[0]
        events = json.loads(batch["payload"])
        gtd.ingest(self.state,batch["id"],self.analysis(events[0]["event_key"]),events)
        self.state.db.execute("UPDATE candidates SET state='accepted',target_id='native-1'")
        self.state.db.commit()
        changed = self.analysis(events[0]["event_key"],"明年再回复")
        changed["candidates"][0]["bucket"]="someday"
        gtd.ingest(self.state,batch["id"],changed,events)
        candidate = self.state.rows("SELECT * FROM candidates")[0]
        self.assertEqual((candidate["title"],candidate["bucket"],candidate["target_id"]),
                         ("回复导师","next","native-1"))
        self.assertEqual(len(self.state.rows("SELECT * FROM questions WHERE state='open'")),1)
        gtd.ingest(self.state,batch["id"],changed,events)
        self.assertEqual(len(self.state.rows("SELECT * FROM questions WHERE state='open'")),1)

    def test_invalid_evidence_does_not_consume_batch(self):
        self.events.write_text(json.dumps(self.event())+"\n")
        gtd.spool(self.state,self.history)
        batch = self.pending()[0]
        with self.assertRaises(ValueError):
            gtd.ingest(self.state,batch["id"],self.analysis("made-up"),
                       json.loads(batch["payload"]))
        self.assertEqual(len(self.pending()),1)
        self.assertEqual(self.state.rows("SELECT * FROM candidates"),[])

    def test_due_only_change_keeps_the_proposed_date_in_the_question(self):
        self.events.write_text(json.dumps(self.event())+"\n")
        gtd.spool(self.state,self.history)
        batch=self.pending()[0]
        events=json.loads(batch["payload"])
        original=self.analysis(events[0]["event_key"])
        original["candidates"][0]["suggested_due"]="2026-11-05"
        gtd.ingest(self.state,batch["id"],original,events)
        self.state.db.execute("UPDATE candidates SET state='accepted',target_id='native-1'")
        self.state.db.commit()
        changed=self.analysis(events[0]["event_key"])
        changed["candidates"][0]["suggested_due"]="2026-11-06"
        gtd.ingest(self.state,batch["id"],changed,events)
        question=self.state.rows("SELECT question FROM questions")[0]["question"]
        self.assertIn("2026-11-05",question)
        self.assertIn("2026-11-06",question)
        self.assertEqual(self.state.rows("SELECT suggested_due FROM candidates")[0]["suggested_due"],"2026-11-05")

    def test_native_write_receipt_is_idempotent_and_uncertain_write_stops(self):
        plan = self.root/"plan.json"
        payload={"operations":[{"op_id":"write-1","action":"create_reminder",
                   "list_id":"fixture-list","title":"合成"}]}
        plan.write_text(json.dumps(payload))
        with patch.object(gtd,"native",return_value={"ok":True,"item":{"id":"fixture-id"}}) as native:
            gtd.apply_plan(self.state,plan)
            gtd.apply_plan(self.state,plan)
            self.assertEqual(native.call_count,1)
            payload["operations"][0]["title"]="不同内容"
            plan.write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                gtd.apply_plan(self.state,plan)
            self.assertEqual(native.call_count,1)
        payload["operations"][0]["op_id"]="uncertain"
        plan.write_text(json.dumps(payload))
        with patch.object(gtd,"native",side_effect=TimeoutError("synthetic timeout")) as native:
            with self.assertRaises(TimeoutError):
                gtd.apply_plan(self.state,plan)
            with self.assertRaises(RuntimeError):
                gtd.apply_plan(self.state,plan)
            self.assertEqual(native.call_count,1)

    def test_notification_only_once_for_an_open_question(self):
        self.state.db.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?)",
            ("q",None,"合成问题","open","",None,gtd.now()))
        self.state.db.commit()
        with patch.object(gtd.subprocess,"run",return_value=subprocess.CompletedProcess([],0,"","")) as run:
            self.assertTrue(gtd.notify(self.state)["submitted"])
            self.assertFalse(gtd.notify(self.state)["submitted"])
            self.assertEqual(run.call_count,1)

    def test_snapshot_failure_is_explicit_and_preserves_last_known_data(self):
        fixture={"ok":True,"generated_at":"2026-11-04T00:00:00Z",
                 "permissions":{"calendar":"fullAccess","reminders":"fullAccess"},
                 "reminders":[],"events":[{"id":"calendar-fixture","title":"合成安排"}]}
        with patch.object(gtd,"native",return_value=fixture):
            gtd.snapshot(self.state)
        with patch.object(gtd,"native",side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                gtd.snapshot(self.state)
        self.assertFalse(json.loads((self.state.base/"snapshot-status.json").read_text())["ok"])
        self.assertEqual(json.loads((self.state.base/"last-snapshot.json").read_text())["events"][0]["id"],"calendar-fixture")
        observed={}
        def result(command,**kwargs):
            observed["prompt"]=kwargs["input"]
            final={"summary":"fixture","candidates":[],"questions":[]}
            return subprocess.CompletedProcess(command,0,json.dumps({"type":"item.completed",
                "item":{"type":"agent_message","text":json.dumps(final)}})+"\n","")
        with patch.object(gtd,"extractor_command",return_value=["fake","--model","fixture-model"]),patch.object(gtd.subprocess,"run",side_effect=result):
            gtd.analyze(self.state,[])
        context=json.loads(observed["prompt"].split("OBSERVED_DATA_JSON:\n",1)[1])
        self.assertEqual(context["existing_calendar_events"][0]["id"],"calendar-fixture")
        self.assertFalse(context["system_snapshot_status"]["ok"])

    def test_native_uses_stable_app_identity_and_private_request_files(self):
        binary=self.root/"Fixture.app"/"Contents"/"MacOS"/"gtd-native"
        observed={}
        def fake_open(arguments,**kwargs):
            self.assertEqual(arguments[:3],["/usr/bin/open","-W","-n"])
            self.assertIn("-g",arguments)
            source=Path(arguments[arguments.index("--stdin")+1])
            output=Path(arguments[arguments.index("--stdout")+1])
            errors=Path(arguments[arguments.index("--stderr")+1])
            self.assertEqual(json.loads(source.read_text()),{"start":"fixture"})
            for path in (source,output,errors):
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
            output.write_text('{"ok":true,"fixture":true}')
            observed["files"]=[source,output,errors]
            return subprocess.CompletedProcess(arguments,0,"","")
        self.assertNotIn("PERSONAL_GTD_NATIVE",gtd.os.environ)
        with patch.object(gtd,"native_binary",return_value=binary), \
             patch.object(gtd.subprocess,"run",side_effect=fake_open):
            self.assertTrue(gtd.native(self.state,"snapshot",{"start":"fixture"})["fixture"])
        self.assertTrue(all(not p.exists() for p in observed["files"]))

    def test_unknown_action_item_is_rejected_but_diagnostics_are_allowed(self):
        final={"type":"item.completed","item":{"type":"agent_message",
               "text":json.dumps({"summary":"fixture","candidates":[],"questions":[]})}}
        diagnostic={"type":"item.completed","item":{"type":"error","message":"synthetic warning"}}
        with patch.object(gtd,"extractor_command",return_value=["fake","--model","fixture-model"]), \
             patch.object(gtd.subprocess,"run",return_value=subprocess.CompletedProcess([],0,
                json.dumps(diagnostic)+"\n"+json.dumps(final)+"\n","")):
            gtd.analyze(self.state,[])
        action={"type":"item.completed","item":{"type":"unexpected_file_read","id":"x"}}
        with patch.object(gtd,"extractor_command",return_value=["fake","--model","fixture-model"]), \
             patch.object(gtd.subprocess,"run",return_value=subprocess.CompletedProcess([],0,
                json.dumps(action)+"\n"+json.dumps(final)+"\n","")):
            with self.assertRaises(RuntimeError):
                gtd.analyze(self.state,[])

    def test_native_request_initialization_failure_leaves_no_payload(self):
        binary=self.root/"Fixture.app"/"Contents"/"MacOS"/"gtd-native"
        real_open=Path.open
        def bad_open(path,*args,**kwargs):
            if str(path).endswith(".stderr") and args and args[0]=="x":
                raise OSError("synthetic creation failure")
            return real_open(path,*args,**kwargs)
        with patch.object(gtd,"native_binary",return_value=binary), \
             patch.object(Path,"open",new=bad_open):
            with self.assertRaises(OSError):
                gtd.native(self.state,"snapshot",{"private_fixture":"synthetic"})
        self.assertEqual(list((self.state.base/"runtime"/"requests").iterdir()),[])

    def test_native_complete_ack_is_authoritative_after_launch_wait_race(self):
        binary=self.root/"Fixture.app"/"Contents"/"MacOS"/"gtd-native"
        def fake_open(arguments,**kwargs):
            output=Path(arguments[arguments.index("--stdout")+1])
            output.write_text('{"ok":true,"fixture":"committed"}')
            return subprocess.CompletedProcess(arguments,1,"","Unable to block on application (GetProcessPID())")
        with patch.object(gtd,"native_binary",return_value=binary), \
             patch.object(gtd.subprocess,"run",side_effect=fake_open):
            result=gtd.native(self.state,"status")
        self.assertTrue(result["ok"])
        self.assertEqual(result["fixture"],"committed")
        self.assertEqual(result["launch_wait_exit_status"],1)

    def test_processor_recovers_without_another_filesystem_event(self):
        gtd.atomic_json(self.state.base/"config.json",{"history_root":str(self.history)})
        watcher=MagicMock()
        watcher.poll.side_effect=[None,None,None,0]
        watcher.stderr=io.StringIO("")
        selector=MagicMock()
        selector.select.return_value=[]
        clock=[0]
        def tick():
            clock[0]+=100
            return clock[0]
        with patch.object(gtd,"native_binary",return_value=Path("/bin/true")), \
             patch.object(gtd.subprocess,"Popen",return_value=watcher), \
             patch.object(gtd.selectors,"DefaultSelector",return_value=selector), \
             patch.object(gtd.time,"monotonic",side_effect=tick), \
             patch.object(gtd,"spool",return_value={"records":0}), \
             patch.object(gtd,"snapshot",return_value={}), \
             patch.object(gtd,"process",side_effect=[RuntimeError("temporary"),{"processed_batches":1}]) as process:
            with self.assertRaises(RuntimeError):
                gtd.daemon(self.state)
        self.assertEqual(process.call_count,2)

    def test_manual_capture_and_bounded_history_do_not_move_hook_cursor(self):
        key=gtd.capture(self.state,"收集一个合成想法")["candidate_key"]
        self.assertEqual(self.state.rows("SELECT key FROM candidates")[0]["key"],key)
        self.events.write_text(json.dumps(self.event())+"\n")
        result=gtd.history_window(self.state,self.history,"2026-11-04T00:00:00+08:00",
                                 "2026-11-04T01:00:00+08:00")
        self.assertEqual(result["events"],1)
        self.assertFalse(self.state.rows("SELECT * FROM cursors"))

    def test_extractor_keeps_user_route_and_disables_mcp_without_copying_credentials(self):
        home=self.root/"config-home"
        home.mkdir()
        (home/"config.toml").write_text('''model="fixture-model"
model_provider="fixture-provider"
model_reasoning_effort="medium"
cli_auth_credentials_store="keyring"
[model_providers.fixture-provider]
name="Fixture"
base_url="http://127.0.0.1:1/v1"
wire_api="responses"
http_headers={Authorization="fixture-secret"}
[mcp_servers.desktop-only]
command="/fixture/native"
[mcp_servers."server.with.dots"]
command="/fixture/other"
''')
        original=(home/"config.toml").read_text()
        with patch.object(gtd.shutil,"which",return_value="/fixture/codex"):
            command=gtd.extractor_command(self.state,home)
        self.assertNotIn("--ignore-user-config",command)
        self.assertNotIn("--profile",command)
        self.assertFalse(any("fixture-secret" in argument for argument in command))
        override=next(arg for arg in command if arg.startswith("mcp_servers="))
        data=tomllib.loads(override)["mcp_servers"]
        self.assertEqual(data,{"desktop-only":{"enabled":False},"server.with.dots":{"enabled":False}})
        self.assertEqual((home/"config.toml").read_text(),original)
        self.assertEqual([p.name for p in home.iterdir()],["config.toml"])

    def test_retry_limit_preserves_error_and_only_notifies_once(self):
        gtd.atomic_json(self.state.base/"config.json",{"history_root":str(self.history)})
        watcher=MagicMock()
        watcher.poll.side_effect=[None]*7+[0]
        watcher.stderr=io.StringIO("")
        source=types.SimpleNamespace(readline=lambda:'{"kind":"history_changed"}\n')
        selector=MagicMock()
        selector.select.return_value=[(types.SimpleNamespace(fileobj=source),None)]
        clock=[0]
        def tick():
            clock[0]+=1000
            return clock[0]
        with patch.object(gtd,"native_binary",return_value=Path("/bin/true")), \
             patch.object(gtd.subprocess,"Popen",return_value=watcher), \
             patch.object(gtd.selectors,"DefaultSelector",return_value=selector), \
             patch.object(gtd.time,"monotonic",side_effect=tick), \
             patch.object(gtd,"spool",return_value={"records":1}) as spool, \
             patch.object(gtd,"snapshot",return_value={}), \
             patch.object(gtd,"notify",return_value={"submitted":True}) as notify, \
             patch.object(gtd,"process",side_effect=RuntimeError("persistent fixture failure")) as process:
            with self.assertRaises(RuntimeError):
                gtd.daemon(self.state)
        status=json.loads((self.state.base/"hook-status.json").read_text())
        self.assertEqual(process.call_count,4)
        self.assertGreater(spool.call_count,4)
        self.assertEqual(notify.call_count,1)
        self.assertIn("persistent fixture failure",status["error"])
        self.assertEqual(status["failures"],4)


if __name__ == "__main__":
    unittest.main()
