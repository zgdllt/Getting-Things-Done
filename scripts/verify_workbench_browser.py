"""Exercise the workbench against synthetic state; never touches native applications."""
import argparse
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch
from playwright.sync_api import sync_playwright, expect

import gtd
import gtd_clarify
import gtd_models
import gtd_dashboard as web


def run(artifacts=None, browser_executable=None):
    with tempfile.TemporaryDirectory(prefix="gtd-browser-") as temp:
        base=Path(temp);state=gtd.State(base)
        gtd.atomic_json(base/"config.json", {"memory_source_enabled": False, "timezone": "Asia/Shanghai", "timezone_source": "user"})
        key=gtd.capture(state,"设计项目：整理第一版演示","project","设计项目")["candidate_key"]
        with state.db:
            state.db.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?)",("diagnostic",key,"这次想先验证哪一步？","open","",None,gtd.now()))
        today=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
        native={"ok":True,"generated_at":gtd.now(),"permissions":{"reminders":"fullAccess","calendar":"fullAccess"},
                "lists":[{"id":"fixture-list","title":"个人行动","source":"验证数据","writable":True}],
                "calendars":[],"reminders":[{"id":"fixture-reminder","title":"整理本周计划","list":"个人行动","list_id":"fixture-list","due":{"value":today,"all_day":True},"completed":False,"modified_at":"v1","notes":"合成验证事项"}],
                "events":[{"id":"fixture-event","title":"产品演示 · 原型评审","calendar":"学习","start":today+"T14:00:00+08:00","end":today+"T15:00:00+08:00","all_day":False,"location":"图书馆"}]}
        gtd.atomic_json(base/"last-snapshot.json",native);gtd.atomic_json(base/"snapshot-status.json",{"ok":True})
        gtd.atomic_json(base/"hook-status.json",{"state":"needs_attention","error":"Candidate cites an event outside this batch"})
        server=web.Server(("127.0.0.1",0),base,"fixture-token")
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        writes=[];workers=[];model_calls=[];fail_next=[False]
        def fake_model(local,command,prompt,*args):
            model_calls.append(gtd_models.selection(local))
            time.sleep(1.1)
            if fail_next[0]:
                fail_next[0]=False
                raise TimeoutError("合成模型服务暂时超时")
            context=json.loads(prompt.split("INPUT_JSON:\n",1)[1])
            return {"summary":"已把你的答复整理成具体下一步。", "updates":[{
                "candidate_key":candidate["key"], "title":"完成第一版页面原型并核对主要交互" if candidate["key"]==key else "检查重试后的合成事项",
                "bucket":"next", "project":candidate["project"], "suggested_due":None,
                "disposition":"actionable", "explanation":"先完成页面原型，再按你的决定核对主要交互；保存答复后已自动更新下一步。", "follow_up":None
            } for candidate in context["candidates"]]}
        def start_worker(local):
            def work():
                worker=gtd.State(local.base)
                try:gtd_clarify.process(worker)
                finally:worker.db.close()
            thread=threading.Thread(target=work);workers.append(thread);thread.start()
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
            with patch.object(gtd,"native",side_effect=fake_native),patch.object(gtd,"run_extractor",side_effect=fake_model),patch.object(gtd,"extractor_command",return_value=["fake","--model","fixture"]),patch.object(gtd_clarify,"start",side_effect=start_worker),sync_playwright() as p:
                launch={"headless":True}
                if browser_executable:launch["executable_path"]=browser_executable
                browser=p.chromium.launch(**launch)
                context=browser.new_context(viewport={"width":1440,"height":1020},permissions=["clipboard-read","clipboard-write"])
                page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/#access=fixture-token")
                page.wait_for_load_state('networkidle');page.get_by_role('heading',name='今天，先做哪一件？').wait_for()
                assert 'access=' not in page.url
                page.screenshot(path=str(artifacts/'workbench-fixture-today.png'),full_page=True)
                settings=page.locator('#app-settings')
                assert settings.bounding_box()['x']<222
                settings.click();page.get_by_role('heading',name='应用设置',exact=True).wait_for()
                expect(page.locator('#hook-status span')).to_have_text('历史整理已暂停')
                expect(page.locator('[data-history-error]')).to_contain_text('来源引用校验失败')
                expect(page.locator('[data-history-error]')).to_contain_text('待整理 0 批')
                # The dedicated cards scenario tests the new default. Keep the
                # original bulk-flow regression in the selectable list mode.
                page.locator('input[name=task_display][value=list]').check()
                page.get_by_role('button',name='保存展示设置').click()
                expect(page.locator('dialog')).not_to_be_visible()
                gtd.atomic_json(base/"hook-status.json",{"state":"watching"})
                page.evaluate("load()")
                expect(page.locator('#hook-status span')).to_have_text('正在接收新线索')
                settings.click()
                expect(page.locator('[data-history-error]')).to_have_count(0)
                expect(page.locator('#hook-status')).to_have_attribute('title','')
                expect(page.locator('#model-choice')).to_have_value('gpt-6.1-terra')
                expect(page.locator('#reasoning-choice')).to_have_value('high')
                page.screenshot(path=str(artifacts/'workbench-fixture-settings.png'),full_page=True)
                page.locator('#model-choice').select_option('__custom__');page.locator('#custom-model').fill('fixture-cheap-model')
                page.locator('#reasoning-choice').select_option('medium');page.get_by_role('button',name='保存模型设置').click()
                expect(page.locator('dialog')).not_to_be_visible()
                page.reload();expect(page.locator('#settings-model-summary')).to_have_text('fixture-cheap-model · medium')
                settings.click();page.locator('#model-choice').select_option('gpt-6.1-terra');page.locator('#reasoning-choice').select_option('high')
                page.get_by_role('button',name='保存模型设置').click();expect(page.locator('dialog')).not_to_be_visible()
                page.evaluate("location.hash='access=fixture-token&view=inbox'")
                page.get_by_role('heading',name='先理清楚，再放进清单。').wait_for()
                assert page.url.endswith('#inbox')
                page.locator('nav [data-view=inbox]').click()
                page.locator('[data-answer=diagnostic]').fill('先完成页面原型，再核对主要交互。')
                page.reload();expect(page.locator('[data-answer=diagnostic]')).to_have_value('先完成页面原型，再核对主要交互。')
                page.screenshot(path=str(artifacts/'workbench-fixture-clarify.png'),full_page=True)
                page.locator('#save-answers').click()
                page.get_by_text('答复已保存，正在整理',exact=True).wait_for()
                expect(page.locator('[data-select]')).to_be_disabled()
                page.screenshot(path=str(artifacts/'workbench-fixture-answer-pending.png'),full_page=True)
                expect(page.locator('[data-select]')).to_be_enabled(timeout=10000)
                page.get_by_role('heading',name='完成第一版页面原型并核对主要交互',exact=True).wait_for()
                page.get_by_text('根据你的答复',exact=True).wait_for()
                assert model_calls[-1]=={'model':'gpt-6.1-terra','reasoning_effort':'high'}
                page.screenshot(path=str(artifacts/'workbench-fixture-answer-result.png'),full_page=True)
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
                retry_key=gtd.capture(state,"合成服务失败重试验证")['candidate_key']
                with state.db:
                    state.db.execute("INSERT INTO questions VALUES(?,?,?,?,?,?,?)",('retry-question',retry_key,'这条合成事项是否推进？','open','',None,gtd.now()))
                page.locator('nav [data-view=inbox]').click();page.reload()
                page.locator('[data-answer=retry-question]').fill('推进，并检查实际结果。');fail_next[0]=True
                page.locator('#save-answers').click()
                page.get_by_role('button',name='重试整理',exact=True).wait_for(timeout=10000)
                page.get_by_text('合成模型服务暂时超时',exact=True).wait_for()
                page.get_by_role('button',name='重试整理',exact=True).click()
                page.get_by_role('heading',name='检查重试后的合成事项',exact=True).wait_for(timeout=10000)
                assert writes and len(writes)==2
                page.set_viewport_size({"width":390,"height":844});page.screenshot(path=str(artifacts/'workbench-fixture-mobile.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#app-settings').click();page.get_by_role('heading',name='应用设置',exact=True).wait_for()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.get_by_role('button',name='关闭',exact=True).click()
                assert not errors,errors
                browser.close()
            print(json.dumps({"ok":True,"flows":["session","today","clarify","draft-persistence","preview-before-write","confirm-write","complete","capture","dismiss","projects","review","clipboard","mobile","settings-menu","model-persistence","immediate-answer-response","model-failure-retry"],"native_test_writes":0,"fake_native_writes":len(writes),"browser_errors":errors,"artifacts":str(artifacts)},ensure_ascii=False))
        finally:
            server.shutdown();server.server_close();thread.join()
            for worker in workers:worker.join(10)
            state.db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts',default='.test-artifacts')
    parser.add_argument('--browser',help='Optional installed Chromium/Chrome executable')
    args=parser.parse_args()
    run(args.artifacts,args.browser)
