#!/usr/bin/env python3
"""Bridge GTD clarification questions to a Xiaomi band through AstroBox.

The AstroBox plugin "GTD Band Bridge" listens on 127.0.0.1:47913 of the host
running AstroBox. Two ways to reach it:

  phone host:   python3 gtd_band.py pair --adb <ip:port> --code <6 digits>
                (adb forward; the code is shown on the plugin page)
  desktop host: the plugin writes bridge.json in its directory; found automatically

One open GTD question is on the band at a time. Answers are applied as:

  dismiss -> candidate dismissed, its open questions closed (same rule as the workbench)
  later   -> not pushed again for LATER_HOURS
  mac     -> never pushed to the band again; answer it in the workbench

Every step is journaled in the `band_events` table of the GTD inbox database,
which `report` turns into answer rate, time-to-answer and choice counts.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import gtd  # noqa: E402  (GTD core, standard library only)
import gtd_clarify  # noqa: E402  (same answer-processing job as the workbench)
import gtd_options as band_options  # noqa: E402  (shared with the workbench)

logger = logging.getLogger("gtd-band")

PACKAGE = "com.zgdllt.gtdband"
PORT = 47913
ASTROBOX_DIR = Path.home() / "Library" / "Application Support" / "moe.astralsight.astrobox"
PAIRING_FILE = "band.json"
LATER_HOURS = 2.0
OUTSTANDING_HOURS = 2.0
UNDELIVERED_MINUTES = 10.0
# "option" = the user tapped one of the model-generated answers.
CHOICES = ("dismiss", "later", "mac", "option")
MAX_ANSWER_CHARS = 40
LOOPBACK = {"127.0.0.1", "localhost", "::1"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS band_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  question_key TEXT,
  kind TEXT NOT NULL,
  choice TEXT,
  band_ms INTEGER,
  detail TEXT,
  applied INTEGER NOT NULL DEFAULT 0,
  src TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS band_events_question ON band_events(question_key, kind);
"""


class BridgeError(RuntimeError):
    """The AstroBox plugin is unreachable, misconfigured or rejected the request."""


# ---------------------------------------------------------------- bridge

def check_loopback(url: str) -> str:
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in LOOPBACK:
        raise BridgeError(f"refusing non-loopback bridge url {url!r}")
    return url.rstrip("/")


def find_bridge(root: Path = ASTROBOX_DIR) -> Dict[str, Any]:
    """Return the newest bridge.json written by this plugin under a desktop AstroBox."""
    best: Optional[Dict[str, Any]] = None
    for path in root.rglob("bridge.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("package") != PACKAGE:
            continue
        if not record.get("url") or not record.get("token"):
            continue
        if best is None or record.get("startedAt", 0) > best.get("startedAt", 0):
            best = record
    if best is None:
        raise BridgeError(f"no paired phone and no desktop bridge.json under {root}")
    return best


def adb(serial: str, *args: str, timeout: float = 20.0) -> str:
    try:
        result = subprocess.run(["adb", "-s", serial, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise BridgeError(f"adb {' '.join(args)}: {e}") from e
    if result.returncode != 0:
        raise BridgeError(f"adb {' '.join(args)}: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def ensure_forward(serial: str, port: int = PORT) -> None:
    """Keep tcp:<port> on this Mac forwarded to the phone's loopback port.

    `adb forward --list` succeeds even when the phone is gone, so the forward
    itself is the real check; on failure reconnect wireless adb once and retry.
    """
    if f"{serial} tcp:{port} tcp:{port}" in adb(serial, "forward", "--list"):
        return
    try:
        adb(serial, "forward", f"tcp:{port}", f"tcp:{port}")
    except BridgeError:
        subprocess.run(["adb", "connect", serial], capture_output=True, timeout=20)
        adb(serial, "forward", f"tcp:{port}", f"tcp:{port}")


class BridgeClient:
    def __init__(self, url: str, token: str, timeout: float = 10.0) -> None:
        self.url = check_loopback(url)
        self.token = token
        self.timeout = timeout

    @classmethod
    def discover(cls, state_dir: Path, bridge_file: Optional[Path] = None) -> "BridgeClient":
        try:
            if bridge_file:
                record = json.loads(bridge_file.read_text(encoding="utf-8"))
            elif (state_dir / PAIRING_FILE).exists():
                record = json.loads((state_dir / PAIRING_FILE).read_text(encoding="utf-8"))
                if record.get("adb"):
                    ensure_forward(record["adb"], record.get("port", PORT))
            else:
                record = find_bridge()
            return cls(record["url"], record["token"])
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise BridgeError(f"bridge record unusable: {e}") from e

    def call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
             auth: bool = True) -> Dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method)
        if auth:
            request.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                detail = e.read()[:200]
            raise BridgeError(f"{method} {path}: HTTP {e.code} {detail!r}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise BridgeError(f"{method} {path}: {e}") from e


def pair(state_dir: Path, code: str, serial: Optional[str], port: int = PORT) -> Dict[str, Any]:
    """Exchange the plugin's pairing code for its token and remember it locally (0600)."""
    if serial:
        ensure_forward(serial, port)
    url = f"http://127.0.0.1:{port}"
    reply = BridgeClient(url, "").call("POST", "/v1/pair", {"code": code}, auth=False)
    if reply.get("package") != PACKAGE or not reply.get("token"):
        raise BridgeError("pairing reply is not from GTD Band Bridge")
    record = {"url": url, "token": reply["token"], "adb": serial, "port": port, "pairedAt": gtd.now()}
    gtd.atomic_json(state_dir / PAIRING_FILE, record)
    return {"paired": True, "url": url, "adb": serial, "pluginVersion": reply.get("version")}


# ---------------------------------------------------------------- journal

def open_state(base: Path) -> gtd.State:
    state = gtd.State(base)
    state.db.executescript(SCHEMA)
    state.db.commit()
    band_options.ensure_schema(state)
    return state


def journal(state: gtd.State, kind: str, question_key: Optional[str] = None, choice: Optional[str] = None,
            band_ms: Optional[int] = None, detail: Optional[Dict[str, Any]] = None, applied: int = 0,
            src: Optional[str] = None) -> None:
    state.db.execute(
        "INSERT OR IGNORE INTO band_events(at,question_key,kind,choice,band_ms,detail,applied,src) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (gtd.now(), question_key, kind, choice, band_ms, json.dumps(detail or {}, ensure_ascii=False),
         applied, src))


def ago(hours: float = 0.0, minutes: float = 0.0) -> str:
    delta = gtd.dt.timedelta(hours=hours, minutes=minutes)
    return (gtd.dt.datetime.now(gtd.dt.timezone.utc) - delta).isoformat()


def awaiting(state: gtd.State) -> List[str]:
    """Questions pushed recently with no band answer and no withdrawal since."""
    rows = state.rows(
        """SELECT DISTINCT p.question_key FROM band_events p
           WHERE p.kind='pushed'
             AND p.at >= CASE WHEN json_extract(p.detail,'$.delivered') THEN ? ELSE ? END
             AND NOT EXISTS (SELECT 1 FROM band_events a WHERE a.question_key=p.question_key
                             AND a.kind IN ('answer','withdrawn') AND a.at>=p.at)""",
        (ago(hours=OUTSTANDING_HOURS), ago(minutes=UNDELIVERED_MINUTES)))
    return [row["question_key"] for row in rows]


def still_askable(state: gtd.State, question_key: str) -> bool:
    row = state.db.execute(
        "SELECT 1 FROM questions q JOIN candidates c ON c.key=q.candidate_key "
        "WHERE q.key=? AND q.state='open' AND c.state IN ('inbox','resolved')", (question_key,)).fetchone()
    return row is not None


def next_question(state: gtd.State, exclude: Sequence[str] = ()) -> Optional[Dict[str, Any]]:
    """Oldest open question, preferring ones the band has been asked fewer times."""
    skip = ",".join("?" * len(exclude))
    row = state.db.execute(
        """SELECT q.key, q.question, c.title,
                  (SELECT COUNT(*) FROM band_events e WHERE e.question_key=q.key AND e.kind='pushed') AS pushes
           FROM questions q JOIN candidates c ON c.key=q.candidate_key
           WHERE q.state='open' AND c.state IN ('inbox','resolved')
             AND NOT EXISTS (SELECT 1 FROM band_events e WHERE e.question_key=q.key AND e.kind='answer'
                             AND (e.choice IN ('mac','dismiss') OR (e.choice='later' AND e.at>=?)))
             AND q.key NOT IN (""" + skip + """)
           ORDER BY pushes, q.created_at, q.key LIMIT 1""", (ago(hours=LATER_HOURS), *exclude)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- actions

def withdraw_stale(state: gtd.State, client: BridgeClient) -> List[str]:
    """Take questions off the band once they were answered or dismissed elsewhere."""
    stale = [key for key in awaiting(state) if not still_askable(state, key)]
    for key in stale:
        client.call("POST", "/v1/withdraw", {"id": key})
        with state.db:
            journal(state, "withdrawn", key, applied=1)
    return stale


def push(state: gtd.State, client: BridgeClient) -> Optional[str]:
    """Send the next open question unless one is still waiting on the band."""
    withdraw_stale(state, client)
    if awaiting(state):
        return None
    item = next_question(state)
    if item is None:
        return None
    options = band_options.get_or_generate(state, item["key"])
    result = client.call("POST", "/v1/ask", {"id": item["key"], "title": item["title"] or "",
                                             "question": item["question"], "options": options,
                                             "launch": True, "replace": True})
    with state.db:
        journal(state, "pushed", item["key"], applied=1,
                detail={"delivered": bool(result.get("delivered")), "launchError": result.get("launchError"),
                        "sendError": result.get("sendError") or result.get("error"), "options": options})
    return item["key"]


def prefetch(state: gtd.State) -> Optional[str]:
    """Generate options for the question after the one on the band, while the user answers."""
    item = next_question(state, exclude=awaiting(state))
    if item is None or band_options.cached(state, item["key"]) is not None:
        return None
    band_options.generate(state, item["key"])
    return item["key"]


def apply_dismiss(state: gtd.State, question_key: str) -> str:
    """Dismiss the candidate behind a still-open question, mirroring the workbench rule."""
    with state.lock("operations.lock"), state.db:
        row = state.db.execute("SELECT candidate_key FROM questions WHERE key=? AND state='open'",
                               (question_key,)).fetchone()
        if row is None:
            return "stale"
        candidate = row["candidate_key"]
        current = state.db.execute("SELECT state FROM candidates WHERE key=?", (candidate,)).fetchone()
        if not current or current["state"] not in ("inbox", "resolved", "dismissed"):
            return "candidate_not_dismissible"
        state.db.execute("UPDATE candidates SET state='dismissed',updated_at=? WHERE key=?", (gtd.now(), candidate))
        state.db.execute("UPDATE questions SET state='answered',answer='用户在手环上否掉了此候选事项' "
                         "WHERE candidate_key=? AND state='open'", (candidate,))
    return "dismissed"


def apply_option(state: gtd.State, question_key: str, text: str) -> str:
    """Save a tapped option as the answer and start GTD's answer job, like the workbench."""
    with state.lock("operations.lock"), state.db:
        row = state.db.execute("SELECT 1 FROM questions WHERE key=? AND state='open'", (question_key,)).fetchone()
        if row is None:
            return "stale"
        state.db.execute("UPDATE questions SET state='answered',answer=? WHERE key=?", (text, question_key))
        job = gtd_clarify.enqueue(state, [question_key])
    gtd_clarify.launch(state, {"ok": True, "job_id": job})
    return "answered"


def apply_pending(state: gtd.State) -> int:
    applied = 0
    for row in state.rows("SELECT id,question_key,choice,detail FROM band_events "
                          "WHERE kind='answer' AND applied=0 ORDER BY id"):
        outcome = "recorded"
        try:
            if row["choice"] == "dismiss":
                outcome = apply_dismiss(state, row["question_key"])
            elif row["choice"] == "option":
                text = json.loads(row["detail"] or "{}").get("optionText") or ""
                outcome = apply_option(state, row["question_key"], text) if text else "invalid_option"
        except gtd.ProcessorBusy:
            logger.info("GTD is busy; %s of %s retried next cycle", row["choice"], row["question_key"])
            continue
        with state.db:
            state.db.execute("UPDATE band_events SET applied=1, detail=json_set(COALESCE(detail,'{}'),'$.outcome',?) "
                             "WHERE id=?", (outcome, row["id"]))
        applied += 1
    return applied


def sync(state: gtd.State, client: BridgeClient) -> int:
    """Journal plugin events, then ack them; nothing is deleted on the plugin before commit."""
    events: List[Dict[str, Any]] = client.call("GET", "/v1/events").get("events", [])
    upto = 0
    with state.db:
        for event in events:
            seq = int(event.get("seq") or 0)
            upto = max(upto, seq)
            src = f"{seq}:{event.get('receivedAt')}"
            kind = event.get("type")
            if kind == "answer" and event.get("choice") in CHOICES:
                text = event.get("text")
                detail = {"shownAt": event.get("shownAt"), "receivedAt": event.get("receivedAt"),
                          "pendingOnPlugin": event.get("pending")}
                if event["choice"] == "option":
                    detail["optionIndex"] = event.get("option")
                    detail["optionText"] = text.strip()[:MAX_ANSWER_CHARS] if isinstance(text, str) else ""
                journal(state, "answer", event.get("id"), event["choice"], event.get("answeredAt"), detail, src=src)
            elif kind == "shown":
                journal(state, "shown", event.get("id"), band_ms=event.get("shownAt"), applied=1, src=src)
    if upto:
        client.call("POST", "/v1/events/ack", {"upto": upto})
    return apply_pending(state)


def diagnose(state: gtd.State, client: BridgeClient, wait: float = 4.0) -> Dict[str, Any]:
    """Go/no-go test for both directions of the interconnect link.

    band->host: the band app sends hello when it starts and every 15 s.
    round trip: the band answers a ping with pong.
    """
    start = int(client.call("GET", "/v1/status").get("nowMs", 0))
    launch = client.call("POST", "/v1/ping", {"launch": True})
    time.sleep(wait)  # a freshly launched app may miss the first ping
    second = client.call("POST", "/v1/ping", {"launch": False})
    time.sleep(2.0)
    status = client.call("GET", "/v1/status")
    return {
        "launch": launch,
        "ping": second,
        "band_to_host": max(status.get("lastHelloMs", 0), status.get("lastPongMs", 0)) >= start > 0,
        "round_trip": status.get("lastPongMs", 0) >= start > 0,
        "status": status,
    }


def report(state: gtd.State) -> Dict[str, Any]:
    pushed = {r["question_key"] for r in state.rows("SELECT DISTINCT question_key FROM band_events WHERE kind='pushed'")}
    answers = state.rows("SELECT question_key, choice, band_ms, detail FROM band_events WHERE kind='answer'")
    latencies = []
    for row in answers:
        shown = json.loads(row["detail"] or "{}").get("shownAt")
        if shown and row["band_ms"] and row["band_ms"] >= shown:
            latencies.append((row["band_ms"] - shown) / 1000)
    answered = {row["question_key"] for row in answers}
    return {
        "pushed": len(pushed),
        "answered": len(answered & pushed),
        "answer_rate": round(len(answered & pushed) / len(pushed), 3) if pushed else None,
        "median_seconds_shown_to_answer": round(statistics.median(latencies), 1) if latencies else None,
        "choices": {choice: sum(1 for row in answers if row["choice"] == choice) for choice in CHOICES},
    }


def run(state: gtd.State, state_dir: Path, bridge_file: Optional[Path], interval: float) -> None:
    client: Optional[BridgeClient] = None
    while True:
        try:
            client = client or BridgeClient.discover(state_dir, bridge_file)
            sync(state, client)
            key = push(state, client)
            if key:
                logger.info("pushed %s", key)
            ahead = prefetch(state)
            if ahead:
                logger.info("prepared options for %s", ahead)
        except BridgeError as e:
            logger.warning("%s", e)
            client = None  # rediscover: adb forward or token may have changed
        except sqlite3.Error as e:
            logger.warning("database: %s; retrying next cycle", e)
        time.sleep(interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(description="GTD <-> Xiaomi band bridge")
    parser.add_argument("--state", type=Path, default=gtd.DEFAULT_STATE)
    parser.add_argument("--bridge", type=Path, help="explicit bridge.json (default: pairing, then desktop AstroBox)")
    parser.add_argument("command", choices=["pair", "status", "ping", "push", "sync", "run", "report"])
    parser.add_argument("--code", help="pairing code shown on the plugin page (pair)")
    parser.add_argument("--adb", help="adb serial of the phone running AstroBox, e.g. 192.168.1.5:5555 (pair)")
    parser.add_argument("--interval", type=float, default=20.0)
    args = parser.parse_args()
    state_dir = args.state.expanduser()
    if args.command == "pair":
        if not args.code:
            parser.error("pair needs --code")
        print(json.dumps(pair(state_dir, args.code, args.adb), ensure_ascii=False, indent=2))
        return
    state = open_state(state_dir)
    if args.command == "report":
        print(json.dumps(report(state), ensure_ascii=False, indent=2))
        return
    if args.command == "run":
        run(state, state_dir, args.bridge, args.interval)
        return
    client = BridgeClient.discover(state_dir, args.bridge)
    if args.command == "status":
        value: Any = client.call("GET", "/v1/status")
    elif args.command == "ping":
        value = diagnose(state, client)
    elif args.command == "push":
        value = {"pushed": push(state, client)}
    else:
        value = {"applied": sync(state, client)}
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except BridgeError as error:
        logger.error("%s", error)
        sys.exit(2)
