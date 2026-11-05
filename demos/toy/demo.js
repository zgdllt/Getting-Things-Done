"use strict";
const $ = selector => document.querySelector(selector);
const escape = value => String(value ?? "").replace(/[&<>"']/g, c =>
  ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
const STORAGE = "getting-things-done.toy.v1";
const seed = () => ({
  display: "cards",
  capture: "",
  items: [
    {id:"reading", title:"读完一章书，记下一个想试的想法", state:"today", bucket:"学习", note:"从十分钟开始，留下一句话就够。"},
    {id:"walk", title:"放下屏幕，出去散步二十分钟", state:"today", bucket:"生活", note:"给注意力留一点空白。"},
    {id:"presentation", title:"为小组分享选一个具体案例", state:"today", bucket:"项目", note:"完成标准：选定案例，并写下要回答的一个问题。"},
    {id:"trip", title:"周末去看看那个展览", state:"inbox", bucket:"生活", question:"这周要安排，还是先放进将来想做的事？", source:"演示线索 · 浏览器里看过一篇展览介绍"},
    {id:"course", title:"把网课里没弄懂的一段补上", state:"inbox", bucket:"学习", question:"具体是哪一段？接下来可以做的最小一步是什么？", source:"演示线索 · 学习笔记里留下了一个问号"},
    {id:"reply", title:"跟进还没收到的反馈", state:"inbox", bucket:"等待", question:"在等谁的什么反馈？准备什么时候再看一下？", source:"演示线索 · 小组协作记录"}
  ]
});
let data;
try {
  const saved = JSON.parse(localStorage.getItem(STORAGE) || "null");
  data = saved && Array.isArray(saved.items) ? saved : seed();
} catch (_) { data = seed(); }
const state = {view:["today","inbox","done"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "today", query:""};
const deck = new TaskDeck(() => render());
let confirmation = null;
let storageWarned = false;
function persist() {
  try { localStorage.setItem(STORAGE, JSON.stringify(data)); }
  catch (_) {
    if (!storageWarned) {
      storageWarned = true;
      toast("浏览器未允许保存，当前操作会保留到页面关闭。");
    }
  }
}
function toast(text) {
  $("#toast").textContent = text;
  $("#toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("#toast").hidden = true, 3800);
}
function itemCard(item, index) {
  if (item.state !== "inbox") return `<article class="focus-task"><div><span class="tag green">${escape(item.bucket)}</span><p class="focus-task-title">${escape(item.title)}</p><p class="intro focus-notes">${escape(item.note || "从这里开始，往前走一小步。")}</p></div>${item.state === "done" ? `<button class="button subtle" data-restore="${escape(item.id)}">↶ 放回今天</button>` : `<button class="button primary focus-complete" data-complete="${escape(item.id)}">✓ 标记完成</button>`}</article>`;
  return `<article class="candidate" data-candidate="${escape(item.id)}"><div class="candidate-top"><div><span class="tag amber">${item.answer ? "已澄清，等你确认" : "需要你的判断"}</span><h2>${escape(item.title)}</h2></div><span class="candidate-num">${String(index + 1).padStart(2, "0")}</span></div><div class="meta"><span>${escape(item.bucket)}</span><span>· 演示候选</span></div>
    <div class="question"><label for="answer-${escape(item.id)}">${escape(item.question)}</label><textarea id="answer-${escape(item.id)}" data-answer="${escape(item.id)}" placeholder="写下你的决定或具体下一步…">${escape(item.draft ?? item.answer ?? "")}</textarea></div>
    ${item.answer ? `<div class="agent-response"><strong>已按你的原话收束 · 本地演示</strong><p>${escape(item.answer)}</p><small>这份 demo 不做模型推断；你可以在确认时修改正式标题。</small></div>` : ""}
    <details class="source-details"><summary>查看演示依据</summary><div class="source">${escape(item.source || "你在这个演示中手动收集")}</div></details>
    <div class="candidate-actions"><button class="button subtle" data-dismiss="${escape(item.id)}">忽略此线索</button><div class="actions"><button class="button" data-save="${escape(item.id)}">保存答复</button>${item.answer ? `<button class="button primary" data-confirm="${escape(item.id)}">确认下一步 →</button>` : ""}</div></div></article>`;
}
function listRow(item, index) {
  if (item.state === "inbox") return itemCard(item, index);
  return `<article class="task"><button class="complete-button" aria-label="${item.state === "done" ? "恢复" : "完成"}：${escape(item.title)}" data-${item.state === "done" ? "restore" : "complete"}="${escape(item.id)}">${item.state === "done" ? "✓" : ""}</button><div class="task-content"><p class="task-title">${escape(item.title)}</p><div class="meta">${escape(item.bucket)} · ${item.state === "done" ? "已完成" : "下一步行动"}</div><p class="intro">${escape(item.note || "")}</p></div></article>`;
}
function render() {
  const active = document.activeElement, activeId = active.id, caret = active.selectionStart;
  const items = data.items.filter(item => item.state === state.view &&
    (!state.query || (item.title + " " + (item.note || "")).toLocaleLowerCase().includes(state.query.toLocaleLowerCase())));
  const copy = {
    today:["01", "今天，先做哪一件？", "清单承接记挂，你只需要专心眼前这一件。", "TODAY · A LITTLE ROOM TO THINK"],
    inbox:["02", "先理清楚，再放进清单。", "先答复、再确认。想法可以留下，承诺由你决定。", "INBOX · CLARIFY TOGETHER"],
    done:["03", "已经向前走了这些。", "给做完的事一个位置，也给自己留一点余裕。", "DONE · A LITTLE FURTHER"]
  }[state.view];
  $("#main").innerHTML = `<div class="heading"><div><p class="eyebrow">${copy[3]}</p><h1>${copy[1]}</h1><p class="intro">${copy[2]}</p></div><span class="edition" aria-hidden="true">${copy[0]}</span></div>
    <form id="capture-form" class="capture-form"><span class="capture-icon" aria-hidden="true">＋</span><label class="sr-only" for="capture-input">随手记下一个想法或待办</label><input id="capture-input" value="${escape(data.capture || "")}" placeholder="先记下来，稍后一起理清楚。" required maxlength="1000"><button class="button primary" type="submit">记下来 ↵</button></form>
    <div class="demo-work-area">${items.length ? data.display === "list" ? `<section class="panel demo-list">${items.map(listRow).join("")}</section>` : deck.render({id:state.view + ":" + state.query, items, key:item => item.id, renderItem:itemCard, label:state.view === "inbox" ? "待澄清卡片" : "待办卡片"}) : `<div class="empty"><span class="empty-glyph">✓</span><h3>${state.query ? "没有找到相关事项" : state.view === "inbox" ? "这一页已经理清了" : state.view === "done" ? "给第一件做完的事留个位置" : "今天的清单已经清空"}</h3><p>${state.query ? "换个关键词试试。" : "可以收集一个想法，或者去另一页看看。"}</p><button class="button subtle" data-view="${state.view === "inbox" ? "today" : "inbox"}">${state.view === "inbox" ? "回到今天" : "去待澄清"} →</button></div>`}</div>`;
  for (const view of ["today", "inbox", "done"]) {
    $(`#${view}-count`).textContent = data.items.filter(item => item.state === view).length;
    $(`nav [data-view="${view}"]`).setAttribute("aria-current", state.view === view ? "page" : "false");
  }
  if (activeId && document.getElementById(activeId)) {
    const node = document.getElementById(activeId);
    node.focus({preventScroll:true});
    if (caret != null && node.setSelectionRange) node.setSelectionRange(caret, caret);
  }
}
function navigate(view) {
  if (!["today", "inbox", "done"].includes(view)) return;
  state.view = view;
  location.hash = view;
  render();
}
function settings() {
  $("#dialog-content").innerHTML = `<h2 id="dialog-title">应用设置</h2><p>选一个让自己更轻松的看法。</p><form id="preferences-form"><fieldset class="display-options"><legend class="sr-only">待办展示样式</legend>${[["cards", "滑动卡片", "一次一件，左右切换 · 默认", "▱"], ["list", "条目列表", "展开浏览，适合批量查看", "☷"]].map(([value,title,description,glyph]) => `<label class="display-option"><input type="radio" name="task_display" value="${value}" ${data.display === value ? "checked" : ""}><span><strong>${title}</strong><small>${description}</small></span><span aria-hidden="true">${glyph}</span></label>`).join("")}</fieldset><p>切换样式会保留答复草稿和当前事项。</p><div class="dialog-actions"><button class="button subtle" type="button" data-close-dialog>关闭</button><button class="button primary" type="submit">保存展示设置</button></div></form><section class="settings-status"><h3 class="settings-section-title">关于这个演示</h3><p>仅使用浏览器本地存储。保存答复为本地模拟，不调用模型、不读取 Computer History、日历或提醒事项。</p><button class="button subtle" id="reset-demo">恢复初始演示数据…</button></section>`;
  $("#confirm-dialog").showModal();
}
async function itemAction(item, action) {
  const messages = {
    complete:["正在记录完成…", "已完成：" + item.title, "✓ 完成一件了，继续看下一件。"],
    restore:["正在放回今天…", "已放回今天", "✓ 已放回今天。"],
    dismiss:["正在忽略线索…", "已忽略这条线索", "✓ 已忽略，继续看下一件。"],
    save:["正在保存答复…", "答复已保存 · 接下来确认下一步", "✓ 答复已保存，确认后再放进今天。"]
  }[action];
  const answer = (item.draft || "").trim();
  if (action === "save" && !answer) return toast("先写下你的决定或下一步。");
  try {
    await deck.act({
      id:state.view + ":" + state.query, pending:messages[0], success:messages[1],
      kind:action === "save" ? "save" : "advance"
    }, () => {
      if (action === "complete") item.state = "done";
      if (action === "restore") item.state = "today";
      if (action === "dismiss") item.state = "dismissed";
      if (action === "save") item.answer = answer;
      persist();
    });
    toast(messages[2]);
  } catch (error) { toast(error.message || "操作未完成，请重试。"); }
}
document.addEventListener("input", event => {
  const node = event.target;
  if (node.id === "search") { state.query = node.value; render(); }
  if (node.id === "capture-input") { data.capture = node.value; persist(); }
  if (node.dataset.answer) {
    const item = data.items.find(item => item.id === node.dataset.answer);
    if (item) { item.draft = node.value; persist(); }
  }
});
document.addEventListener("submit", event => {
  if (!["capture-form", "preferences-form", "next-step-form"].includes(event.target.id)) return;
  event.preventDefault();
  if (event.target.id === "capture-form") {
    const title = $("#capture-input").value.trim();
    if (!title) return;
    const id = crypto.randomUUID();
    data.items.unshift({id, title, state:"inbox", bucket:"手动收集", question:"这件事接下来想怎么做？", source:"你在这个演示中手动收集"});
    deck.positions.set("inbox:", {key:id, index:0, keys:[id]});
    data.capture = "";
    state.query = ""; $("#search").value = "";
    persist(); navigate("inbox"); toast("已收集，先决定它的下一步。");
  } else if (event.target.id === "preferences-form") {
    data.display = new FormData(event.target).get("task_display");
    persist(); $("#confirm-dialog").close(); render(); toast("展示样式已保存。");
  } else {
    const item = data.items.find(item => item.id === confirmation);
    const title = $("#next-title").value.trim();
    if (!item || !title) return;
    item.title = title; item.state = "today"; item.note = item.answer;
    persist(); $("#confirm-dialog").close(); render(); toast("已确认，放进演示的今天清单。");
  }
});
document.addEventListener("click", async event => {
  const button = event.target.closest("button");
  if (!button || button.disabled) return;
  if (button.dataset.view) return navigate(button.dataset.view);
  if (button.hasAttribute("data-close-dialog")) return $("#confirm-dialog").close();
  if (button.id === "app-settings") return settings();
  if (button.id === "reset-demo") {
    $("#dialog-content").innerHTML = `<h2 id="dialog-title">恢复初始演示数据？</h2><p>将清除这个 demo 中新增的事项和答复，保留你的展示样式。</p><div class="dialog-actions"><button class="button subtle" data-close-dialog>取消</button><button class="button primary" id="confirm-reset">恢复演示</button></div>`;
    return;
  }
  if (button.id === "confirm-reset") {
    const display = data.display;
    data = seed(); data.display = display;
    deck.positions.clear(); deck.feedback.clear(); state.query = ""; $("#search").value = "";
    persist(); $("#confirm-dialog").close(); navigate("today"); toast("已恢复初始演示。");
    return;
  }
  const id = button.dataset.complete || button.dataset.restore || button.dataset.dismiss || button.dataset.save || button.dataset.confirm;
  const item = data.items.find(item => item.id === id);
  if (!item) return;
  if (button.dataset.complete) return itemAction(item, "complete");
  if (button.dataset.restore) return itemAction(item, "restore");
  if (button.dataset.dismiss) return itemAction(item, "dismiss");
  if (button.dataset.save) return itemAction(item, "save");
  if (button.dataset.confirm) {
    confirmation = item.id;
    $("#dialog-content").innerHTML = `<h2 id="dialog-title">把下一步写具体</h2><p>确认后，才会放进 demo 的今天清单。</p><form id="next-step-form"><label for="next-title">行动标题</label><input id="next-title" type="text" required maxlength="1000" value="${escape(item.answer)}"><div class="dialog-actions"><button class="button subtle" type="button" data-close-dialog>取消</button><button class="button primary" type="submit">确认加入今天</button></div></form>`;
    $("#confirm-dialog").showModal();
  }
  persist(); render();
});
window.addEventListener("hashchange", () => {
  const view = location.hash.slice(1);
  if (["today", "inbox", "done"].includes(view)) { state.view = view; render(); }
});
render();
