"""OpenAI-compatible chat-completions provider for GTD (custom model vendors).

Credentials live in a private JSON file outside any repository, e.g.
~/.codex/personal-gtd/secrets/<name>.json  (mode 0600):
    {"base_url": "https://host/v1", "api_key": "...", "model": "..."}
Optional "extra_body" is merged into every request, for server-specific settings such as
{"max_tokens": 8000, "chat_template_kwargs": {"enable_thinking": false}} on local reasoning models.
The key is never logged; errors include only status codes and a short body.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

USER_AGENT = "GettingThingsDone/0.5 (OpenAI-compatible client)"
FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


class ProviderError(RuntimeError):
    """The provider failed or returned something that is not a JSON object."""


def load(path: Path) -> Dict[str, str]:
    path = Path(path).expanduser()
    if path.stat().st_mode & 0o077:
        raise ProviderError(f"{path} must not be readable by other users (chmod 600)")
    value = json.loads(path.read_text(encoding="utf-8"))
    for key in ("base_url", "api_key", "model"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ProviderError(f"provider file lacks {key}")
    return value


def chat_json(provider: Dict[str, str], system: str, user: str, timeout: float = 600,
              retries: int = 2, temperature: float = 0.2) -> Dict[str, Any]:
    """One JSON-object completion; retries transient failures with backoff.

    Instructions lead the user message by default: some gateways (seen on pinaic.com,
    2026-11-12) silently drop the system role. Set ``"system_role": true`` in the provider
    file to send them as a separate system message instead.
    """
    if provider.get("system_role") is True:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    else:
        messages = [{"role": "user", "content": system + "\n\n---\n\n" + user}]
    body = {"model": provider["model"], "temperature": temperature,
            "response_format": {"type": "json_object"}, "messages": messages}
    extra = provider.get("extra_body") or {}
    if not isinstance(extra, dict) or {"model", "messages"} & set(extra):
        raise ProviderError("extra_body must be an object and cannot replace model or messages")
    body.update(extra)
    request = urllib.request.Request(provider["base_url"].rstrip("/") + "/chat/completions",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + provider["api_key"],
                                              "Content-Type": "application/json",
                                              # Some gateways' WAFs reject the default Python-urllib agent.
                                              "User-Agent": USER_AGENT})
    last = ""
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
            text = FENCE.sub("", payload["choices"][0]["message"]["content"] or "")
            value = json.loads(text)
            if not isinstance(value, dict):
                raise ProviderError("completion is not a JSON object")
            value["_usage"] = payload.get("usage") or {}
            return value
        except urllib.error.HTTPError as error:
            with error:
                last = f"HTTP {error.code}: {error.read()[:200]!r}"
            if error.code < 500 and error.code != 429:
                break
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError) as error:
            last = f"{type(error).__name__}: {str(error)[:200]}"
        if attempt < retries:
            time.sleep(5 * (attempt + 1))
    raise ProviderError(last or "provider failed")


def list_models(base_url: str, api_key: str, timeout: float = 15) -> List[str]:
    """Model IDs from the endpoint's GET /models (OpenAI format); errors carry no key."""
    request = urllib.request.Request(base_url.rstrip("/") + "/models", method="GET",
                                     headers={"Authorization": "Bearer " + api_key, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        with error:
            raise ProviderError(f"HTTP {error.code}: {error.read()[:200]!r}") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ProviderError(f"{type(error).__name__}: {str(error)[:200]}") from None
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ProviderError("该地址没有返回模型目录（GET /models）")
    return sorted({entry["id"] for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)})


def schema_instruction(schema: Dict[str, Any]) -> str:
    return "Return only one JSON object that conforms to this JSON Schema:\n" + json.dumps(schema, ensure_ascii=False)


def usage_total(results: List[Dict[str, Any]]) -> Dict[str, int]:
    total: Dict[str, int] = {}
    for result in results:
        for key, value in (result.get("_usage") or {}).items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
    return total
