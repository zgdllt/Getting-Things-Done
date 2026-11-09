"use strict";
window.GTDFileLibrary = (() => {
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const colors = ["none", "gray", "green", "purple", "blue", "yellow", "red", "orange"];
  const labels = {important_urgent:"重要且紧急", important:"重要不紧急", urgent:"紧急但较不重要", later:"可暂缓", done:"已完成", undecided:"待判断"};
  let data = null, client = null, error = "", filter = "suggested", tagFilter = "", selected = new Set();
  let loading = false, plan = null, busy = false, generation = 0, committing = false, page = 0, searchTimer = null;
  const size = bytes => bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + " MB" : (bytes / 1024).toFixed(1) + " KB";
  const chip = (name, color) => `<span class="file-tag file-tag-${colors[color] || "none"}"><i aria-hidden="true"></i>${escape(name)}</span>`;
  const tagColor = name => data?.tags.find(tag => tag.name === name)?.color || 0;
  const redraw = () => { if(client?.visible()) client.redraw(); };
  async function load() {
    const request = ++generation;
    try {
      const params = new URLSearchParams({page,limit:40,filter,tag:tagFilter,q:client.query()});
      const value = await client.api("/api/files?" + params);
      if(request !== generation) return;
      data = value; page = data.page; error = "";
      data.items.filter(item=>!item.present||item.status!=="suggested").forEach(item=>selected.delete(item.id));
    } catch(failure) { error = failure.message; }
    redraw();
  }
  async function enter() {
    if(!client) return;
    await load();
    if(data && !data.initialized && !data.paused) {
      await client.api("/api/files/projects",{});
      await load();
    }
  }
  function options(category) {
    return Object.entries(labels).filter(([key]) => key === "undecided" || data.mapping[key]).map(([key, title]) =>
      `<option value="${key}" ${category === key ? "selected" : ""}>${escape(title)}${data.mapping[key] ? " · " + escape(data.mapping[key]) : ""}</option>`).join("");
  }
  function renderItem(item) {
    const name = data.mapping[item.category];
    const eligible = item.status === "suggested" && name && !item.tags.includes(name);
    return `<article class="file-card" data-file-item="${escape(item.id)}">
      <div class="file-card-top"><span class="file-glyph" aria-hidden="true">${item.directory ? "▱" : "▤"}</span><div>
        <h2>${escape(item.name)}</h2><p class="file-path">${escape(item.path)}</p></div>
        ${eligible ? `<label class="file-select"><input type="checkbox" data-file-select="${escape(item.id)}" ${selected.has(item.id) ? "checked" : ""}>选入本批</label>` : ""}</div>
      <div class="file-current"><small>现有标签</small>${item.tags.length ? item.tags.map(tag => chip(tag, item.tag_colors?.[tag] || tagColor(tag))).join("") : '<span class="file-muted">尚无标签</span>'}</div>
      <div class="file-suggestion"><label>处理建议<select data-file-category="${escape(item.id)}" ${busy ? "disabled" : ""}>${options(item.category)}</select></label>
      ${name ? `<div class="file-proposed"><small>${item.tags.includes(name) ? "已具有此标签" : "建议添加"}</small>${chip(name, tagColor(name))}</div>` : ""}</div>
      <p class="file-reason">${escape(item.reason || (item.status === "pending" ? "agent 正在准备建议。" : "依据不足，先保留待判断。"))}</p>
      ${item.activity ? `<small class="file-muted">${escape(item.source === "codex" ? "Codex 项目根目录" : "手动加入")} · ${item.activity.last_conversation ? "最近对话 " + escape(new Date(item.activity.last_conversation*1000).toLocaleDateString()) : "未找到对话记录"}<br>${escape(item.activity.note)}</small>` : ""}
      ${item.model ? `<small class="file-muted">${escape(item.model)} · ${escape(item.reasoning_effort)}</small>` : ""}
      <div class="file-actions"><button class="button small subtle" data-file-open="${escape(item.id)}">打开</button>
      <button class="button small subtle" data-file-reveal="${escape(item.id)}">在 Finder 中显示 ↗</button>
      ${item.status !== "dismissed" ? `<button class="button small subtle" data-file-dismiss="${escape(item.id)}">暂不整理</button>` : ""}</div></article>`;
  }
  function render() {
    if(!data) return `<div class="loading"><h1>把资料也理一理。</h1><p>${escape(error || "正在读取资料工作区…")}</p><button class="button" data-files-action="reload">重新读取</button></div>`;
    const present = data.items.filter(item => item.present);
    const items = present.filter(item =>
      (filter === "all" || (filter === "suggested" ? ["pending", "suggested"].includes(item.status) : item.status === filter)) &&
      (!tagFilter || item.tags.includes(tagFilter) || data.mapping[item.category] === tagFilter) &&
      client.matches(`${item.name} ${item.path} ${item.reason} ${item.tags.join(" ")}`));
    const job = data.job;
    const active = ["pending", "processing"].includes(job.state);
    return `<div class="heading"><div><p class="eyebrow">REFERENCE · ROOM FOR YOUR FILES</p><h1>按轻重缓急，找到手边的资料。</h1>
      <p class="intro">agent 提建议，你确认后添加 Finder 标签。文件留在原位，绿色表示已完成。</p></div><span class="edition">05</span></div>
      ${error ? `<div class="notice" role="alert">${escape(error)}</div>` : ""}
      <section class="file-toolbar"><button class="button primary" data-files-action="projects">同步 Codex 项目</button>
        <button class="button" data-files-action="choose" ${data.paused?"disabled":""}>＋ 添加文件或文件夹</button>
        <button class="button" data-files-action="scan" ${!data.scopes.length || active || busy || data.paused ? "disabled" : ""}>${active ? "正在准备建议…" : "刷新建议"}</button>
        <button class="button" data-files-action="${data.paused?"resume":"pause"}">${data.paused?"恢复整理":"暂停整理"}</button></section>
      <p class="quiet-note">每个项目或所选文件夹只占一项，子目录不展开。闲置 ${data.idle_days} 天或活动记录不足时建议灰色；已有绿色保留为完成。</p>
      ${data.scopes.length ? `<details class="file-scopes"><summary>${data.scopes.length} 个项目与所选项 · ${data.observer_alive ? "正在观察活动" : data.paused ? "整理已暂停" : "观察未运行，可恢复整理"}</summary>${data.scopes.map(scope =>
        `<div><span>${escape(scope.name)}<small>${escape(scope.path)} · ${scope.source==="codex"?"来自 Codex":"手动加入"} · 仅此项</small></span><button class="button small subtle" data-file-remove="${escape(scope.id)}">移出范围</button></div>`).join("")}</details>` : ""}
      ${data.project_errors?.length ? `<div class="notice">${data.project_errors.map(escape).join("<br>")}</div>` : ""}
      ${job.state !== "idle" ? `<div class="${job.state === "error" ? "notice" : "file-job"}" role="status">${active ? '<span class="loading-mark"></span>' : ""}<span>${escape(job.summary || "")}${job.error ? `<br>${escape(job.error)} <button class="button small" data-files-action="scan">重试建议</button>` : ""}</span></div>` : ""}
      ${data.scan_errors.length ? `<details class="notice"><summary>${data.scan_errors.length} 项无法读取</summary>${data.scan_errors.map(item => `<p>${escape(item.path)} · ${escape(item.error)}</p>`).join("")}</details>` : ""}
      ${!data.scopes.length ? '<section class="panel empty"><span class="empty-glyph">▱</span><h2>从 Codex 项目开始整理。</h2><p>同步本机项目根目录，也可以自行添加文件或文件夹。只读取所选项的名称和标签，不递归列出目录内容。</p></section>' :
      `<div class="filters">${[["suggested","待判断与建议"],["applied","已应用"],["dismissed","暂不整理"],["all","全部"]].map(([key,title]) =>
        `<button class="filter ${filter === key ? "active" : ""}" data-file-filter="${key}">${title}</button>`).join("")}
      <label class="file-filter-label">按标签<select id="files-tag-filter"><option value="">全部标签</option>${data.tags.map(tag =>
        `<option value="${escape(tag.name)}" ${tag.name === tagFilter ? "selected" : ""}>${escape(tag.name)}</option>`).join("")}</select></label></div>
      ${items.length ? client.collection("files:"+filter+":"+tagFilter,items,item=>item.id,renderItem,"资料卡片") : '<section class="panel empty"><h2>这里暂时没有资料。</h2><p>可以换个筛选，或等 agent 整理完成。</p></section>'}
      <div class="file-pages"><span>共 ${data.total} 项 · 第 ${data.page+1} / ${Math.max(1,Math.ceil(data.total/data.limit))} 页 · 每页最多 ${data.limit} 项</span><div class="actions">
      <button class="button small" data-files-action="previous" ${!page?"disabled":""}>上一页</button><button class="button small" data-files-action="next" ${(page+1)*data.limit>=data.total?"disabled":""}>下一页</button></div></div>
      <div class="inbox-footer"><p>已有标签保留。先核对建议，预览后再应用。</p><div class="actions"><button class="button subtle" data-files-action="select">选中本页可应用建议</button><button class="button primary" data-files-action="prepare" ${!selected.size || busy ? "disabled" : ""}>预览 ${selected.size} 项 →</button></div></div>`}
      ${data.operations.length ? `<details class="file-history"><summary>最近标签操作 · ${data.operations.length}</summary>${data.operations.map(op =>
        `<div><span>${escape(new Date(op.created_at).toLocaleString())} · ${op.success_count} 项已添加
        ${op.results.filter(item=>!item.ok).map(item=>`<small>${escape(item.path)} · ${escape(item.error)}</small>`).join("")}
        ${op.undo_results?.filter(item=>!item.ok).map(item=>`<small>${escape(item.error)}</small>`).join("")}</span>
        ${!["undone","applying","undoing"].includes(op.state) ? `<button class="button small" data-file-undo="${escape(op.id)}">撤销本次添加</button>` : `<small>${op.state === "undone" ? "已撤销" : "回执待核对"}</small>`}</div>`).join("")}</details>` : ""}`;
  }
  function showDialog(html) {
    document.querySelector("#dialog-content").innerHTML = html;
    const dialog=document.querySelector("#confirm-dialog");if(!dialog.open)dialog.showModal();
  }
  function mappingDialog() {
    showDialog(`<h2 id="dialog-title">颜色与重要／紧急程度</h2><p>只使用现有 Finder 标签。绿色固定表示已完成；其他映射可以调整，文件原有标签不会被清除。</p>
      <form id="file-mapping-form">${Object.entries(labels).filter(([key])=>key!=="undecided").map(([key,title]) =>
        `<label>${escape(title)}<select name="${key}"><option value="">暂不映射</option>${data.tags.filter(tag=>key!=="done" || tag.color===2).map(tag =>
          `<option value="${escape(tag.name)}" ${data.mapping[key]===tag.name?"selected":""}>${escape(tag.name)}</option>`).join("")}</select></label>`).join("")}
      <div class="dialog-actions"><button type="button" class="button subtle" data-close-dialog>取消</button><button class="button primary">保存映射</button></div></form>`);
  }
  function cacheDialog() {
    showDialog(`<h2 id="dialog-title">资料整理与缓存</h2>
      <p>缓存目录：<code class="file-path">${escape(data.cache.path)}</code></p>
      <dl class="file-cache"><dt>当前资料索引</dt><dd>${size(data.cache.index_bytes)} · ${data.item_count} 项</dd>
        <dt>旧扫描存档</dt><dd>${size(data.cache.archive_bytes)} · ${data.cache.archives.length} 份</dd></dl>
      <p>清理只删除本机的扫描结果。原文件、Finder 标签、提醒事项、日历和标签操作回执都会保留。</p>
      <form id="file-policy-form"><label>闲置期限（天）<input type="number" name="idle_days" min="1" max="3650" value="${data.idle_days}" required></label>
      <p>长期无对话、无文件变动，或活动证据不足时，建议灰色。灰色仍需确认后应用；绿色表示已完成。</p>
      <button class="button" type="submit">保存整理设置</button></form>
      <h3 class="settings-section-title">清理范围</h3><form id="file-cache-form">
      ${data.cache.archives.map(item=>`<label class="file-cache-option"><input type="checkbox" name="archive" value="${escape(item.name)}" checked><span>${escape(item.name)} · ${size(item.bytes)}</span></label>`).join("")}
      <label class="file-cache-option"><input type="checkbox" name="reset_index"><span>同时清空当前索引与建议，并暂停整理；保留项目范围、设置和撤销记录。</span></label>
      <div class="dialog-actions"><button class="button subtle" type="button" data-close-dialog>关闭</button><button class="button" type="submit">清理所选缓存</button></div></form>`);
  }
  function picker() {
    if(window.webkit?.messageHandlers?.gtdFilesPicker) {
      window.webkit.messageHandlers.gtdFilesPicker.postMessage({});
      return;
    }
    showDialog(`<h2 id="dialog-title">加入资料工作区</h2><p>桌面 App 支持系统选择器。浏览器中可以填写本机绝对路径，每行一项。</p><form id="file-path-form">
      <label>文件或文件夹路径<textarea name="paths" rows="4" placeholder="粘贴从 Finder 复制的绝对路径，每行一项" required></textarea></label>
      <p>仅整理所选文件或文件夹本身，不扫描子目录。</p>
      <div class="dialog-actions"><button class="button subtle" type="button" data-close-dialog>取消</button><button class="button primary">加入并准备建议</button></div></form>`);
  }
  async function action(button) {
    if(busy) return;
    busy = true; button.disabled = true;
    try {
      const kind = button.dataset.filesAction;
      if(kind === "choose") { picker(); return; }
      if(kind === "cache") { await load(); cacheDialog(); return; }
      if(kind === "mapping") {
        await load();
        if(!data?.tags.length){client.toast("请先在「资料」页选择工作区，以读取现有 Finder 标签。");return;}
        mappingDialog(); return;
      }
      if(kind === "reload") { await load(); return; }
      if(kind === "select") {
        data.items.filter(item=>item.present&&item.status==="suggested"&&data.mapping[item.category]&&!item.tags.includes(data.mapping[item.category]))
          .forEach(item=>selected.add(item.id));
      } else if(kind === "projects") {
        await client.api("/api/files/projects",{});page=0;
      } else if(kind === "pause") await client.api("/api/files/pause",{});
      else if(kind === "resume") await client.api("/api/files/resume",{});
      else if(kind === "next") page++;
      else if(kind === "previous") page=Math.max(0,page-1);
      else if(kind === "scan") await client.api("/api/files/refresh",{});
      else if(kind === "prepare") {
        const result = await client.api("/api/files/prepare",{item_ids:[...selected]}); plan = result.plan;
        showDialog(`<h2 id="dialog-title">确认添加这批 Finder 标签</h2><p>文件保留原位，已有标签保留；本次只添加下面的标签。</p>
          ${plan.items.map(item=>`<div class="preview-item"><h3>${escape(item.path)}</h3><p>已有：${escape(item.before.join("、")||"无")}</p><p>添加：${item.add.map(tag=>chip(tag,tagColor(tag))).join("")}</p></div>`).join("")}
          <div class="dialog-actions"><button class="button subtle" data-close-dialog>取消</button><button class="button primary" data-files-action="commit">确认应用 ${plan.items.length} 项</button></div>`);
        return;
      } else if(kind === "commit") {
        committing = true;
        document.querySelectorAll("#confirm-dialog [data-close-dialog]").forEach(node=>node.disabled=true);
        const result = await client.api("/api/files/commit",{plan_id:plan.id});
        document.querySelector("#confirm-dialog").close(); selected.clear();
        client.toast(result.operation.state==="done" ? "标签已写入 Finder，并已核对。" : "部分文件未完成，请查看操作记录。", result.operation.state!=="done");
      } else if(button.dataset.fileUndo) {
        const result=await client.api("/api/files/undo",{operation_id:button.dataset.fileUndo});
        client.toast(result.operation.state==="undone" ? "已撤销本次添加，其他标签保留。" : "部分撤销未完成，请查看记录。",result.operation.state!=="undone");
      } else if(button.dataset.fileDismiss) await client.api("/api/files/edit",{item_id:button.dataset.fileDismiss,dismiss:true});
      else if(button.dataset.fileRemove) await client.api("/api/files/remove",{scope_id:button.dataset.fileRemove});
      else if(button.dataset.fileOpen || button.dataset.fileReveal) {
        await client.api("/api/files/open",{item_id:button.dataset.fileOpen||button.dataset.fileReveal,reveal:!!button.dataset.fileReveal}); return;
      }
      await load();
    } catch(failure) { client.toast(failure.message,true); }
    finally { busy=false; committing=false; button.disabled=false; document.querySelectorAll("#confirm-dialog [data-close-dialog]").forEach(node=>node.disabled=false); redraw(); }
  }
  document.addEventListener("click", event => {
    const button=event.target.closest("button");
    if(button && (button.dataset.filesAction || button.dataset.fileUndo || button.dataset.fileDismiss || button.dataset.fileRemove || button.dataset.fileOpen || button.dataset.fileReveal)) {
      action(button);
    }
  });
  document.addEventListener("change",async event=>{
    const target=event.target;
    if(target.dataset.fileSelect) { target.checked ? selected.add(target.dataset.fileSelect) : selected.delete(target.dataset.fileSelect); redraw(); }
    else if(target.id==="files-tag-filter") { tagFilter=target.value;page=0;load(); }
    else if(target.dataset.fileCategory) {
      if(busy)return;
      busy=true;target.disabled=true;
      try { await client.api("/api/files/edit",{item_id:target.dataset.fileCategory,category:target.value});await load(); }
      catch(failure) {client.toast(failure.message,true);}
      finally {busy=false;target.disabled=false;redraw();}
    }
  });
  document.addEventListener("click",event=>{
    const button=event.target.closest("[data-file-filter]");
    if(button){filter=button.dataset.fileFilter;page=0;load();}
  });
  document.addEventListener("input",event=>{
    if(event.target.id==="search"&&client?.visible()){
      clearTimeout(searchTimer);searchTimer=setTimeout(()=>{page=0;load();},200);
    }
  });
  document.addEventListener("submit",async event=>{
    const form=event.target;
    if(!["file-path-form","file-mapping-form","file-policy-form","file-cache-form"].includes(form.id))return;
    event.preventDefault();
    const button=form.querySelector("button[type=submit],button:not([type])");button.disabled=true;
    try{
      if(form.id==="file-path-form"){await client.api("/api/files/register",{paths:form.elements.paths.value.split("\n").map(path=>path.trim()).filter(Boolean)});}
      else if(form.id==="file-mapping-form") await client.api("/api/files/edit",{mapping:Object.fromEntries(new FormData(form))});
      else if(form.id==="file-policy-form") {
        await client.api("/api/files/edit",{idle_days:Number(form.elements.idle_days.value)});
        client.toast("整理设置已保存。");
      } else {
        const result=await client.api("/api/files/cache/clear",{archives:new FormData(form).getAll("archive"),reset_index:form.elements.reset_index.checked});
        selected.clear();client.toast(`已清理 ${size(result.freed_bytes)} 的缓存，原文件与标签保留。`);
      }
      document.querySelector("#confirm-dialog").close();await load();
    }catch(failure){client.toast(failure.message,true);}
    finally{button.disabled=false;}
  });
  window.addEventListener("gtd-files-picked",async event=>{
    if(!event.detail.paths.length)return;
    try{await client.api("/api/files/register",event.detail);await load();}
    catch(failure){client.toast(failure.message,true);}
  });
  setInterval(()=>{
    if(client?.visible()&&!busy&&!loading&&!document.hidden&&!document.querySelector("#confirm-dialog").open&&
       !document.activeElement.matches("input,textarea,select")){
      loading=true;load().finally(()=>loading=false);
    }
  },5000);
  document.querySelector("#confirm-dialog")?.addEventListener("cancel",event=>{if(committing)event.preventDefault();});
  return {init(value){client=value;},enter,render,isCommitting:()=>committing};
})();
