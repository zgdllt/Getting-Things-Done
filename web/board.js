"use strict";
// 文枢 · board. The agent keeps the board; the user calibrates it when they have a moment.
(() => {
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const Q = { 1: { name: "先做", hint: "重要且紧急" }, 2: { name: "安排时间", hint: "重要，不急" },
    3: { name: "顺手处理", hint: "紧急，不太重要" }, 4: { name: "放一放", hint: "不急也不重要" } };
  const KIND = { project: "项目", task: "任务", reminder: "提醒事项", event: "日历" };
  const STATUS = { doing: "正在做", started: "已开工", todo: "要做" };
  const STAGES = { active: "在推进", tool: "已成工具", paused: "暂停·有计划", blocked: "卡住了", superseded: "被取代", done: "已完成" };
  const DONE_LABEL = { doing: "正在做", done: "已完成", tool: "已经变成工具", blocked: "卡住了", not_important: "不重要" };
  const WEEK = "日一二三四五六";
  const state = { view: "board", board: null, memory: null, providers: null, notify: null, notifyPoll: null, expanded: new Set(), q4Open: false,
    menuFor: null, undo: null, drag: null, fresh: null, lastLoad: 0, toastTimer: null,
    notesOpen: new Set(), proposalBusy: new Set(), proposalError: {} };
  const main = $("#main"), menu = $("#menu"), toastBox = $("#toast");

  async function api(path, body, token) {
    const headers = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (token) headers["X-GTD-Token"] = token;
    const response = await fetch(path, { method: body === undefined ? "GET" : "POST", headers,
      body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin", cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "操作未完成，请稍后重试");
    return result;
  }

  function toast(text, { error = false, undo = null } = {}) {
    clearTimeout(state.toastTimer);
    state.undo = undo;
    toastBox.className = "toast" + (error ? " error" : "");
    toastBox.innerHTML = `<span>${esc(text)}</span>${undo ? '<button type="button" data-undo>撤销</button>' : ""}`;
    toastBox.hidden = false;
    state.toastTimer = setTimeout(() => { toastBox.hidden = true; state.undo = null; }, undo ? 9000 : 4000);
  }

  const short = (text, n = 16) => { text = String(text || ""); return text.length > n ? text.slice(0, n) + "…" : text; };
  const pad = (n) => String(n).padStart(2, "0");
  function relDue(due) {
    if (!due) return "";
    const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(due);
    const at = new Date(dateOnly ? due + "T23:59:59" : due), now = new Date();
    const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
    const diff = Math.round((day(at) - day(now)) / 86400000);
    const hm = dateOnly ? "" : ` ${pad(at.getHours())}:${pad(at.getMinutes())}`;
    if (diff < 0) return `逾期 ${-diff} 天`;
    if (diff === 0) return "今天" + (hm || "截止");
    if (diff === 1) return "明天" + hm;
    if (diff < 7) return `周${WEEK[at.getDay()]}${hm}`;
    return `${at.getMonth() + 1}月${at.getDate()}日`;
  }
  function ago(iso) {
    if (!iso) return "尚未扫描";
    const minutes = Math.max(0, Math.round((Date.now() - new Date(iso)) / 60000));
    return minutes < 1 ? "刚刚" : minutes < 60 ? `${minutes} 分钟前` : `${Math.round(minutes / 60)} 小时前`;
  }
  function since(iso) {
    const at = new Date(iso);
    if (!iso || isNaN(at)) return "";
    const minutes = Math.max(0, Math.round((Date.now() - at) / 60000));
    if (minutes < 1) return "刚刚";
    if (minutes < 60) return `${minutes} 分钟前`;
    if (minutes < 1440) return `${Math.round(minutes / 60)} 小时前`;
    return `${Math.round(minutes / 1440)} 天前`;
  }
  function dayLabel(due) {
    const at = new Date(due + "T12:00:00");
    return isNaN(at) ? esc(due) : `<span class="day">${at.getMonth() + 1}月${at.getDate()}日（周${WEEK[at.getDay()]}）</span>`;
  }
  const itemById = (id) => state.board?.items.find((i) => i.id === id);

  // A reply the agent understood as a date change or a new task waits here for one tap.
  function proposalStrip(p, item) {
    // Only items in 文枢's own calendar/list (or reminders, via the guarded path) really move.
    const local = p.kind !== "new_task" && item && item.kind !== "reminder" && !item.owned;
    const busy = state.proposalBusy.has(p.id) ? " disabled" : "";
    const error = state.proposalError[p.id];
    const ask = p.kind === "new_task"
      ? `新建「${esc(short(p.title, 18))}」${p.due ? ` · ${dayLabel(p.due)}` : ""}？`
      : `你说「${esc(short(p.text, 14))}」→ 截止改到 ${dayLabel(p.due)}？${local ? '<small class="local-only">只改文枢里的日期，日历不动</small>' : ""}`;
    const yes = p.kind === "new_task" ? "加入" : "好";
    return `<div class="proposal" role="group" aria-label="待确认的提议：${esc(p.summary)}"><p>${ask}</p><div class="proposal-actions">` +
      `<button type="button" class="yes" data-proposal="${esc(p.id)}" data-accept="1"${busy}>${yes}</button>` +
      `<button type="button" data-proposal="${esc(p.id)}" data-accept="0"${busy}>不用</button></div>` +
      `${error ? `<p class="proposal-error" role="alert">${esc(error)}</p>` : ""}</div>`;
  }
  const noteText = (n) => esc(n.waiting_on ? `在等：${n.waiting_on}` : n.summary || n.text);
  function notesBlock(item) {
    const notes = item.notes || [];
    if (!notes.length) return "";
    const open = state.notesOpen.has(item.id), older = notes.slice(1);
    const first = `<p class="note" title="${esc(notes[0].text)}"><span aria-hidden="true">✎</span>${noteText(notes[0])}<time>${esc(since(notes[0].timestamp))}</time></p>`;
    if (!older.length) return first;
    const toggle = `<button type="button" class="notes-toggle" data-notes="${esc(item.id)}" aria-expanded="${open}">${open ? "收起" : `更早的 ${older.length} 条`}</button>`;
    const list = open ? `<ul class="notes-older">${older.map((n) => `<li title="${esc(n.text)}">${noteText(n)}<time>${esc(since(n.timestamp))}</time></li>`).join("")}</ul>` : "";
    return first + toggle + list;
  }

  function card(item, byId) {
    const urgent = item.urgency >= 0.7;
    const kids = (item.children || []).map((id) => byId.get(id)).filter(Boolean);
    const open = state.expanded.has(item.id);
    const reasons = [...(item.due ? [] : item.urgency_reasons || []), ...(item.importance_reasons || [])].filter((r) => !/未设截止/.test(r)).slice(0, 2);
    const left = item.left_off ? `停在：${esc(item.left_off)}` : "";
    const rest = item.remaining && !String(item.title).includes(item.remaining) ? `还剩：${esc(item.remaining)}` : "";
    const children = !kids.length ? "" : `<button type="button" class="children-toggle" data-children="${esc(item.id)}" aria-expanded="${open}">${open ? "收起" : "展开"} ${kids.length} 件子任务</button>` +
      (open ? `<ul class="children">${kids.map((k) => `<li><span title="${esc(k.title)}">${esc(k.title)}</span><button type="button" data-done="${esc(k.id)}" aria-label="已完成：${esc(k.title)}" title="已完成">✓</button><button type="button" data-menu="${esc(k.id)}" aria-haspopup="menu" aria-label="更多操作：${esc(k.title)}">⋯</button></li>`).join("")}</ul>` : "");
    return `<article class="card${state.fresh === item.id ? " fresh" : ""}" draggable="true" data-id="${esc(item.id)}">
      <div class="card-top"><span class="kind">${KIND[item.kind] || ""}</span><span class="status ${esc(item.status)}">${STATUS[item.status] || esc(item.status)}</span>${item.due ? `<span class="due${urgent ? " urgent" : ""}">${esc(relDue(item.due))}</span>` : ""}${item.due && item.due_source === "user" ? '<span class="mine" title="这个截止日是你在回复里定的">你定的</span>' : ""}${item.owned ? '<span class="owned" title="在文枢自己的日历或提醒列表里，文枢可以直接改">文枢的</span>' : ""}${item.pinned ? '<span class="pin" title="你校准过它的位置">◆</span>' : ""}<button type="button" class="more" data-menu="${esc(item.id)}" aria-haspopup="menu" aria-expanded="false" aria-label="更多操作：${esc(item.title)}">⋯</button></div>
      <h3>${esc(item.title)}</h3>${reasons.length ? `<p class="why">${reasons.map(esc).join(" · ")}</p>` : ""}${left || rest ? `<p class="left-off">${left}${left && rest ? "<br>" : ""}${rest}</p>` : ""}${(item.proposals || []).map((p) => proposalStrip(p, item)).join("")}${notesBlock(item)}${children}</article>`;
  }

  function quadrant(q, items, byId) {
    const top = items.filter((i) => i.quadrant === q && i.top_level !== false);
    const collapsed = q === 4 && !state.q4Open;
    const empty = ["", "没有要紧的事。", "没有需要排时间的事。", "没有需要顺手处理的事。", "空的。"][q];
    const body = collapsed
      ? `<p class="collapsed-note">${top.length ? `${top.length} 张卡暂时收着。` : ""}把卡片拖到这里，表示「不急也不重要」。</p><div class="cards drop-only" data-drop="4"></div>`
      : `<div class="cards" data-drop="${q}">${top.length ? top.map((i) => card(i, byId)).join("") : `<p class="empty">${empty}</p>`}</div>`;
    return `<section class="quadrant q${q}" aria-label="${Q[q].name}：${Q[q].hint}"><header class="q-head"><h2>${Q[q].name}</h2><span class="count">${top.length}</span><p>${Q[q].hint}</p>${q === 4 ? `<button type="button" class="ghost toggle" data-q4>${collapsed ? "展开" : "收起"}</button>` : ""}</header>${body}</section>`;
  }

  function boardView() {
    const b = state.board;
    if (!b) return '<div class="loading"><span class="seal big" aria-hidden="true">枢</span><p>正在读取看板…</p></div>';
    const byId = new Map(b.items.map((i) => [i.id, i]));
    const q1 = b.items.filter((i) => i.quadrant === 1 && i.top_level !== false).length;
    const now = new Date();
    const title = q1 ? `今天有<em>${q1}</em>件要紧的事` : "今天没有要紧的事";
    const unsorted = !b.unsorted.count ? "" : `<details class="unsorted"><summary><strong>${b.unsorted.count}</strong> 条线索还没归类 · agent 没把握放进看板，有空可以看看</summary><ul>${b.unsorted.items.map((u) => `<li><span>${esc(u.title)}</span><small>${esc(u.project || "")}</small><button type="button" data-ignore="${esc(u.key)}">忽略</button></li>`).join("")}</ul></details>`;
    return `<header class="board-head"><div><p class="date">${now.getMonth() + 1}月${now.getDate()}日 · 星期${WEEK[now.getDay()]}</p><h1 class="${q1 ? "" : "calm"}">${title}</h1></div><div class="head-meta"><span>${b.nested_count ? `另有 ${b.nested_count} 件子任务收在项目里` : ""}</span><button type="button" class="ghost" data-refresh>重新整理</button></div></header>
      <section class="matrix" aria-label="四象限看板"><div class="axis-x" aria-hidden="true"><span>紧急</span><span>不急</span></div><div class="axis-y" aria-hidden="true"><span>重要</span><span>不重要</span></div>${[1, 2, 3, 4].map((q) => quadrant(q, b.items, byId)).join("")}<div class="hinge" aria-hidden="true">枢</div></section>${unsorted}`;
  }

  function describe(f) {
    const title = `「${short(f.title || f.item_id, 20)}」`;
    if (f.action === "undo") return `撤销了对${title}的调整`;
    if (f.action === "reply") return `在通知里回复${title}：${f.value}`;
    if (f.action === "reply_interpreted") return `理解了你对${title}的回复：${f.summary}`;
    if (f.action === "proposal_accepted") return `接受了提议：${f.summary}`;
    if (f.action === "proposal_rejected") return `没采纳提议：${f.summary}`;
    if (f.action === "dismiss") return `划掉了${title}的提醒`;
    if (f.action === "open") return `点开了${title}的提醒`;
    if (f.action === "test") return "测试通知收到了回应";
    if (f.action === "move") return `把${title}放到「${Q[f.value]?.name || f.value}」`;
    if (f.action === "notification") return `在通知里对${title}选了「${{ start: "开始", done: "已完成", not_important: "不重要", later: "稍后" }[f.value] || f.value}」`;
    return `把${title}标成「${DONE_LABEL[f.value] || f.value}」`;
  }
  function fmtTime(iso) { const d = new Date(iso); return isNaN(d) ? "" : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`; }
  const stageOptions = (current) => (state.memory.stages || []).map((s) => `<option value="${s}" ${s === current ? "selected" : ""}>${STAGES[s] || s}</option>`).join("");

  // When the agent last re-read the projects with the profile (daily, after 09:00).
  function inferenceLine(s) {
    if (!s || !s.started_at) return '<p class="infer-status">还没运行过（每天 9 点后，配置了模型才会运行）</p>';
    if (!s.finished_at) return `<p class="infer-status running">正在按画像重读项目…（${esc(since(s.started_at))}开始）</p>`;
    const n = s.projects_inferred || 0, when = esc(since(s.finished_at));
    if (s.error && !n) return `<p class="infer-status error" title="${esc(s.error)}">上次按画像重读项目：${when}，没有成功（${esc(short(s.error, 40))}）</p>`;
    const done = n ? `更新了 ${n} 个项目` : "没有需要更新的项目";
    return `<p class="infer-status"${n ? ` title="${esc((s.projects || []).join("、"))}"` : ""}>上次按画像重读项目：${when}，${done}${s.error ? "，有部分没成功" : ""}</p>`;
  }

  function memoryView() {
    const m = state.memory;
    if (!m) return '<div class="loading"><p>正在读取记忆…</p></div>';
    const rows = Object.entries(m.projects).sort((a, b) => String(b[1].updated_at).localeCompare(String(a[1].updated_at)))
      .map(([name, e]) => `<tr data-project="${esc(name)}"><td>${esc(name)}</td><td><select data-field="stage" aria-label="${esc(name)} 的阶段">${stageOptions(e.stage)}</select></td><td><input data-field="next_step" value="${esc(e.next_step || "")}" placeholder="下一步"></td><td><input type="date" data-field="next_at" value="${esc((e.next_at || "").slice(0, 10))}" aria-label="计划日期"></td><td><input data-field="note" value="${esc(e.note || "")}" placeholder="备注"></td><td><button type="button" class="ghost" data-save-project>保存</button></td></tr>`).join("");
    const log = m.feedback.map((f) => `<li><time>${esc(fmtTime(f.timestamp))}</time><span>${esc(describe(f))}</span></li>`).join("")
      || "<li><time></time><span>还没有校准记录。在看板上挪一挪卡片，这里就会出现。</span></li>";
    return `<header class="page-head"><h1>它对你的认识</h1><p>agent 读这些来判断事情的轻重和阶段。你在看板上的每次调整都会记在右边；这里的内容也可以直接改。</p></header>
      <div class="mem-grid"><article class="panel"><h2>画像</h2><p class="hint">你的习惯与偏好，用你自己的话写。</p>${inferenceLine(m.inference_status)}<textarea id="profile" aria-label="用户画像">${esc(m.profile)}</textarea><div class="row-actions"><button type="button" class="primary" data-save-profile>保存画像</button></div></article>
      <article class="panel"><h2>最近的校准</h2><ol class="log">${log}</ol></article>
      <article class="panel wide"><h2>项目阶段</h2><p class="hint">「已成工具」「已完成」「被取代」的项目不会被提醒；「暂停·有计划」只在计划那天提醒。</p><table class="stages"><thead><tr><th>项目</th><th>阶段</th><th>下一步</th><th>日期</th><th>备注</th><th></th></tr></thead><tbody>${rows}
        <tr data-new><td><input data-field="name" placeholder="项目名" aria-label="新项目名"></td><td><select data-field="stage" aria-label="新项目阶段">${stageOptions("active")}</select></td><td><input data-field="next_step" placeholder="下一步"></td><td><input type="date" data-field="next_at" aria-label="计划日期"></td><td><input data-field="note" placeholder="备注"></td><td><button type="button" class="ghost" data-save-project>添加</button></td></tr></tbody></table></article></div>`;
  }

  const ANSWER = { start: "开始", done: "已完成", later: "稍后", not_important: "不重要", reply: "回复", dismiss: "划掉", open: "点开" };
  function notifySection() {
    const n = state.notify;
    if (!n) return '<section class="notify"><p>正在读取通知状态…</p></section>';
    const native = n.channel === "native";
    const status = native ? '<p class="ok">已允许系统通知：通知里有「开始 / 已完成 / 稍后 / 不重要」和「回复…」。</p>'
      : `<p class="warn">还没有获得通知授权，现在只能显示不带按钮的通知。点「发一条测试通知」会请求授权；如果之前点过「不允许」，请到 系统设置 → 通知 → Getting Things Done 打开。${n.detail ? `<br><small>${esc(n.detail)}</small>` : ""}</p>`;
    const test = n.test_response ? `<p class="ok">收到你的回应：${esc(ANSWER[n.test_response.action] || n.test_response.action)}${n.test_response.text ? `「${esc(n.test_response.text)}」` : ""}</p>`
      : n.test_sent_at ? '<p class="hint">测试通知已发出，点一下通知里的按钮试试。</p>' : "";
    const recent = (n.recent || []).slice(0, 6).map((r) => `<li><time>${esc(fmtTime(r.at))}</time><span>${esc(short(r.title, 30))}${r.buttons === false ? "（无按钮）" : ""}</span></li>`).join("") || '<li><time></time><span>还没有发过看板通知。</span></li>';
    const q = n.quiet_hours || {};
    return `<section class="notify panel"><h2>通知</h2><p class="hint">只为「先做」里的事发通知（同一件 12 小时内不重复），另外每天最多提醒一件「可能忘了」的重要事。你在通知里的每次点击和回复都会成为反馈。</p>${status}
      <div class="notify-row"><label class="switch"><input type="checkbox" id="notify-enabled" ${n.enabled !== false ? "checked" : ""}> 开启看板通知</label>
      <label>安静时段 <input type="time" id="quiet-start" value="${esc(q.start || "22:00")}"> – <input type="time" id="quiet-end" value="${esc(q.end || "09:00")}"></label>
      <button type="button" class="ghost" data-notify-save>保存</button><button type="button" class="primary" data-notify-test>发一条测试通知</button></div>${test}
      <h3>最近发出的</h3><ol class="log">${recent}</ol></section>`;
  }
  function ownedSection() {
    const o = state.owned;
    if (!o) return "";
    const line = o.ready ? '<p class="ok">文枢日历：已创建。日历和提醒事项里各有一个「文枢」，文枢只直接改这两个；你自己的日历仍要你点头。</p>'
      : '<p class="hint">文枢日历：还没创建。创建后，文枢会在你的默认账户里建一个「文枢」日历和一个「文枢」提醒列表，只在这两个里直接安排事情。</p>';
    return `<section class="panel owned-cal"><h2>文枢日历</h2>${line}${o.ready ? "" : '<button type="button" class="primary" data-owned-setup>创建</button>'}</section>`;
  }
  function settingsView() {
    return `<div class="settings"><header class="page-head"><h1>设置</h1><p>通知怎么打扰你，以及 agent 用哪个模型。</p></header>${notifySection()}${ownedSection()}<div id="dialog-content">${state.providers ? window.GTDProviders.section(state.providers) : "<p>正在读取…</p>"}</div><p class="hint">Finder 标签、Codex 模型等其他设置在 <a href="/classic">经典工作台</a>。</p></div>`;
  }

  function freshness() {
    const node = $("#freshness"), b = state.board;
    if (!b) return;
    node.className = "freshness live";
    node.textContent = `活动记录 · ${ago(b.activity_at)}${b.refreshing ? " · 正在重新整理" : ""}`;
  }

  function render() {
    document.querySelectorAll(".nav").forEach((n) => n.setAttribute("aria-current", n.dataset.view === state.view ? "page" : "false"));
    const scroll = {};
    document.querySelectorAll("[data-drop]").forEach((z) => { scroll[z.dataset.drop] = z.scrollTop; });
    main.innerHTML = state.view === "memory" ? memoryView() : state.view === "settings" ? settingsView() : boardView();
    document.querySelectorAll("[data-drop]").forEach((z) => { z.scrollTop = scroll[z.dataset.drop] || 0; });
    state.fresh = null;
    freshness();
  }

  async function loadBoard() { state.board = await api("/api/board"); state.lastLoad = Date.now(); if (state.view === "board") render(); else freshness(); }
  async function go(view) {
    state.view = view; closeMenu();
    history.replaceState(null, "", "#" + view);
    render();
    try {
      if (view === "board") await loadBoard();
      if (view === "memory") { state.memory = await api("/api/memory"); render(); }
      if (view === "settings") { [state.providers, state.notify, state.owned] = await Promise.all([window.GTDProviders.load(), api("/api/notify"), api("/api/settings/owned-calendar").catch(() => null)]); render(); }
    } catch (error) { toast(error.message, { error: true }); }
  }

  async function feedback(id, action, value) {
    const item = itemById(id);
    closeMenu();
    try {
      const result = await api("/api/board/feedback", { item_id: id, action, value, title: item?.title || "" });
      state.board = result; state.lastLoad = Date.now(); state.fresh = id; render();
      const name = `「${short(item?.title)}」`;
      toast(action === "move" ? `已把${name}放到「${Q[value].name}」` : value === "not_important" ? `已收起${name}，30 天内不再出现`
        : value === "done" ? `已完成${name}` : `记下了：${name}${DONE_LABEL[value]}`, { undo: result.undo });
    } catch (error) { toast(error.message, { error: true }); }
  }

  async function decideProposal(id, accept) {
    if (state.proposalBusy.has(id)) return;
    const owner = state.board?.items.find((i) => (i.proposals || []).some((p) => p.id === id));
    const p = owner?.proposals.find((x) => x.id === id);
    state.proposalBusy.add(id); delete state.proposalError[id]; render();
    try {
      const result = await api("/api/board/proposal", { id, accept });
      state.board = result; state.lastLoad = Date.now();
      if (owner) state.fresh = owner.id;
      const day = p ? dayLabel(p.due).replace(/<[^>]+>/g, "") : "新日期";
      const done = !accept ? "好，不改" : p?.kind === "new_task" ? `已新建「${short(p.title, 14)}」${result.applied_to === "owned_list" ? "，放进了「文枢」提醒列表" : ""}`
        : result.applied_to === "owned_calendar" ? `日程已改到 ${day}` : result.applied_to === "board_only" ? `看板里改到 ${day}（日历没动）` : `截止已改到 ${day}`;
      toast(result.refresh_pending ? done + "，提醒事项稍后同步" : done);
    } catch (error) {
      state.proposalError[id] = error.message;
    } finally {
      state.proposalBusy.delete(id); render();
      document.querySelector(`[data-id="${CSS.escape(owner?.id || "")}"] [data-proposal], [data-id="${CSS.escape(owner?.id || "")}"] .more`)?.focus();
    }
  }

  function openMenu(button) {
    const item = itemById(button.dataset.menu);
    if (!item) return;
    const moves = [1, 2, 3, 4].filter((q) => q !== item.quadrant).map((q) => `<button type="button" role="menuitem" data-act="move" data-val="${q}">移到「${Q[q].name}」</button>`).join("");
    const project = item.kind === "project" ? '<button type="button" role="menuitem" data-act="status" data-val="tool">已经变成工具</button><button type="button" role="menuitem" data-act="status" data-val="blocked">卡住了</button>' : "";
    menu.innerHTML = `<div class="label">状态</div><button type="button" role="menuitem" data-act="status" data-val="doing">正在做</button><button type="button" role="menuitem" data-act="status" data-val="done">已完成</button>${project}<button type="button" role="menuitem" data-act="status" data-val="not_important">不重要</button><hr><div class="label">位置</div>${moves}`;
    menu.hidden = false;
    const rect = button.getBoundingClientRect();
    menu.style.top = Math.min(rect.bottom + 6, window.innerHeight - menu.offsetHeight - 10) + "px";
    menu.style.left = Math.max(10, Math.min(rect.right - menu.offsetWidth, window.innerWidth - menu.offsetWidth - 10)) + "px";
    state.menuFor = item.id; state.menuAt = Date.now(); button.setAttribute("aria-expanded", "true");
    menu.querySelector("button").focus();
  }
  function closeMenu() {
    if (!state.menuFor) return;
    document.querySelectorAll('[data-menu][aria-expanded="true"]').forEach((b) => b.setAttribute("aria-expanded", "false"));
    menu.hidden = true; state.menuFor = null;
  }

  async function saveProject(row) {
    const field = (f) => row.querySelector(`[data-field="${f}"]`)?.value ?? "";
    const name = row.dataset.project || field("name").trim();
    try {
      state.memory = await api("/api/memory/project", { name, stage: field("stage"), next_step: field("next_step"), next_at: field("next_at") || null, note: field("note") });
      render(); toast(`已记下「${short(name)}」的阶段`);
    } catch (error) { toast(error.message, { error: true }); }
  }

  document.addEventListener("click", async (event) => {
    if (!menu.contains(event.target) && !event.target.closest("[data-menu]")) closeMenu();
    const t = event.target.closest("button");
    if (!t || t.closest("[data-provider-action]") || t.dataset.providerAction) return;
    if (t.dataset.view) return go(t.dataset.view);
    if (t.dataset.menu) return state.menuFor === t.dataset.menu ? closeMenu() : openMenu(t);
    if (t.dataset.act) return feedback(state.menuFor, t.dataset.act, t.dataset.act === "move" ? Number(t.dataset.val) : t.dataset.val);
    if (t.dataset.done) return feedback(t.dataset.done, "status", "done");
    if (t.dataset.proposal) return decideProposal(t.dataset.proposal, t.dataset.accept === "1");
    if (t.dataset.notes) { state.notesOpen.has(t.dataset.notes) ? state.notesOpen.delete(t.dataset.notes) : state.notesOpen.add(t.dataset.notes); return render(); }
    if (t.dataset.children) { state.expanded.has(t.dataset.children) ? state.expanded.delete(t.dataset.children) : state.expanded.add(t.dataset.children); return render(); }
    if (t.hasAttribute("data-q4")) { state.q4Open = !state.q4Open; return render(); }
    if (t.hasAttribute("data-undo")) {
      const token = state.undo; toastBox.hidden = true;
      try { state.board = await api("/api/board/undo", { token }); render(); toast("已撤销"); } catch (error) { toast(error.message, { error: true }); }
      return;
    }
    if (t.hasAttribute("data-refresh")) {
      try { state.board = await api("/api/board/refresh", {}); render(); toast(state.board.refreshing ? "正在重新扫描最近的活动，稍后自动更新" : "看板已是最新"); } catch (error) { toast(error.message, { error: true }); }
      return;
    }
    if (t.dataset.ignore) {
      try { await api("/api/dismiss", { candidate_key: t.dataset.ignore }); await loadBoard(); toast("已忽略这条线索"); } catch (error) { toast(error.message, { error: true }); }
      return;
    }
    if (t.hasAttribute("data-save-profile")) {
      try { state.memory = await api("/api/memory/profile", { text: $("#profile").value }); render(); toast("画像已保存，agent 下次判断时会读到"); } catch (error) { toast(error.message, { error: true }); }
      return;
    }
    if (t.hasAttribute("data-save-project")) return saveProject(t.closest("tr"));
    if (t.hasAttribute("data-notify-save")) {
      try { state.notify = await api("/api/notify/settings", { enabled: $("#notify-enabled").checked, quiet_start: $("#quiet-start").value, quiet_end: $("#quiet-end").value }); render(); toast("通知设置已保存"); } catch (error) { toast(error.message, { error: true }); }
      return;
    }
    if (t.hasAttribute("data-owned-setup")) {
      t.disabled = true;
      try { state.owned = await api("/api/settings/owned-calendar", {}); render(); toast("已创建「文枢」日历和提醒列表"); }
      catch (error) { t.disabled = false; toast(error.message, { error: true }); }
      return;
    }
    if (t.hasAttribute("data-notify-test")) {
      try {
        state.notify = await api("/api/notify/test", {}); render();
        toast(state.notify.channel === "native" ? "测试通知已发出" : "已发出无按钮的通知；请允许系统通知以使用按钮", { error: state.notify.channel !== "native" });
        clearInterval(state.notifyPoll);
        const until = Date.now() + 120000;   // watch for the response for two minutes
        state.notifyPoll = setInterval(async () => {
          if (state.view !== "settings" || Date.now() > until) return clearInterval(state.notifyPoll);
          const value = await api("/api/notify").catch(() => null);
          if (value) { state.notify = value; if (value.test_response) { clearInterval(state.notifyPoll); render(); toast("收到你在通知里的回应了"); } }
        }, 3000);
      } catch (error) { toast(error.message, { error: true }); }
      return;
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.menuFor) { const id = state.menuFor; closeMenu(); document.querySelector(`[data-menu="${CSS.escape(id)}"]`)?.focus(); }
  });
  // Scrolling closes the menu, except the scroll that brought the clicked card into view.
  window.addEventListener("scroll", () => { if (Date.now() - (state.menuAt || 0) > 300) closeMenu(); }, true);

  // Drag a card into another quadrant = "this is where it belongs" (feedback with a pin).
  const zoneOf = (target) => target.closest?.(".quadrant")?.querySelector("[data-drop]");
  document.addEventListener("dragstart", (event) => {
    const node = event.target.closest?.(".card");
    if (!node) return;
    state.drag = node.dataset.id; node.classList.add("dragging"); closeMenu();
    event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", node.dataset.id);
  });
  document.addEventListener("dragend", () => {
    state.drag = null;
    document.querySelectorAll(".dragging, .drop-target").forEach((n) => n.classList.remove("dragging", "drop-target"));
  });
  document.addEventListener("dragover", (event) => {
    const zone = state.drag && zoneOf(event.target);
    if (!zone) return;
    event.preventDefault();
    document.querySelectorAll(".drop-target").forEach((n) => n !== zone && n.classList.remove("drop-target"));
    zone.classList.add("drop-target");
  });
  document.addEventListener("drop", (event) => {
    const zone = state.drag && zoneOf(event.target);
    if (!zone) return;
    event.preventDefault();
    const item = itemById(state.drag), quadrantNumber = Number(zone.dataset.drop);
    if (item && item.quadrant !== quadrantNumber) feedback(item.id, "move", quadrantNumber);
  });

  (async () => {
    const params = new URLSearchParams(location.hash.slice(1));
    const token = params.get("access"), view = params.get("view") || location.hash.slice(1);
    state.view = ["board", "memory", "settings"].includes(view) ? view : "board";
    if (token) {
      try { await api("/api/session", {}, token); }
      catch (error) { main.innerHTML = `<div class="loading"><p>请从桌面「文枢」重新打开。${esc(error.message)}</p></div>`; return; }
    }
    window.GTDProviders.init({ api, toast: (text, error) => toast(text, { error }),
      reopen: async () => { state.providers = await window.GTDProviders.load(); if (state.view === "settings") render(); }, reload: loadBoard });
    await go(state.view);
    setInterval(() => {
      if (document.hidden || state.menuFor || state.drag || state.view !== "board") return;
      if (Date.now() - state.lastLoad >= (state.board?.refreshing ? 15000 : 60000)) loadBoard().catch(() => {});
    }, 5000);
  })();
})();
