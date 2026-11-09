//! GTD Band Bridge
//!
//! Loopback HTTP API for Getting Things Done <-> AstroBox interconnect <-> the
//! `com.zgdllt.gtdband` quick app on a Xiaomi Smart Band 9 / 9 NFC.
//!
//! The band can only answer with one of three choices (dismiss / later / mac).
//! Nothing on the band writes to Reminders or Calendar: GTD applies the answer.
//!
//! Access: every request needs `Authorization: Bearer <token>`. The token is
//! persisted in the plugin's private directory. A client obtains it either by
//! reading `bridge.json` (desktop host) or by `POST /v1/pair` with the 6-digit
//! code shown on the plugin page (phone host reached through `adb forward`).
wit_bindgen::generate!({ path: "wit", world: "psys-world-v4-http", generate_all });

use astrobox::psys_host_v4::{device, http_server, interconnect, register, thirdpartyapp, timer, ui};
use exports::astrobox::psys_plugin_v4::{event, http, lifecycle};
use serde_json::{Value, json};
use std::collections::VecDeque;
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

const PACKAGE: &str = "com.zgdllt.gtdband";
const PAGE: &str = "pages/index";
const BRIDGE_FILE: &str = "bridge.json";
const TOKEN_FILE: &str = "token.txt";
const VERSION: &str = env!("CARGO_PKG_VERSION");
/// Fixed so `adb forward tcp:47913 tcp:47913` works; falls back to a free port.
const PORT: u16 = 47913;
const REFRESH_MS: u64 = 5_000;
const MAX_BODY: usize = 8 * 1024;
/// Conservative per-frame budget; the host/band limit is not documented.
const MAX_FRAME: usize = 3 * 1024;
const MAX_TITLE: usize = 40;
const MAX_QUESTION: usize = 160;
const MAX_OUTBOX: usize = 8;
const MAX_INBOX: usize = 200;
const MAX_PAIR_FAILURES: u32 = 5;
/// "option" = one of the model-generated answers sent with the question.
const CHOICES: [&str; 4] = ["dismiss", "later", "mac", "option"];
const MAX_OPTIONS: usize = 3;
const MAX_OPTION_CHARS: usize = 16;
const MAX_ANSWER_CHARS: usize = 40;

#[derive(Default)]
struct State {
    token: String,
    url: String,
    pair_code: String,
    pair_failures: u32,
    /// Connected devices as (name, addr).
    devices: Vec<(String, String)>,
    selected: String,
    registered_for: String,
    /// Version code + fingerprint of the band app seen at last registration.
    app_identity: String,
    /// Questions not yet answered on the band, oldest first.
    outbox: VecDeque<Value>,
    /// Answer/shown frames with a sequence number, removed by `/v1/events/ack`.
    inbox: VecDeque<Value>,
    next_seq: u64,
    last_hello_ms: u64,
    last_pong_ms: u64,
    last_error: String,
    ui_id: String,
}

fn state() -> MutexGuard<'static, State> {
    static STATE: OnceLock<Mutex<State>> = OnceLock::new();
    STATE.get_or_init(|| Mutex::new(State::default())).lock().unwrap_or_else(|e| e.into_inner())
}

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

fn set_error(message: impl Into<String>) {
    state().last_error = message.into();
}

fn random_bytes<const N: usize>() -> Result<[u8; N], String> {
    let mut bytes = [0u8; N];
    getrandom::fill(&mut bytes).map_err(|_| "secure random unavailable".to_string())?;
    Ok(bytes)
}

fn random_token() -> Result<String, String> {
    Ok(random_bytes::<32>()?.iter().map(|b| format!("{b:02x}")).collect())
}

fn new_pair_code() -> String {
    match random_bytes::<4>() {
        Ok(b) => format!("{:06}", u32::from_le_bytes(b) % 1_000_000),
        Err(_) => String::new(), // pairing disabled without secure randomness
    }
}

fn clip(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

fn id_of(frame: &Value) -> Option<&str> {
    frame.get("id").and_then(Value::as_str)
}

/// Write via a temporary file so readers never see a half-written record.
fn write_atomic(path: &str, contents: &str) -> Result<(), String> {
    let temp = format!("{path}.tmp");
    std::fs::write(&temp, contents).map_err(|e| format!("write {temp}: {e}"))?;
    std::fs::rename(&temp, path).map_err(|e| format!("rename {temp}: {e}"))
}

// ---------------------------------------------------------------- devices

async fn refresh_devices() {
    let connected: Vec<(String, String)> = device::get_connected_device_list()
        .await
        .into_iter()
        .map(|d| (d.name.trim().to_string(), d.addr))
        .collect();
    let target = {
        let mut s = state();
        s.devices = connected.clone();
        if s.selected.is_empty() && connected.len() == 1 {
            s.selected = connected[0].1.clone();
        }
        let online = connected.iter().any(|(_, addr)| addr == &s.selected);
        if !online {
            s.registered_for.clear();
        }
        (online && s.registered_for != s.selected).then(|| s.selected.clone())
    };
    if let Some(addr) = target {
        register_receive(&addr).await;
    }
}

/// Identity of the installed band app: version code + signing fingerprint.
fn app_identity(info: &thirdpartyapp::AppInfo) -> String {
    format!("{}:{:?}", info.version_code, info.fingerprint)
}

/// Refresh the host's cached app list, then register for band messages.
///
/// Measured on Band 9 NFC + AstroBox 2.2.5 (Android): a registration made
/// before the current app build was installed makes every band->host send
/// fail with 401; refreshing the list and registering again fixes it.
async fn register_receive(addr: &str) {
    if let Ok(apps) = thirdpartyapp::get_thirdparty_app_list(addr.to_string()).await {
        if let Some(info) = apps.iter().find(|app| app.package_name == PACKAGE) {
            state().app_identity = app_identity(info);
        }
    }
    match register::register_interconnect_recv(addr.to_string(), PACKAGE.into()).await {
        Ok(()) => state().registered_for = addr.to_string(),
        Err(e) => set_error(format!("register_interconnect_recv: {e}")),
    }
}

fn selected_online() -> Result<String, String> {
    let s = state();
    if s.selected.is_empty() {
        return Err("no device selected; POST /v1/select or connect exactly one band".into());
    }
    if !s.devices.iter().any(|(_, addr)| addr == &s.selected) {
        return Err("selected band is not connected in AstroBox".into());
    }
    Ok(s.selected.clone())
}

async fn launch_app(addr: &str) -> Result<(), String> {
    let apps = thirdpartyapp::get_thirdparty_app_list(addr.to_string()).await?;
    let info = apps
        .into_iter()
        .find(|app| app.package_name == PACKAGE)
        .ok_or_else(|| format!("quick app {PACKAGE} is not installed on the band"))?;
    // A reinstalled/updated band app needs a fresh receive registration.
    let changed = {
        let mut s = state();
        let identity = app_identity(&info);
        let changed = s.app_identity != identity;
        s.app_identity = identity;
        changed
    };
    if changed {
        register_receive(addr).await;
    }
    thirdpartyapp::launch_qa(addr.to_string(), info, PAGE.into()).await
}

async fn send_frame(addr: &str, frame: &Value) -> Result<(), String> {
    let text = frame.to_string();
    if text.len() > MAX_FRAME {
        return Err(format!("frame is {} bytes, limit {MAX_FRAME}", text.len()));
    }
    interconnect::send_qaic_message(addr.to_string(), PACKAGE.into(), text).await
}

/// Resend every unanswered question. Used after the band app says hello.
async fn flush_outbox() {
    let Ok(addr) = selected_online() else { return };
    let frames: Vec<Value> = state().outbox.iter().cloned().collect();
    for frame in frames {
        if let Err(e) = send_frame(&addr, &frame).await {
            set_error(format!("resend: {e}"));
            return;
        }
    }
}

// ---------------------------------------------------------------- band frames

/// Accept a raw frame, a JSON-encoded string frame, or the host envelope
/// `{addr, pkgName, payloadText}` scoped to this package and the selected band.
fn decode_band_frame(raw: &str, selected: &str) -> Result<Value, &'static str> {
    fn parse(text: &str) -> Result<Value, &'static str> {
        let value: Value = serde_json::from_str(text).map_err(|_| "invalid_json")?;
        match value {
            Value::String(inner) => serde_json::from_str(&inner).map_err(|_| "invalid_json"),
            other => Ok(other),
        }
    }
    if raw.len() > 16 * 1024 {
        return Err("too_large");
    }
    let outer = parse(raw)?;
    let frame = match outer.get("payloadText").and_then(Value::as_str) {
        Some(text) => {
            if outer.get("pkgName").and_then(Value::as_str) != Some(PACKAGE) {
                return Err("package_mismatch");
            }
            if let Some(addr) = outer.get("addr").and_then(Value::as_str) {
                if addr != selected {
                    return Err("device_mismatch");
                }
            }
            parse(text)?
        }
        None => outer,
    };
    if frame.get("v").and_then(Value::as_u64) != Some(1) {
        return Err("unsupported_version");
    }
    Ok(frame)
}

fn valid_option(frame: &Value) -> bool {
    let index_ok = frame.get("option").and_then(Value::as_u64).is_some_and(|i| (i as usize) < MAX_OPTIONS);
    let text = frame.get("text").and_then(Value::as_str).unwrap_or("").trim();
    index_ok && !text.is_empty() && text.chars().count() <= MAX_ANSWER_CHARS
}

fn push_inbox(s: &mut State, mut event: Value) {
    s.next_seq += 1;
    event["seq"] = json!(s.next_seq);
    event["receivedAt"] = json!(now_ms());
    s.inbox.push_back(event);
    // Drop the oldest "shown" records first; answers are kept as long as possible.
    while s.inbox.len() > MAX_INBOX {
        let shown = s.inbox.iter().position(|e| e.get("type").and_then(Value::as_str) == Some("shown"));
        match shown {
            Some(index) => {
                s.inbox.remove(index);
            }
            None => {
                s.inbox.pop_front();
                s.last_error = "inbox full: oldest answer dropped (is gtd_band.py running?)".into();
            }
        }
    }
}

async fn handle_band_frame(frame: Value) {
    let kind = frame.get("type").and_then(Value::as_str).unwrap_or("").to_string();
    let mut ack: Option<Value> = None;
    {
        let mut s = state();
        match kind.as_str() {
            "hello" => s.last_hello_ms = now_ms(),
            "pong" => s.last_pong_ms = now_ms(),
            "shown" => push_inbox(&mut s, frame.clone()),
            "answer" => {
                let id = id_of(&frame).unwrap_or("").to_string();
                let choice = frame.get("choice").and_then(Value::as_str).unwrap_or("");
                if id.is_empty() || !CHOICES.contains(&choice) || (choice == "option" && !valid_option(&frame)) {
                    s.last_error = "band sent an invalid answer frame".into();
                    return;
                }
                let known = s.outbox.iter().any(|q| id_of(q) == Some(id.as_str()));
                s.outbox.retain(|q| id_of(q) != Some(id.as_str()));
                let mut event = frame.clone();
                event["pending"] = json!(known); // false: withdrawn or answered twice
                push_inbox(&mut s, event);
                ack = Some(json!({"v": 1, "type": "ack", "id": id}));
            }
            _ => {}
        }
    }
    if kind == "hello" {
        // Heartbeat reply: the band shows "已连接电脑" only while it hears from us.
        if let Ok(addr) = selected_online() {
            let _ = send_frame(&addr, &json!({"v": 1, "type": "hi", "ts": now_ms()})).await;
        }
        flush_outbox().await;
    }
    if let (Some(frame), Ok(addr)) = (ack, selected_online()) {
        let _ = send_frame(&addr, &frame).await;
    }
}

// ---------------------------------------------------------------- HTTP API

fn response(status: u16, body: Value) -> http_server::Response {
    http_server::Response {
        status,
        headers: vec![
            http_server::Header { name: "content-type".into(), value: "application/json; charset=utf-8".into() },
            http_server::Header { name: "cache-control".into(), value: "no-store".into() },
        ],
        body: body.to_string().into_bytes(),
    }
}

fn error(status: u16, message: &str) -> http_server::Response {
    response(status, json!({"ok": false, "error": message}))
}

fn authorized(request: &http_server::Request) -> bool {
    let token = state().token.clone();
    if token.is_empty() {
        return false;
    }
    let expected = format!("Bearer {token}");
    request.headers.iter().any(|h| h.name.eq_ignore_ascii_case("authorization") && h.value == expected)
}

fn status_body() -> Value {
    let s = state();
    json!({
        "ok": true,
        "version": VERSION,
        "package": PACKAGE,
        "selected": s.selected,
        "registered": !s.registered_for.is_empty() && s.registered_for == s.selected,
        "devices": s.devices.iter().map(|(name, addr)| json!({"name": name, "addr": addr})).collect::<Vec<_>>(),
        "outbox": s.outbox.iter().filter_map(id_of).collect::<Vec<_>>(),
        "inbox": s.inbox.len(),
        "lastHelloMs": s.last_hello_ms,
        "lastPongMs": s.last_pong_ms,
        "nowMs": now_ms(),
        "lastError": s.last_error,
    })
}

/// Exchange the one-time code shown on the plugin page for the access token.
fn post_pair(body: &Value) -> http_server::Response {
    let code = body.get("code").and_then(Value::as_str).unwrap_or("").trim().to_string();
    let mut s = state();
    if s.pair_code.is_empty() || s.token.is_empty() {
        return error(503, "pairing unavailable");
    }
    if code != s.pair_code {
        s.pair_failures += 1;
        if s.pair_failures >= MAX_PAIR_FAILURES {
            s.pair_code = new_pair_code();
            s.pair_failures = 0;
        }
        return error(403, "wrong pairing code");
    }
    s.pair_code = new_pair_code(); // single use
    s.pair_failures = 0;
    response(200, json!({"ok": true, "token": s.token, "package": PACKAGE, "version": VERSION}))
}

async fn post_ask(body: &Value) -> http_server::Response {
    let id = body.get("id").and_then(Value::as_str).unwrap_or("").trim().to_string();
    let question = body.get("question").and_then(Value::as_str).unwrap_or("").trim().to_string();
    if id.is_empty() || id.len() > 64 || question.is_empty() {
        return error(400, "id (<=64 bytes) and question are required");
    }
    let frame = json!({
        "v": 1,
        "type": "ask",
        "id": id,
        "title": clip(body.get("title").and_then(Value::as_str).unwrap_or(""), MAX_TITLE),
        "question": clip(&question, MAX_QUESTION),
        "options": body.get("options").and_then(Value::as_array).map(|items| items
            .iter()
            .filter_map(Value::as_str)
            .map(|text| clip(text.trim(), MAX_OPTION_CHARS))
            .filter(|text| !text.is_empty())
            .take(MAX_OPTIONS)
            .collect::<Vec<_>>()).unwrap_or_default(),
        "sentAt": now_ms(),
    });
    // `replace` (default) keeps exactly this question on the band.
    let replaced: Vec<String> = {
        let mut s = state();
        let replace = body.get("replace").and_then(Value::as_bool).unwrap_or(true);
        let removed: Vec<String> = if replace {
            s.outbox.drain(..).filter_map(|q| id_of(&q).map(str::to_string)).filter(|q| q != &id).collect()
        } else {
            s.outbox.retain(|q| id_of(q) != Some(id.as_str()));
            Vec::new()
        };
        s.outbox.push_back(frame.clone());
        while s.outbox.len() > MAX_OUTBOX {
            s.outbox.pop_front();
        }
        removed
    };
    refresh_devices().await;
    let addr = match selected_online() {
        Ok(addr) => addr,
        Err(e) => return response(202, json!({"ok": true, "queued": true, "delivered": false, "error": e})),
    };
    for old in replaced {
        let _ = send_frame(&addr, &json!({"v": 1, "type": "withdraw", "id": old})).await;
    }
    let launch = body.get("launch").and_then(Value::as_bool).unwrap_or(true);
    let launched = if launch { launch_app(&addr).await } else { Ok(()) };
    // If the app was just launched, its hello triggers a resend; sending now
    // still covers the case where it was already in the foreground.
    let sent = send_frame(&addr, &frame).await;
    response(200, json!({
        "ok": true,
        "queued": true,
        "delivered": sent.is_ok(),
        "launchError": launched.err(),
        "sendError": sent.err(),
    }))
}

async fn route(request: http_server::Request) -> http_server::Response {
    if request.headers.iter().any(|h| h.name.eq_ignore_ascii_case("origin")) {
        return error(403, "browser origins are not allowed");
    }
    if request.body.len() > MAX_BODY {
        return error(413, "body too large");
    }
    let body: Value = if request.body.is_empty() {
        json!({})
    } else {
        match serde_json::from_slice(&request.body) {
            Ok(value) => value,
            Err(_) => return error(400, "invalid json"),
        }
    };
    if request.method == "POST" && request.path == "/v1/pair" {
        return post_pair(&body);
    }
    if !authorized(&request) {
        return error(401, "unauthorized");
    }
    match (request.method.as_str(), request.path.as_str()) {
        ("GET", "/v1/status") => {
            refresh_devices().await;
            response(200, status_body())
        }
        ("POST", "/v1/select") => {
            let addr = body.get("addr").and_then(Value::as_str).unwrap_or("").to_string();
            refresh_devices().await;
            if !state().devices.iter().any(|(_, a)| a == &addr) {
                return error(404, "device not connected");
            }
            {
                let mut s = state();
                s.selected = addr;
                s.registered_for.clear();
            }
            refresh_devices().await;
            response(200, status_body())
        }
        ("POST", "/v1/ping") => {
            refresh_devices().await;
            let addr = match selected_online() {
                Ok(addr) => addr,
                Err(e) => return error(409, &e),
            };
            if body.get("launch").and_then(Value::as_bool).unwrap_or(false) {
                if let Err(e) = launch_app(&addr).await {
                    return error(502, &e);
                }
            }
            match send_frame(&addr, &json!({"v": 1, "type": "ping", "ts": now_ms()})).await {
                Ok(()) => response(200, json!({"ok": true})),
                Err(e) => error(502, &e),
            }
        }
        ("POST", "/v1/ask") => post_ask(&body).await,
        ("POST", "/v1/withdraw") => {
            let id = body.get("id").and_then(Value::as_str).unwrap_or("").to_string();
            state().outbox.retain(|q| id_of(q) != Some(id.as_str()));
            if let Ok(addr) = selected_online() {
                let _ = send_frame(&addr, &json!({"v": 1, "type": "withdraw", "id": id})).await;
            }
            response(200, json!({"ok": true}))
        }
        ("GET", "/v1/events") => {
            let events: Vec<Value> = state().inbox.iter().cloned().collect();
            response(200, json!({"ok": true, "events": events}))
        }
        ("POST", "/v1/events/ack") => {
            let upto = body.get("upto").and_then(Value::as_u64).unwrap_or(0);
            let mut s = state();
            s.inbox.retain(|e| e.get("seq").and_then(Value::as_u64).unwrap_or(0) > upto);
            response(200, json!({"ok": true, "remaining": s.inbox.len()}))
        }
        _ => error(404, "not found"),
    }
}

// ---------------------------------------------------------------- lifecycle

fn load_or_create_token() -> Result<String, String> {
    if let Ok(saved) = std::fs::read_to_string(TOKEN_FILE) {
        let saved = saved.trim().to_string();
        if saved.len() == 64 && saved.chars().all(|c| c.is_ascii_hexdigit()) {
            return Ok(saved);
        }
    }
    let token = random_token()?;
    write_atomic(TOKEN_FILE, &token)?;
    Ok(token)
}

async fn start() -> Result<(), String> {
    let token = load_or_create_token()?;
    let info = match http_server::start(http_server::ServerOptions { port: PORT, bind_all_interfaces: false }).await {
        Ok(info) => info,
        Err(fixed) => http_server::start(http_server::ServerOptions { port: 0, bind_all_interfaces: false })
            .await
            .map_err(|e| format!("http server: {fixed}; fallback: {e}"))?,
    };
    {
        let mut s = state();
        s.token = token.clone();
        s.url = info.url.clone();
        s.pair_code = new_pair_code();
    }
    let record = json!({"url": info.url, "token": token, "package": PACKAGE, "version": VERSION, "startedAt": now_ms()});
    write_atomic(BRIDGE_FILE, &record.to_string())?;
    timer::set_interval(REFRESH_MS, "refresh");
    Ok(())
}

fn render() {
    let (id, lines) = {
        let s = state();
        let device = s
            .devices
            .iter()
            .find(|(_, addr)| addr == &s.selected)
            .map(|(name, _)| name.clone())
            .unwrap_or_else(|| "未选择（只连接一个手环时自动选择）".into());
        let lines = vec![
            format!("GTD Band Bridge {VERSION}"),
            format!("配对码：{}", if s.pair_code.is_empty() { "不可用" } else { &s.pair_code }),
            format!("本机接口：{}", if s.url.is_empty() { "未启动" } else { &s.url }),
            format!("手环：{device}"),
            format!("消息接收：{}", if s.registered_for == s.selected && !s.selected.is_empty() { "已注册" } else { "未注册" }),
            format!("待回答：{}，未取走事件：{}", s.outbox.len(), s.inbox.len()),
            format!("最近错误：{}", if s.last_error.is_empty() { "无" } else { &s.last_error }),
        ];
        (s.ui_id.clone(), lines)
    };
    if id.is_empty() {
        return;
    }
    let mut root = ui::Element::new(ui::ElementType::Div, None)
        .flex()
        .flex_direction(ui::FlexDirection::Column)
        .gap(6)
        .padding(12);
    for line in lines {
        root = root.child(ui::Element::new(ui::ElementType::P, Some(&line)).margin(0));
    }
    ui::render(&id, root);
}

struct Plugin;

impl lifecycle::Guest for Plugin {
    async fn on_load() {
        if let Err(e) = start().await {
            set_error(e);
        }
        refresh_devices().await;
    }
}

impl http::Guest for Plugin {
    async fn handle(_server_id: u32, request: http_server::Request) -> http_server::Response {
        let reply = route(request).await;
        render();
        reply
    }
}

impl event::Guest for Plugin {
    async fn on_event(kind: event::EventType, payload: String) -> String {
        match kind {
            event::EventType::Timer | event::EventType::DeviceAction => refresh_devices().await,
            event::EventType::InterconnectMessage => {
                let selected = state().selected.clone();
                match decode_band_frame(&payload, &selected) {
                    Ok(frame) => handle_band_frame(frame).await,
                    Err(e) => set_error(format!("band frame: {e}")),
                }
            }
            _ => {}
        }
        render();
        String::new()
    }

    async fn on_ui_event(_id: String, _event: ui::Event, _payload: String) -> String {
        String::new()
    }

    async fn on_ui_render(id: String) {
        state().ui_id = id;
        refresh_devices().await;
        render();
    }

    async fn on_card_render(id: String) {
        ui::render_to_text_card(&id, "GTD Band Bridge：打开插件查看配对码和连接状态");
    }
}

export!(Plugin);
