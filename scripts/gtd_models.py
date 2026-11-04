"""GTD-specific model preferences, independent of the user's chat defaults."""
import json
import os
from pathlib import Path
import tomllib

DEFAULT_MODEL = "gpt-6.1-terra"
DEFAULT_EFFORT = "high"
EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


def codex_configuration(home=None):
    directory = Path(home) if home is not None else Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    path = directory / "config.toml"
    return directory, tomllib.loads(path.read_text()) if path.exists() else {}


def selection(state):
    config = state.config()
    return {"model": config.get("extractor_model") or DEFAULT_MODEL,
            "reasoning_effort": config.get("extractor_reasoning_effort") or DEFAULT_EFFORT}


def command_args(state):
    current = selection(state)
    return ["--model", current["model"], "-c", "model_reasoning_effort=" + json.dumps(current["reasoning_effort"])]


def catalog(state, home=None):
    directory, config = codex_configuration(home)
    configured = config.get("model_catalog_json")
    path = Path(configured).expanduser() if configured else directory / "models_cache.json"
    models = []
    if path.is_file():
        try:
            value = json.loads(path.read_text())
            entries = value.get("models", []) if isinstance(value, dict) else value
            for entry in entries:
                slug = entry.get("slug")
                if not slug or "image" in slug.lower() or entry.get("visibility") == "hide":
                    continue
                levels = [level["effort"] for level in entry.get("supported_reasoning_levels", [])]
                models.append({"id": slug, "name": entry.get("display_name") or slug,
                               "efforts": levels or EFFORTS})
        except (ValueError, TypeError, KeyError):
            models = []
    current = selection(state)
    for slug in (DEFAULT_MODEL, current["model"]):
        if not any(model["id"] == slug for model in models):
            models.insert(0, {"id": slug, "name": slug, "efforts": EFFORTS})
    return {**current, "provider": config.get("model_provider") or "openai", "models": models}


def save(state, model, effort, write_json):
    if not isinstance(model, str) or not model.strip() or any(char.isspace() for char in model.strip()):
        raise ValueError("请填写有效的模型 ID")
    if effort not in EFFORTS:
        raise ValueError("请选择有效的推理强度")
    with state.lock("configuration.lock", blocking=True):
        config = state.config()
        config.update(extractor_model=model.strip(), extractor_reasoning_effort=effort)
        write_json(state.base / "config.json", config)
    return {"ok": True, **selection(state)}
