"use strict";
// Model provider settings: Codex or an OpenAI-compatible endpoint (domestic APIs, relays, local servers).
window.GTDProviders = (() => {
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  let client = null, data = null, catalog = [], editing = null, tab = "preset";

  const host = url => { try { return new URL(url).host; } catch { return url; } };
  const dot = status => !status ? `<span class="provider-dot" title="尚未测试连接"></span>`
    : `<span class="provider-dot ${status.ok ? "ok" : "bad"}" title="${escape(status.ok ? `连接正常 · ${status.seconds}s` : status.error)}"></span>`;

  function card(profile) {
    const active = data.backend === "provider" && data.active === profile.id;
    const traits = [profile.disable_thinking ? "已关闭思考" : "", profile.max_tokens ? `最多 ${profile.max_tokens} token` : ""].filter(Boolean).join(" · ");
    return `<div class="provider-card ${active ? "active" : ""}">
      <div class="provider-head"><strong>${escape(profile.name)}</strong><span class="provider-badge">${escape(profile.id)}</span>${dot(profile.status)}${active ? `<span class="provider-badge current">使用中</span>` : ""}</div>
      <p>${escape(profile.model)} · ${escape(host(profile.base_url))}${traits ? ` · ${escape(traits)}` : ""}</p>
      <div class="provider-actions">${active ? "" : `<button class="button primary" type="button" data-provider-action="use" data-id="${escape(profile.id)}">使用</button>`}
        <button class="button" type="button" data-provider-action="test" data-id="${escape(profile.id)}">测试连接</button>
        <button class="button" type="button" data-provider-action="edit" data-id="${escape(profile.id)}">编辑</button>
        ${active ? "" : `<button class="button subtle danger" type="button" data-provider-action="delete" data-id="${escape(profile.id)}">删除</button>`}</div></div>`;
  }

  function section(value) {
    data = value;
    const codex = data.backend === "codex";
    const external = data.external ? `<div class="notice"><strong>正在使用外部配置文件</strong><p>${escape(data.external.path)} · ${escape(data.external.model || "无法读取")}</p></div>` : "";
    return `<h3 class="settings-section-title">模型供应商</h3>
      <p>历史整理、答复整理与快捷选项都使用这里选中的供应商。可接入国产大模型官方接口、中转站，或通过 SSH 隧道使用自己部署的模型。</p>${external}
      <div class="provider-card ${codex ? "active" : ""}"><div class="provider-head"><strong>Codex</strong><span class="provider-badge">默认</span>${codex ? `<span class="provider-badge current">使用中</span>` : ""}</div>
        <p>沿用 Codex 的登录与模型设置（见下方「模型服务」）。</p>
        <div class="provider-actions">${codex ? "" : `<button class="button primary" type="button" data-provider-action="use-codex">使用</button>`}</div></div>
      ${data.profiles.map(card).join("")}
      <button class="button" type="button" data-provider-action="add">＋ 添加供应商</button>`;
  }

  function editor() {
    const p = editing.profile || {}, isNew = !editing.profile;
    const tabs = isNew ? `<div class="provider-tabs" role="tablist"><button type="button" role="tab" aria-selected="${tab === "preset"}" data-provider-action="tab" data-tab="preset">常用模型提供商</button><button type="button" role="tab" aria-selected="${tab === "custom"}" data-provider-action="tab" data-tab="custom">自定义模型 API</button></div>` : "";
    const presets = isNew && tab === "preset" ? `<p>选择一个提供商，会自动填好 API 地址；之后只需填写密钥并选择模型。</p><div class="provider-presets">${data.presets.map(preset => `<button class="provider-preset" type="button" data-provider-action="preset" data-id="${escape(preset.id)}"><strong>${escape(preset.name)}</strong><small>${escape(host(preset.base_url))}</small></button>`).join("")}</div>` : "";
    const fill = editing.fill || {};
    const value = key => escape(fill[key] ?? p[key] ?? "");
    const form = !isNew || tab === "custom" || editing.fill ? `<form id="provider-form" autocomplete="off">
      ${isNew ? `<p>连接中转站、自部署服务或其他兼容 OpenAI 协议的接口，需填写 API 地址和模型。</p>` : ""}
      <label for="provider-id">Provider ID</label><input id="provider-id" required ${isNew ? "" : "disabled"} value="${value("id")}" placeholder="例如 deepseek、strata-local" pattern="[a-z][a-z0-9_\\-]{0,39}">
      <p class="field-hint">以小写字母开头，只含小写字母、数字、- 和 _；保存为本机私有配置文件。</p>
      <label for="provider-name">显示名称</label><input id="provider-name" value="${value("name")}" placeholder="显示名称">
      <label for="provider-url">API 地址</label><input id="provider-url" required value="${value("base_url")}" placeholder="https://api.example.com/v1">
      <p class="field-hint">需使用 https；http 只允许本机地址（例如 SSH 隧道 http://127.0.0.1:18080/v1）。</p>
      <label>API 协议</label><p class="provider-fixed">OpenAI Chat Completions（兼容）</p>
      <label for="provider-key">API 密钥</label><input id="provider-key" type="password" value="${escape(fill.api_key ?? "")}" placeholder="${isNew ? "输入 API 密钥；本地无鉴权服务可留空" : (p.has_key ? "已保存，留空则保持不变" : "当前未设置；本地无鉴权服务可留空")}">
      <div class="provider-catalog-head"><label for="provider-model">模型</label><button class="button subtle" type="button" data-provider-action="models">获取可用模型</button></div>
      <input id="provider-model" required list="provider-model-list" value="${value("model")}" placeholder="选择或直接填写模型 ID">
      <datalist id="provider-model-list">${catalog.map(id => `<option value="${escape(id)}"></option>`).join("")}</datalist>
      <div id="provider-catalog" class="provider-catalog">${catalog.length ? catalog.map(id => `<button type="button" class="provider-chip" data-provider-action="pick" data-model="${escape(id)}">${escape(id)}</button>`).join("") : `<p>点「获取可用模型」读取该地址的模型目录；目录外的模型 ID 也可以直接填写。</p>`}</div>
      <label class="provider-check"><input id="provider-thinking" type="checkbox" ${(fill.disable_thinking ?? p.disable_thinking) ? "checked" : ""}> 关闭思考模式（本地推理模型推荐，避免长时间思考或循环输出）</label>
      <label for="provider-max">最大输出 token（可选）</label><input id="provider-max" type="number" min="256" max="200000" step="1" value="${escape(fill.max_tokens ?? p.max_tokens ?? "")}" placeholder="例如 8000；留空表示不限制">
      <div class="dialog-actions"><button class="button subtle" type="button" data-provider-action="cancel">取消</button><button class="button primary" type="submit">${isNew ? "创建供应商" : "保存修改"}</button></div></form>` : `<div class="dialog-actions"><button class="button subtle" type="button" data-provider-action="cancel">取消</button></div>`;
    document.querySelector("#dialog-content").innerHTML = `<h2 id="dialog-title">${isNew ? "添加模型供应商" : `编辑「${escape(p.name)}」`}</h2>${tabs}${presets}${form}`;
  }

  const formValues = () => ({
    id: editing.profile ? editing.profile.id : document.querySelector("#provider-id").value.trim(),
    name: document.querySelector("#provider-name").value.trim(),
    base_url: document.querySelector("#provider-url").value.trim(),
    api_key: document.querySelector("#provider-key").value.trim(),
    model: document.querySelector("#provider-model").value.trim(),
    disable_thinking: document.querySelector("#provider-thinking").checked,
    max_tokens: document.querySelector("#provider-max").value ? Number(document.querySelector("#provider-max").value) : null,
  });

  function open(profile) { editing = {profile, fill: null}; catalog = []; tab = "preset"; editor(); }

  async function act(button) {
    const kind = button.dataset.providerAction, id = button.dataset.id;
    if (kind === "add") return open(null);
    if (kind === "edit") return open(data.profiles.find(p => p.id === id));
    if (kind === "cancel") { editing = null; return client.reopen(); }
    if (kind === "tab") { tab = button.dataset.tab; editing.fill = null; return editor(); }
    if (kind === "preset") { const preset = data.presets.find(p => p.id === id); const taken = data.profiles.some(p => p.id === preset.id);
      editing.fill = {id: taken ? "" : preset.id, name: preset.name, base_url: preset.base_url, disable_thinking: preset.id === "local"}; return editor(); }
    if (kind === "pick") { document.querySelector("#provider-model").value = button.dataset.model; return; }
    button.disabled = true;
    try {
      if (kind === "models") {
        const form = formValues();
        const result = await client.api("/api/providers/models", {id: editing.profile?.id, base_url: form.base_url, api_key: form.api_key});
        if (!result.ok) { client.toast(`获取失败：${result.error}`, true); return; }
        editing.fill = {...form, id: form.id}; catalog = result.models; editor();
        client.toast(catalog.length ? `找到 ${catalog.length} 个模型。` : "该地址没有返回任何模型，可直接填写模型 ID。");
      } else if (kind === "test") {
        const result = await client.api("/api/providers/test", {id});
        client.toast(result.ok ? `连接正常，${result.seconds} 秒返回。` : `连接失败：${result.error}`, !result.ok); await client.reopen();
      } else if (kind === "use" || kind === "use-codex") {
        await client.api("/api/providers/use", kind === "use" ? {backend: "provider", id} : {backend: "codex"});
        client.toast("已切换，从下一次模型调用生效。"); await client.reopen(); await client.reload();
      } else if (kind === "delete") {
        // Two-step confirmation in place: the native WKWebView has no JavaScript confirm() panel.
        if (button.dataset.armed !== "1") { button.dataset.armed = "1"; button.textContent = "确认删除？（密钥一并删除）"; return; }
        await client.api("/api/providers/delete", {id}); client.toast("已删除。"); await client.reopen();
      }
    } catch (error) { client.toast(error.message, true); }
    finally { button.disabled = false; }
  }

  async function submit(form) {
    const button = form.querySelector("[type=submit]"); button.disabled = true;
    try { await client.api("/api/providers/save", formValues()); editing = null; client.toast("供应商已保存。使用前可以先测试连接。"); await client.reopen(); }
    catch (error) { client.toast(error.message, true); }
    finally { button.disabled = false; }
  }

  document.addEventListener("click", event => { const button = event.target.closest("[data-provider-action]"); if (button && client) { event.preventDefault(); act(button); } });
  document.addEventListener("submit", event => { if (event.target.id === "provider-form" && client) { event.preventDefault(); submit(event.target); } });

  return { init: value => { client = value; }, section, load: () => client.api("/api/providers") };
})();
