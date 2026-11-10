"""Model provider profiles: switch GTD between Codex and OpenAI-compatible endpoints.

A profile is a provider file `<state>/secrets/<id>.json` (mode 0600) in the format
`gtd_provider.load` reads, plus an optional display `name`. Existing hand-written
provider files in that folder appear as profiles without migration. API keys are
written but never returned by any function here.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import gtd
import gtd_models
import gtd_provider

PROFILE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
NEW_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
# OpenAI-compatible endpoints of common domestic providers, plus a local server; the user still picks the model.
PRESETS = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1"},
    {"id": "qwen", "name": "通义千问（阿里云百炼）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"id": "glm", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4"},
    {"id": "kimi", "name": "Kimi（月之暗面）", "base_url": "https://api.moonshot.cn/v1"},
    {"id": "local", "name": "本地 / SSH 隧道服务", "base_url": "http://127.0.0.1:8080/v1"},
]
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
NO_AUTH_KEY = "no-auth"
TEST_PROMPT = '只输出这个 JSON 对象：{"ok": true}'


def folder(state: gtd.State) -> Path:
    """Directory holding provider profiles (created private on demand)."""
    return state.base / "secrets"


def profile_path(state: gtd.State, profile_id: Any) -> Path:
    if not isinstance(profile_id, str) or not PROFILE_ID.match(profile_id):
        raise ValueError("供应商 ID 只能包含字母、数字、- 和 _，最多 40 个字符")
    return folder(state) / f"{profile_id}.json"


def read_profile(path: Path) -> Optional[Dict[str, Any]]:
    """Parse a provider file; None if it is not a usable provider profile."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k]
                                              for k in ("base_url", "api_key", "model")):
        return None
    return value


def public(profile_id: str, value: Dict[str, Any]) -> Dict[str, Any]:
    """Profile fields that are safe to show in the workbench (no API key)."""
    extra = value.get("extra_body") if isinstance(value.get("extra_body"), dict) else {}
    thinking = (extra.get("chat_template_kwargs") or {}).get("enable_thinking")
    return {"id": profile_id, "name": value.get("name") or profile_id, "base_url": value["base_url"],
            "model": value["model"], "has_key": value["api_key"] != NO_AUTH_KEY,
            "disable_thinking": thinking is False, "max_tokens": extra.get("max_tokens")}


def active_path(state: gtd.State) -> Optional[Path]:
    path = gtd.provider_file(state)
    return path.resolve() if path else None


def status_path(state: gtd.State) -> Path:
    return state.base / "provider-status.json"


def statuses(state: gtd.State) -> Dict[str, Any]:
    try:
        value = json.loads(status_path(state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def listing(state: gtd.State) -> Dict[str, Any]:
    """Backend in use, all profiles with their last connection check, and presets."""
    active = active_path(state)
    checked = statuses(state)
    profiles, active_id = [], None
    directory = folder(state)
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        value = read_profile(path)
        if value and PROFILE_ID.match(path.stem):
            profiles.append({**public(path.stem, value), "status": checked.get(path.stem)})
            if active and path.resolve() == active:
                active_id = path.stem
    external = None
    if active and active_id is None:
        value = read_profile(active)
        external = {"path": str(active), "model": value["model"] if value else None}
    return {"backend": "provider" if active else "codex", "active": active_id,
            "external": external, "profiles": profiles, "presets": PRESETS}


def validate_url(url: Any) -> str:
    """Require https, or plain http only to this machine (an SSH tunnel or local server)."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("请填写 Base URL，例如 https://api.deepseek.com/v1")
    parts = urlsplit(url.strip())
    if parts.scheme == "https" and parts.hostname:
        return url.strip().rstrip("/")
    if parts.scheme == "http" and parts.hostname in LOOPBACK:
        return url.strip().rstrip("/")
    raise ValueError("Base URL 需使用 https；http 只允许本机地址（127.0.0.1 / localhost）")


def save(state: gtd.State, data: Dict[str, Any]) -> Dict[str, Any]:
    """Create or update a profile. A blank API key keeps the stored one (or `no-auth` for new local servers)."""
    path = profile_path(state, data.get("id"))
    if not path.exists() and not NEW_PROFILE_ID.match(path.stem):
        raise ValueError("新供应商 ID 需以小写字母开头，只含小写字母、数字、- 和 _")
    model = data.get("model")
    if not isinstance(model, str) or not model.strip() or any(c.isspace() for c in model.strip()):
        raise ValueError("请填写有效的模型 ID")
    max_tokens = data.get("max_tokens")
    if max_tokens in ("", None):
        max_tokens = None
    elif not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 256 <= max_tokens <= 200000:
        raise ValueError("最大输出 token 需为 256–200000 的整数，或留空")
    base_url = validate_url(data.get("base_url"))
    with state.lock("configuration.lock", blocking=True):
        old = read_profile(path) or {}
        stored = old.get("api_key") if old.get("api_key") != NO_AUTH_KEY else None
        key = data.get("api_key") or stored
        if not key:
            # Only a server on this machine (e.g. an SSH tunnel) may run without a key.
            if urlsplit(base_url).hostname not in LOOPBACK:
                raise ValueError("请填写 API 密钥；只有本机地址（127.0.0.1 / localhost）可以不填")
            key = NO_AUTH_KEY
        if not isinstance(key, str) or any(c.isspace() for c in key.strip()):
            raise ValueError("API Key 格式无效")
        extra = dict(old.get("extra_body") or {})
        kwargs = dict(extra.get("chat_template_kwargs") or {})
        if data.get("disable_thinking"):
            kwargs["enable_thinking"] = False
        else:
            kwargs.pop("enable_thinking", None)
        if kwargs:
            extra["chat_template_kwargs"] = kwargs
        else:
            extra.pop("chat_template_kwargs", None)
        if max_tokens is None:
            extra.pop("max_tokens", None)
        else:
            extra["max_tokens"] = max_tokens
        name = data.get("name") if isinstance(data.get("name"), str) and data["name"].strip() else path.stem
        value = {**old, "name": name.strip(), "base_url": base_url,
                 "api_key": key.strip(), "model": model.strip()}
        if extra:
            value["extra_body"] = extra
        else:
            value.pop("extra_body", None)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        gtd.atomic_json(path, value)
        os.chmod(path, 0o600)
    return {"ok": True, **listing(state)}


def use(state: gtd.State, data: Dict[str, Any]) -> Dict[str, Any]:
    """Switch extraction, clarification and quick answers to Codex or to one profile."""
    with state.lock("configuration.lock", blocking=True):
        config = state.config()
        if data.get("backend") == "codex":
            config["extractor_backend"] = "codex"
        elif data.get("backend") == "provider":
            path = profile_path(state, data.get("id"))
            if not read_profile(path):
                raise ValueError("找不到这个供应商配置")
            config.update(extractor_backend="provider", extractor_provider=str(path))
        else:
            raise ValueError("请选择 Codex 或一个供应商")
        gtd.atomic_json(state.base / "config.json", config)
    return {"ok": True, **listing(state)}


def delete(state: gtd.State, data: Dict[str, Any]) -> Dict[str, Any]:
    path = profile_path(state, data.get("id"))
    with state.lock("configuration.lock", blocking=True):
        if active_path(state) == path.resolve():
            raise ValueError("正在使用的供应商不能删除，请先切换到其他供应商或 Codex")
        if not read_profile(path):
            raise ValueError("找不到这个供应商配置")
        path.unlink()
        checked = statuses(state)
        if checked.pop(path.stem, None) is not None:
            gtd.atomic_json(status_path(state), checked)
    return {"ok": True, **listing(state)}


def models(state: gtd.State, data: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch the model catalog for a saved profile or for the unsaved form (address + key)."""
    stored = read_profile(profile_path(state, data["id"])) if data.get("id") else None
    stored = stored or {}
    base_url = validate_url(data.get("base_url") or stored.get("base_url"))
    key = data.get("api_key") or stored.get("api_key") or NO_AUTH_KEY
    try:
        return {"ok": True, "models": gtd_provider.list_models(base_url, key)}
    except gtd_provider.ProviderError as error:
        return {"ok": False, "models": [], "error": str(error)[:300]}


def check(state: gtd.State, data: Dict[str, Any]) -> Dict[str, Any]:
    """One tiny JSON request to the profile; reports latency, never the key."""
    path = profile_path(state, data.get("id"))
    try:
        provider = gtd_provider.load(path)
    except (OSError, ValueError) as error:
        raise ValueError(f"配置无法读取：{error}") from error
    started = time.time()
    try:
        value = gtd_provider.chat_json(provider, "只输出 JSON。", TEST_PROMPT, timeout=90, retries=0)
        result = {"ok": value.get("ok") is True, "seconds": round(time.time() - started, 1), "model": provider["model"],
                  "error": "" if value.get("ok") is True else "返回内容不是预期的 JSON"}
    except gtd_provider.ProviderError as error:
        result = {"ok": False, "seconds": round(time.time() - started, 1), "model": provider["model"],
                  "error": str(error)[:300]}
    with state.lock("configuration.lock", blocking=True):
        checked = statuses(state)
        checked[path.stem] = {**result, "at": gtd.now()}
        gtd.atomic_json(status_path(state), checked)
    return result


def label(state: gtd.State) -> Dict[str, str]:
    """Model and qualifier to show for the backend in use (workbench summary, answer jobs)."""
    path = active_path(state)
    if not path:
        return {**gtd_models.selection(state), "backend": "codex"}
    value = read_profile(path) or {}
    return {"model": value.get("model") or "未知模型", "reasoning_effort": value.get("name") or path.stem,
            "backend": "provider"}
