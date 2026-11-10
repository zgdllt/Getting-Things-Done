#!/usr/bin/env python3
"""Pluggable local clue sources for the GTD evidence inbox.

The rest of the application consumes normalized source records.  Computer
History is only the first adapter; Markdown memories are a second, already
summarized source that can reduce the amount of raw activity sent to a model.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MEMORY_FILENAME = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-"
    r"(?P<id>[^-]+)-(?P<window>10min|6h)-"
)
_MEMORY_CACHE = {}


class SourceUnavailable(RuntimeError):
    """A configured source cannot currently be read."""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def parse_timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(dt.timezone.utc)


def local_timezone_name():
    """Return the system IANA timezone without depending on a fixed locale."""
    current = dt.datetime.now().astimezone().tzinfo
    key = getattr(current, "key", None)
    if key:
        return key
    localtime = Path("/etc/localtime")
    try:
        target = localtime.resolve()
        marker = "/zoneinfo/"
        value = str(target)
        if marker in value:
            return value.split(marker, 1)[1]
    except OSError:
        pass
    # A fixed-offset timezone has no reliable IANA name. UTC is explicit and
    # keeps date formatting deterministic until the user configures one.
    return "UTC"


def timezone_name(config):
    config = config or {}
    configured = config.get("timezone")
    if config.get("timezone_source") == "system":
        return local_timezone_name()
    # v0.3.0 wrote Asia/Shanghai as an implicit default. Treat that legacy
    # value as system-managed unless the user explicitly marked it otherwise.
    if configured == "Asia/Shanghai" and "timezone_source" not in config:
        return local_timezone_name()
    if configured:
        try:
            ZoneInfo(configured)
            return configured
        except ZoneInfoNotFoundError:
            pass
    return local_timezone_name()


def default_memory_root():
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    return codex_home / "memories" / "extensions" / "skysight"


def project_event(event, path, offset):
    """Normalize a Computer History event while retaining action evidence."""
    app, window = event.get("app", {}), event.get("window", {})
    portions = []
    for name in ("keyboard", "ax", "selection", "mouse"):
        part = event.get(name)
        if isinstance(part, dict):
            for field in ("text", "target"):
                value = part.get(field)
                if value:
                    portions.append(value if isinstance(value, str) else encode(value))
    text = "\n".join(dict.fromkeys(portions))
    if not text and not window:
        return []
    key = str(path) + ":" + str(event.get("id", offset))
    base = {"event_key": key, "timestamp": event.get("timestamp"), "kind": event.get("kind"),
            "source_type": "computer_history_event", "app": app, "window": window}
    pieces = [text[i:i + 12000] for i in range(0, len(text), 12000)] or [""]
    return [{**base, "text": part, "part": i + 1, "parts": len(pieces),
             "event_key": key + (f"#part{i+1}" if len(pieces) > 1 else "")}
            for i, part in enumerate(pieces)]


class HistoryAdapter:
    """Interface consumed by the durable batch and bounded-history layers."""

    name = "unknown"
    schema = "unknown"

    def collect(self, state, root, baseline=False):
        raise NotImplementedError

    def window(self, root, start, end):
        raise NotImplementedError

    def status(self, root):
        raise NotImplementedError


class ComputerHistoryAdapter(HistoryAdapter):
    name = "computer_history"
    schema = "segments/*/events.jsonl"

    def segment_paths(self, root):
        root = Path(root).expanduser().resolve()
        segments = root / "segments"
        if not segments.is_dir():
            raise SourceUnavailable("Computer History segments directory is unavailable")
        return sorted(segments.glob("*/events.jsonl"))

    @staticmethod
    def _signature(stream, position):
        stream.seek(0)
        head = stream.read(min(position, 256))
        stream.seek(max(0, position - 256))
        return hashlib.sha256(head + stream.read(min(position, 256))).hexdigest()

    def collect(self, state, root, baseline=False):
        records, cursors, malformed = [], [], 0
        source_times = []
        for path in self.segment_paths(root):
            try:
                stat = path.stat()
                saved = state.db.execute(
                    "SELECT inode,offset,signature FROM cursors WHERE path=?", (str(path),)
                ).fetchone()
                offset = saved["offset"] if saved and saved["inode"] == stat.st_ino \
                    and saved["offset"] <= stat.st_size else 0
                with path.open("rb") as stream:
                    if offset and saved["signature"] and self._signature(stream, offset) != saved["signature"]:
                        offset = 0
                    if baseline and saved is None:
                        data = stream.read()
                        offset = data.rfind(b"\n") + 1
                    stream.seek(offset)
                    while not baseline:
                        start = stream.tell()
                        line = stream.readline()
                        if not line or not line.endswith(b"\n"):
                            stream.seek(start)
                            break
                        offset = stream.tell()
                        try:
                            event = json.loads(line)
                            if not isinstance(event, dict):
                                raise ValueError("Not an object")
                        except (ValueError, UnicodeDecodeError):
                            malformed += 1
                            continue
                        if isinstance(event.get("timestamp"), str):
                            source_times.append(event["timestamp"])
                        records.extend(project_event(event, path, start))
                    checkpoint = self._signature(stream, offset)
                cursors.append((str(path), stat.st_ino, offset, checkpoint))
            except FileNotFoundError:
                # Rolling segments may disappear between directory listing and read.
                continue
        return {
            "records": records,
            "cursors": cursors,
            "malformed_lines": malformed,
            "first_event_at": min(source_times) if source_times else None,
            "latest_event_at": max(source_times) if source_times else None,
        }

    def window(self, root, start, end):
        events, available = [], []
        for path in self.segment_paths(root):
            try:
                with path.open("rb") as stream:
                    while True:
                        offset = stream.tell()
                        line = stream.readline()
                        if not line:
                            break
                        if not line.endswith(b"\n"):
                            continue
                        try:
                            event = json.loads(line)
                            timestamp = parse_timestamp(event["timestamp"])
                        except (ValueError, KeyError, TypeError):
                            continue
                        available.append(timestamp)
                        if start <= timestamp < end:
                            events += project_event(event, path, offset)
            except FileNotFoundError:
                continue
        return events, available

    def status(self, root):
        root_value = str(Path(root).expanduser().resolve()) if root else ""
        if not root_value:
            return {"state": "not_configured", "adapter": self.name, "root": None,
                    "schema": self.schema, "segments": 0}
        try:
            paths = self.segment_paths(root_value)
        except SourceUnavailable as error:
            return {"state": "unavailable", "adapter": self.name, "root": root_value,
                    "schema": self.schema, "segments": 0, "error": str(error)}
        latest = max((path.stat().st_mtime for path in paths if path.exists()), default=None)
        latest_at = dt.datetime.fromtimestamp(latest, dt.timezone.utc).isoformat() if latest else None
        age_hours = (dt.datetime.now(dt.timezone.utc).timestamp() - latest) / 3600 if latest else None
        state = "stale" if age_hours is not None and age_hours > 36 else "ready"
        return {"state": state, "adapter": self.name, "root": root_value,
                "schema": self.schema, "segments": len(paths), "latest_at": latest_at,
                "age_hours": round(age_hours, 1) if age_hours is not None else None,
                "warning_after_hours": 36}


ADAPTERS = {ComputerHistoryAdapter.name: ComputerHistoryAdapter}


def history_adapter(config):
    name = (config or {}).get("history_source", ComputerHistoryAdapter.name)
    adapter = ADAPTERS.get(name)
    if not adapter:
        raise SourceUnavailable("Unknown history source adapter: " + str(name))
    return adapter()


def _memory_metadata(path):
    match = MEMORY_FILENAME.match(path.name)
    if not match:
        return None
    try:
        end = dt.datetime.strptime(match["stamp"], "%Y-%m-%dT%H-%M-%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    duration = dt.timedelta(minutes=10 if match["window"] == "10min" else 6 * 60)
    return {"path": path, "window": match["window"], "start": end - duration, "end": end,
            "id": match["id"]}


def _memory_body(path):
    raw = path.read_text(encoding="utf-8", errors="replace")
    title = ""
    if raw.startswith("---"):
        pieces = raw.split("---", 2)
        frontmatter = pieces[1] if len(pieces) == 3 else ""
        body = pieces[2] if len(pieces) == 3 else raw
        for line in frontmatter.splitlines():
            if line.startswith("title:"):
                title = line.split(":", 1)[1].strip().strip('"\'')
                break
    else:
        body = raw
    return title, body.strip()


def _memory_catalog(root):
    """Cache parsed memory bodies while the long-running subscriber is alive."""
    root = Path(root).expanduser()
    resources = root / "resources"
    if not resources.is_dir():
        return []
    cache_key = str(root.resolve())
    previous = _MEMORY_CACHE.get(cache_key, {})
    current = {}
    for path in sorted(resources.glob("*.md")):
        metadata = _memory_metadata(path)
        if not metadata:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        token = (stat.st_mtime_ns, stat.st_size)
        old = previous.get(str(path))
        if old and old["token"] == token:
            item = old["item"]
        else:
            try:
                title, body = _memory_body(path)
            except OSError:
                continue
            if not body:
                continue
            item = {"path": path, "metadata": metadata, "title": title, "body": body}
        current[str(path)] = {"token": token, "item": item}
    _MEMORY_CACHE[cache_key] = current
    return [entry["item"] for entry in current.values()]


def _memory_record(path, metadata, body, title, source_type="computer_history_memory"):
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:20]
    return {
        "event_key": "memory:" + digest + (":retro" if source_type.endswith("retro") else ""),
        "timestamp": metadata["end"].isoformat(),
        "kind": "computer_history.memory_summary",
        "source_type": source_type,
        "source_path": str(path),
        "memory_window": metadata["window"],
        "memory_start": metadata["start"].isoformat(),
        "memory_end": metadata["end"].isoformat(),
        "app": {"name": "Computer History memory"},
        "window": {"title": title or path.stem},
        "text": body[:12000],
        "part": 1,
        "parts": 1,
    }


def memory_summaries(config, event_times=()):
    config = config or {}
    if config.get("memory_source_enabled", True) is False:
        return []
    root = Path(config.get("memory_root") or default_memory_root()).expanduser()
    times = [parse_timestamp(value) for value in event_times]
    times = [value for value in times if value]
    summaries = []
    for item in _memory_catalog(root):
        path, metadata, title, body = item["path"], item["metadata"], item["title"], item["body"]
        if times and not any(metadata["start"] <= value <= metadata["end"] for value in times):
            continue
        summaries.append(_memory_record(path, metadata, body, title))
    # Prefer detailed summaries for model input. If none exist, a recent
    # six-hour summary still gives useful continuity without raw event text.
    detailed = [item for item in summaries if item["memory_window"] == "10min"]
    chosen = detailed[-12:] if detailed else [item for item in summaries if item["memory_window"] == "6h"][-2:]
    return chosen


_RECALL_STOPWORDS = {
    "用户", "继续", "查看", "打开", "进行", "工作", "页面", "项目", "当前", "完成",
    "一个", "这个", "相关", "没有", "可以", "需要", "记录", "发现", "以及", "同时",
    "codex", "computer", "history", "memory", "summary", "getting", "things", "done",
}


def _recall_terms(value):
    value = str(value or "").lower()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_/-]{1,}", value))
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", value):
        if sequence not in _RECALL_STOPWORDS:
            terms.add(sequence)
        terms.update(sequence[index:index + 2] for index in range(len(sequence) - 1))
    return {term for term in terms if term not in _RECALL_STOPWORDS and len(term) >= 2}


def retrospective_summaries(config, raw_events, current_summaries=()):
    """Retrieve older local summaries that share concrete terms with current work."""
    config = config or {}
    if config.get("memory_source_enabled", True) is False:
        return []
    root = Path(config.get("memory_root") or default_memory_root()).expanduser()
    query_parts = []
    for event in raw_events:
        query_parts.extend([
            event.get("text", ""),
            event.get("window", {}).get("title", "") if isinstance(event.get("window"), dict) else "",
            event.get("app", {}).get("name", "") if isinstance(event.get("app"), dict) else "",
        ])
    query_terms = _recall_terms(" ".join(query_parts))
    if not query_terms:
        return []
    current_paths = {item.get("source_path") for item in current_summaries}
    current_times = [parse_timestamp(event.get("timestamp")) for event in raw_events]
    current_times = [value for value in current_times if value]
    scored = []
    for item in _memory_catalog(root):
        path, metadata, title, body = item["path"], item["metadata"], item["title"], item["body"]
        if str(path) in current_paths:
            continue
        if current_times and any(metadata["start"] <= value <= metadata["end"] for value in current_times):
            continue
        terms = _recall_terms(title + "\n" + body)
        overlap = query_terms & terms
        if not overlap:
            continue
        # Short summaries are more precise; recency breaks otherwise equal ties.
        precision = 2 if metadata["window"] == "10min" else 1
        score = len(overlap) * precision
        scored.append((score, metadata["end"], path, metadata, title, body, overlap))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    records = []
    for score, _end, path, metadata, title, body, overlap in scored[:4]:
        record = _memory_record(path, metadata, body, title, "computer_history_memory_retro")
        record["retrieval_score"] = score
        record["retrieval_terms"] = sorted(overlap)[:12]
        records.append(record)
    return records


# Never sent to any model, whatever the source. Users extend this with `excluded_apps` in config.json.
DEFAULT_EXCLUDED_APPS = ("com.1password.1password", "com.agilebits.onepassword7", "com.bitwarden.desktop",
                         "com.apple.keychainaccess", "com.apple.Passwords")


def excluded_apps(config):
    return {value.lower() for value in (*DEFAULT_EXCLUDED_APPS, *((config or {}).get("excluded_apps") or []))}


def is_excluded(event, excluded):
    app = event.get("app") if isinstance(event.get("app"), dict) else {}
    names = (app.get("bundleIdentifier"), app.get("name"))
    return any(isinstance(name, str) and name.lower() in excluded for name in names)


def analysis_records(raw_events, config):
    """Use summaries for covered intervals and retain raw events only for gaps.

    Events from excluded (sensitive) apps are dropped here, before any model call."""
    excluded = excluded_apps(config)
    raw_events = [event for event in raw_events if not is_excluded(event, excluded)]
    if not raw_events:
        return []
    summaries = memory_summaries(config, [item.get("timestamp") for item in raw_events])
    spans = [(parse_timestamp(item["memory_start"]), parse_timestamp(item["memory_end"]))
             for item in summaries]
    result = list(summaries)
    for event in raw_events:
        timestamp = parse_timestamp(event.get("timestamp"))
        if not summaries or timestamp is None or not any(start <= timestamp <= end for start, end in spans):
            result.append(event)
    return result + retrospective_summaries(config, raw_events, summaries)


def memory_status(config):
    config = config or {}
    root = Path(config.get("memory_root") or default_memory_root()).expanduser()
    enabled = config.get("memory_source_enabled", True) is not False
    resources = root / "resources"
    files = sorted(resources.glob("*.md")) if resources.is_dir() else []
    metadata = [_memory_metadata(path) for path in files]
    metadata = [item for item in metadata if item]
    latest = max((item["end"] for item in metadata), default=None)
    state = "disabled" if not enabled else "ready" if resources.is_dir() else "unavailable"
    return {"state": state, "enabled": enabled, "root": str(root),
            "summaries": len(metadata), "latest_at": latest.isoformat() if latest else None}


def status(config):
    config = config or {}
    adapter = history_adapter(config)
    return {"history": adapter.status(config.get("history_root", "")),
            "memories": memory_status(config)}
