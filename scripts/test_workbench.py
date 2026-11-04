import concurrent.futures
import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

import gtd
import gtd_clarify
import gtd_dashboard as web


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.state = gtd.State(self.base)
        self.key = gtd.capture(self.state, "合成候选")['candidate_key']
        self.fixture = {"ok": True, "permissions": {"reminders":"fullAccess","calendar":"fullAccess"},
                        "lists": [{"id":"list","title":"测试列表","source":"本地","writable":True}],
                        "reminders":[{"id":"reminder","title":"合成任务","modified_at":"v1"}], "events":[]}
        gtd.atomic_json(self.base/"last-snapshot.json", self.fixture)

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def question(self, key="q"):
        with self.state.db:
            self.state.db.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?)",(key,self.key,"这件事是否推进？","open","",None,gtd.now()))

    def prepare(self):
        return web.prepare(self.state,{"items":[{"candidate_key":self.key,"title":"明确下一步","list_id":"list"}]})

    def test_answers_are_atomic_and_do_not_create_formal_items(self):
        self.question()
        with self.assertRaises(ValueError):
            web.save_answers(self.state,[{"key":"q","text":"同意"},{"key":"missing","text":"答复"}])
        self.assertEqual(self.state.rows("SELECT state FROM questions")[0]["state"],"open")
        web.save_answers(self.state,[{"key":"q","text":"暂不设完成标准"}])
        self.assertEqual(self.state.rows("SELECT state FROM candidates")[0]["state"],"inbox")
        self.assertFalse(self.state.rows("SELECT * FROM operations"))

    def test_gui_minimal_path_keeps_supported_stable_python(self):
        with patch.object(gtd.sys,'executable','/opt/homebrew/Cellar/python@3.15/3.15.0/bin/python3'), \
             patch.object(Path,'is_file',lambda p:str(p)=='/opt/homebrew/bin/python3'), \
             patch.dict(gtd.os.environ,{'PATH':'/usr/bin:/bin'}):
            self.assertEqual(gtd.stable_python(),'/opt/homebrew/bin/python3')

    def test_prepare_requires_answer_and_commit_replays_receipt(self):
        self.question()
        with self.assertRaises(ValueError): self.prepare()
        web.save_answers(self.state,[{"key":"q","text":"推进"}])
        with patch.object(gtd,"extractor_command",return_value=["synthetic-codex","--model","fixture-model"]), patch.object(gtd,"run_extractor",return_value={"summary":"已整理", "updates":[{
                "candidate_key":self.key,"title":"明确下一步","bucket":"next","project":None,
                "suggested_due":None,"disposition":"actionable","explanation":"先做这一步。","follow_up":None}]}):
            gtd_clarify.process(self.state)
        plan=self.prepare()
        self.assertFalse(self.state.rows("SELECT * FROM operations"))
        with patch.object(gtd,"native",return_value={"ok":True,"item":{"id":"real-fixture-id"}}) as native, patch.object(gtd,"snapshot",return_value={}):
            web.action(self.state,"/api/commit",{"plan_id":plan["plan_id"]})
            web.action(self.state,"/api/commit",{"plan_id":plan["plan_id"]})
            self.assertEqual(native.call_count,1)
        self.assertEqual(self.state.rows("SELECT state FROM candidates")[0]["state"],"accepted")

    def test_reprepare_cannot_bypass_uncertain_native_write(self):
        first=self.prepare()
        with patch.object(gtd,"native",side_effect=TimeoutError("ACK lost")) as native:
            with self.assertRaises(TimeoutError): web.action(self.state,"/api/commit",{"plan_id":first["plan_id"]})
            second=self.prepare()
            with self.assertRaises(RuntimeError): web.action(self.state,"/api/commit",{"plan_id":second["plan_id"]})
            self.assertEqual(native.call_count,1)

    def test_stale_preview_rejected_before_native(self):
        plan=self.prepare()
        with self.state.db: self.state.db.execute("UPDATE candidates SET updated_at='new-version'")
        with patch.object(gtd,"native") as native:
            with self.assertRaises(ValueError): web.action(self.state,"/api/commit",{"plan_id":plan["plan_id"]})
            native.assert_not_called()

    def test_complete_replay_survives_snapshot_version_update(self):
        data={"id":"reminder","expected_modified_at":"v1","op_id":str(uuid.uuid4())}
        def refresh(_state):
            self.fixture['reminders'][0]['modified_at']='v2'
            gtd.atomic_json(self.base/'last-snapshot.json',self.fixture)
        with patch.object(gtd,"native",return_value={"ok":True,"item":{"id":"reminder"}}) as native,patch.object(gtd,"snapshot",side_effect=refresh):
            web.action(self.state,"/api/complete",data)
            web.action(self.state,"/api/complete",data)
            self.assertEqual(native.call_count,1)

    def test_dismiss_closes_question_and_cannot_dismiss_system_item(self):
        self.question()
        web.action(self.state,"/api/dismiss",{"candidate_key":self.key})
        self.assertFalse(self.state.rows("SELECT * FROM questions WHERE state='open'"))
        with self.state.db:self.state.db.execute("UPDATE candidates SET state='accepted'")
        with self.assertRaises(ValueError):web.action(self.state,"/api/dismiss",{"candidate_key":self.key})

    def test_rich_notification_is_deduplicated_under_concurrency(self):
        self.question()
        calls=[]
        def send(*args,**kwargs):
            calls.append(args[0]);time.sleep(.06)
            return subprocess.CompletedProcess([],0,"","")
        def notify():
            local=gtd.State(self.base)
            try:return gtd.notify(local)
            finally:local.db.close()
        with patch.object(gtd.subprocess,"run",side_effect=send),concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:notify(),range(2)))
        self.assertEqual(len(calls),1)
        self.assertIn("合成候选",calls[0][-1]);self.assertIn("这件事是否推进",calls[0][-1])
        self.assertEqual(sum(r['submitted'] for r in results),1)

    def test_ingest_waits_for_accepted_version_and_records_change_question(self):
        plan=self.prepare();started=threading.Event();release=threading.Event()
        event={"event_key":"event","text":"合成新版本"}
        analysis={"summary":"新提议","candidates":[{"key":self.key,"title":"不同的新标题","bucket":"someday","event_keys":["event"]}],"questions":[]}
        def native(*args,**kwargs):started.set();release.wait(3);return {"ok":True,"item":{"id":"system-v1"}}
        def write():
            local=gtd.State(self.base)
            try:return gtd.apply_plan(local,self.base/'workbench-plans'/(plan['plan_id']+'.json'))
            finally:local.db.close()
        def ingest():
            local=gtd.State(self.base)
            try:gtd.ingest(local,"batch",analysis,[event])
            finally:local.db.close()
        with patch.object(gtd,"native",side_effect=native),concurrent.futures.ThreadPoolExecutor(2) as pool:
            writing=pool.submit(write);self.assertTrue(started.wait(2));ingesting=pool.submit(ingest)
            time.sleep(.05);self.assertFalse(ingesting.done());release.set();writing.result();ingesting.result()
        candidate=self.state.rows("SELECT * FROM candidates")[0]
        self.assertEqual((candidate['title'],candidate['state']),("合成候选","accepted"))
        self.assertEqual(len(self.state.rows("SELECT * FROM questions WHERE state='open'")),1)

    def test_uncertain_notification_batch_does_not_absorb_or_block_new_questions(self):
        self.question("q1")
        gtd.atomic_json(self.base/"workbench.json",{"fixture":True})
        delivered=[];first_id=[None];resolved=[False]
        def deliver(_state,title,body,identifier=None):
            if first_id[0] is None:first_id[0]=identifier
            delivered.append((identifier,body))
            if identifier==first_id[0] and not resolved[0]:
                return {"channel":"native","submitted":False,"ack_pending":True}
            return {"channel":"native","submitted":True,"clickable":True}
        with patch('gtd_notifications.deliver',side_effect=deliver):
            self.assertFalse(gtd.notify(self.state)['submitted'])
            self.question("q2")
            result=gtd.notify(self.state)
            self.assertTrue(result['submitted'])
            rows={r['key']:r for r in self.state.rows('SELECT * FROM questions')}
            self.assertIsNone(rows['q1']['notified_at']);self.assertIsNotNone(rows['q2']['notified_at'])
            resolved[0]=True;gtd.notify(self.state)
        self.assertIsNotNone(self.state.rows("SELECT notified_at FROM questions WHERE key='q1'")[0]['notified_at'])
        self.assertEqual(len({entry[0] for entry in delivered}),2)
        self.assertEqual(len(self.state.rows('SELECT * FROM notification_batches')),2)

    def test_private_http_auth_origin_and_new_token_replace_old_cookie(self):
        server=web.Server(("127.0.0.1",0),self.base,"current-token")
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        port=server.server_port
        def request(method,path,body=None,headers=None):
            connection=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
            connection.request(method,path,json.dumps(body) if body is not None else None,headers or {})
            response=connection.getresponse();data=response.read();connection.close();return response.status,response.headers,data
        try:
            self.assertEqual(request('GET','/api/state')[0],401)
            self.assertEqual(request('GET','/api/state',headers={'Host':'attacker.invalid','X-GTD-Token':'current-token'})[0],403)
            self.assertEqual(request('POST','/api/capture',{'text':'x'},{'X-GTD-Token':'current-token','Origin':'https://attacker.invalid','Content-Type':'application/json'})[0],403)
            headers={'X-GTD-Token':'current-token','Cookie':'gtd_session=old-token','Origin':f'http://127.0.0.1:{port}','Content-Type':'application/json'}
            status,response,_=request('POST','/api/session',{},headers)
            self.assertEqual(status,200);self.assertIn('HttpOnly',response['Set-Cookie'])
            self.assertEqual(request('GET','/api/state',headers={'Cookie':'gtd_session=current-token'})[0],200)
            self.assertEqual(request('GET','/../../config.json',headers={'X-GTD-Token':'current-token'})[0],404)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()
