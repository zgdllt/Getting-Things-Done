"""Local activity history: a GTD clue source that does not depend on Computer History.

Reads what is already on disk, with no new permissions:
  * Codex sessions   (~/.codex/sessions)     your messages + final answers, per project (cwd)
  * Claude sessions  (~/.claude/projects)    your messages + end-of-turn replies
  * git commits      (repos under configured roots)  "this was done" evidence

Every record uses the projected Computer History shape consumed by the GTD
extractor (event_key, timestamp, kind, source_type, app, window, text), so it
can run alone or side by side with Computer History for comparison.
Secrets are masked before anything leaves this module: session logs can
contain API keys and tokens that were pasted into a conversation.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import gtd_sources

MAX_TEXT = 1500
MAX_EVENTS = 4000
CODEX_ROOT = Path.home() / ".codex" / "sessions"
CLAUDE_ROOT = Path.home() / ".claude" / "projects"
DEFAULT_GIT_ROOTS = [Path.home() / "workspace"]

SECRET_PATTERNS = [
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),   # JWT
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"),                              # API keys
    re.compile(r"\b(?:ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}"),                  # GitHub tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                            # AWS key id
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{20,}"),                           # bearer tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]
INJECTED = re.compile(r"<(system-reminder|ide_selection|ide_opened_file|command-[a-z-]+|local-command-[a-z-]+)>"
                      r"[\s\S]*?</\1>")
CODEX_CONTEXT_PREFIXES = ("<environment_context>", "<user_instructions>", "# AGENTS.md", "<permissions",
                          "<turn_aborted>", "<user_shell_command>")


def redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[已隐去密钥]", text)
    return text


def clean(text: str) -> str:
    text = INJECTED.sub("", text).strip()
    text = redact(text)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"


def _ts(value: Any) -> Optional[dt.datetime]:
    return gtd_sources.parse_timestamp(value) if value else None


def _record(key: str, timestamp: str, kind: str, source: str, app: str, title: str, text: str) -> Dict[str, Any]:
    return {"event_key": key, "timestamp": timestamp, "kind": kind, "source_type": source,
            "app": {"name": app}, "window": {"title": title}, "text": text, "part": 1, "parts": 1}


def _files(root: Path, pattern: str, start: dt.datetime) -> List[Path]:
    """Session files that may contain events at or after `start` (by mtime)."""
    floor = start.timestamp()
    paths = [Path(p) for p in glob.glob(str(root / pattern), recursive=True)]
    return sorted(p for p in paths if p.is_file() and p.stat().st_mtime >= floor)


def _lines(path: Path, needles: Tuple[bytes, ...]) -> Iterator[Tuple[int, Dict[str, Any]]]:
    """Yield parsed JSON lines that contain one of `needles` (cheap pre-filter for large logs)."""
    with path.open("rb") as stream:
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line:
                return
            if not line.endswith(b"\n") or not any(n in line for n in needles):
                continue
            try:
                yield offset, json.loads(line)
            except ValueError:
                continue


def _texts(content: Any, kinds: Iterable[str]) -> str:
    if isinstance(content, str):
        return content
    wanted = set(kinds)
    return "\n".join(item.get("text", "") for item in content or []
                     if isinstance(item, dict) and item.get("type") in wanted)


def codex_events(start: dt.datetime, end: dt.datetime, root: Path = CODEX_ROOT) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for path in _files(root, "**/*.jsonl", start):
        project = ""
        for offset, row in _lines(path, (b'"session_meta"', b'"turn_context"', b'"role":"user"',
                                         b'"final_answer"')):
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if row.get("type") in ("session_meta", "turn_context") and payload.get("cwd"):
                project = Path(payload["cwd"]).name
                continue
            if row.get("type") != "response_item" or payload.get("type") != "message":
                continue
            stamp = _ts(row.get("timestamp"))
            if stamp is None or not start <= stamp < end:
                continue
            role, phase = payload.get("role"), payload.get("phase")
            if role == "user":
                text = _texts(payload.get("content"), ("input_text",)).strip()
                if not text or text.startswith(CODEX_CONTEXT_PREFIXES):
                    continue
                kind = "agent.user_message"
            elif role == "assistant" and phase == "final_answer":
                text, kind = _texts(payload.get("content"), ("output_text",)), "agent.final_answer"
            else:
                continue
            text = clean(text)
            if text:
                events.append(_record(f"{path}:{offset}", row["timestamp"], kind, "local_codex_session",
                                      "Codex", project, text))
    return events


def claude_events(start: dt.datetime, end: dt.datetime, root: Path = CLAUDE_ROOT) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for path in _files(root, "**/*.jsonl", start):
        for offset, row in _lines(path, (b'"type":"user"', b'"end_turn"')):
            stamp = _ts(row.get("timestamp"))
            if stamp is None or not start <= stamp < end or row.get("isSidechain"):
                continue
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            if row.get("type") == "user" and (row.get("origin") or {}).get("kind") == "human":
                text, kind = _texts(message.get("content"), ("text",)), "agent.user_message"
            elif row.get("type") == "assistant" and message.get("stop_reason") == "end_turn":
                text, kind = _texts(message.get("content"), ("text",)), "agent.final_answer"
            else:
                continue
            text = clean(text)
            if text:
                project = Path(row.get("cwd") or "").name
                events.append(_record(f"{path}:{offset}", row["timestamp"], kind, "local_claude_session",
                                      "Claude Code", project, text))
    return events


def git_repos(roots: Iterable[Path]) -> List[Path]:
    repos = set()
    for root in roots:
        for marker in list(Path(root).glob("*/.git")) + list(Path(root).glob("*/*/.git")):
            repos.add(marker.parent.resolve())
    return sorted(repos)


def git_events(start: dt.datetime, end: dt.datetime, roots: Iterable[Path] = DEFAULT_GIT_ROOTS) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for repo in git_repos(roots):
        try:
            out = subprocess.run(["git", "-C", str(repo), "log", "--all", "--no-merges",
                                  f"--since={start.isoformat()}", f"--until={end.isoformat()}",
                                  "--format=%H%x1f%aI%x1f%s"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in out.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                events.append(_record(f"git:{repo.name}:{parts[0][:12]}", parts[1], "git.commit",
                                      "local_git_commit", "git", repo.name, clean(parts[2])))
    return events


def window(start: dt.datetime, end: dt.datetime, git_roots: Optional[Iterable[Path]] = None,
           sources: Iterable[str] = ("codex", "claude", "git")) -> List[Dict[str, Any]]:
    """All local events in [start, end), oldest first, capped at MAX_EVENTS."""
    wanted = set(sources)
    events: List[Dict[str, Any]] = []
    if "codex" in wanted:
        events += codex_events(start, end)
    if "claude" in wanted:
        events += claude_events(start, end)
    if "git" in wanted:
        events += git_events(start, end, git_roots or DEFAULT_GIT_ROOTS)
    events.sort(key=lambda e: _ts(e["timestamp"]) or start)
    return events[:MAX_EVENTS]


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Count local activity events (no content printed)")
    parser.add_argument("--hours", type=float, default=24)
    args = parser.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    found = window(now - dt.timedelta(hours=args.hours), now)
    counts: Dict[str, int] = {}
    for event in found:
        counts[event["source_type"] + "/" + event["kind"]] = counts.get(event["source_type"] + "/" + event["kind"], 0) + 1
    print(json.dumps({"events": len(found), "chars": sum(len(e["text"]) for e in found), "by_kind": counts},
                     ensure_ascii=False, indent=2))
