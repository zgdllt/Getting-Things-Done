"""Exercise the workbench against synthetic state; never touches native applications."""
import argparse
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch
from playwright.sync_api import sync_playwright, expect

import gtd
import gtd_dashboard as web


def run(artifacts=None, browser_executable=None):
    with tempfile.TemporaryDirectory(prefix="gtd-browser-") as temp:
        base=Path(temp);state=gtd.State(base)
        key=gtd.capture(state,"设计项目：整理第一版演示","project","设计项目")["candidate_key"]
        with state.db:
            state.db.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?)",("diagnostic",key,"这次想先验证哪一步？","open","",None,gtd.now()))
        today=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
        native={"ok":True,"generated_at":gtd.now(),"permissions":{"reminders":"fullAccess","calendar":"fullAccess"},
                "lists":[{"id":"fixture-list","title":"个人行动","source":"验证数据","writable":True}],
                "calendars":[],"reminders":[{"id":"fixture-reminder","title":"整理本周计划","list":"个人行动","list_id":"fixture-list","due":{"value":today,"all_day":True},"completed":False,"modified_at":"v1","notes":"合成验证事项"}],
                "events":[{"id":"fixture-event","title":"产品演示 · 原型评审","calendar":"学习","start":today+"T14:00:00+08:00","end":today+"T15:00:00+08:00","all_day":False,"location":"图书馆"}]}
        gtd.atomic_json(base/"last-snapshot.json",native);gtd.atomic_json(base/"snapshot-status.json",{"ok":True})
        server=web.Server(("127.0.0.1",0),base,"fixture-token")
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        writes=[]
        def fake_native(_state,command,payload=None,**kwargs):
            if command=="snapshot":return json.loads(json.dumps(native))
            assert command=="apply"
            writes.append(payload)
            if payload['action']=='create_reminder':
                item={"id":"created-fixture","title":payload['title'],"list_id":"fixture-list","list":"个人行动","modified_at":"new","completed":False,"due":None,"notes":payload.get('notes','')}
                native['reminders'].append(item)
            else:
                item=next(i for i in native['reminders'] if i['id']==payload['id'])
                item.update(completed=True,modified_at="v2")
            return {"ok":True,"item":item}
        artifacts=Path(artifacts or ".test-artifacts").resolve();artifacts.mkdir(parents=True,exist_ok=True)
        errors=[]
        try:
            with patch.object(gtd,"native",side_effect=fake_native),sync_playwright() as p:
                launch={"headless":True}
                if browser_executable:launch["executable_path"]=browser_executable
                browser=p.chromium.launch(**launch)
                context=browser.new_context(viewport={"width":1440,"height":1020},permissions=["clipboard-read","clipboard-write"])
                page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/#access=fixture-token")
                page.wait_for_load_state('networkidle');page.get_by_role('heading',name='今天，先做哪一件？').wait_for()
                assert 'access=' not in page.url
                page.screenshot(path=str(artifacts/'workbench-fixture-today.png'),full_page=True)
                page.evaluate("location.hash='access=fixture-token&view=inbox'")
                page.get_by_role('heading',name='先理清楚，再放进清单。').wait_for()
                assert page.url.endswith('#inbox')
                page.locator('nav [data-view=inbox]').click()
                page.locator('[data-answer=diagnostic]').fill('先完成页面原型，再核对主要交互。')
                page.screenshot(path=str(artifacts/'workbench-fixture-clarify.png'),full_page=True)
                page.locator('#save-answers').click()
                expect(page.locator('[data-select]')).to_be_enabled()
                assert writes==[]
                page.locator('[data-select]').check();page.locator('#prepare-selected').click()
                page.locator('#destination').select_option('fixture-list')
                page.locator('[data-title]').fill('完成第一版页面原型，记录交互问题')
                page.locator('#plan-form [type=submit]').click();page.locator('#commit-plan').wait_for()
                assert writes==[]
                page.locator('#commit-plan').click();expect(page.locator('dialog')).not_to_be_visible()
                assert len(writes)==1 and writes[0]['action']=='create_reminder'
                page.locator('nav [data-view=today]').click()
                page.locator('[data-complete=fixture-reminder]').click();page.locator('[data-complete=fixture-reminder]').wait_for(state='detached')
                assert len(writes)==2 and writes[-1]['completed'] is True
                page.locator('#capture-input').fill('浏览器隔离验证：记下一个新想法')
                page.locator('#capture-form [type=submit]').click();page.wait_for_timeout(300)
                page.locator('nav [data-view=inbox]').click()
                page.get_by_role('heading',name='浏览器隔离验证：记下一个新想法').wait_for()
                page.get_by_role('button',name='忽略此线索').click()
                page.get_by_role('heading',name='浏览器隔离验证：记下一个新想法').wait_for(state='detached')
                page.locator('nav [data-view=projects]').click();page.get_by_role('heading',name='设计项目',exact=True).wait_for()
                page.locator('nav [data-view=review]').click();page.locator('[data-check=calendar]').check()
                page.locator('nav [data-view=inbox]').click();page.locator('nav [data-view=review]').click();assert page.locator('[data-check=calendar]').is_checked()
                page.locator('#copy-review').click();assert '$personal-gtd' in page.evaluate('navigator.clipboard.readText()')
                page.set_viewport_size({"width":390,"height":844});page.screenshot(path=str(artifacts/'workbench-fixture-mobile.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors,errors
                browser.close()
            print(json.dumps({"ok":True,"flows":["session","today","clarify","preview-before-write","confirm-write","complete","capture","dismiss","projects","review","clipboard","mobile"],"native_test_writes":0,"fake_native_writes":len(writes),"browser_errors":errors,"artifacts":str(artifacts)},ensure_ascii=False))
        finally:server.shutdown();server.server_close();thread.join();state.db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts',default='.test-artifacts')
    parser.add_argument('--browser',help='Optional installed Chromium/Chrome executable')
    args=parser.parse_args()
    run(args.artifacts,args.browser)
