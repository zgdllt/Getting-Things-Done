"""Replay a past day through local-only history and score it against your decisions.

Computer History keeps only ~1-2 hours of raw events, so past days cannot be
replayed through it. Instead: run the GTD extractor on local history alone for
a day you already triaged, then ask a judge model which proposals correspond to
candidates GTD (fed by Computer History) produced that day. Your decisions on
those candidates are the labels:

  positive = accepted or resolved     negative = dismissed     unlabeled = inbox

Labels come from the Computer History pipeline, so it has a home advantage:
novel local proposals cannot be scored automatically and are listed for review.
Reports (with personal content) go to ~/.codex/personal-gtd/experiments/ (0600).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import gtd
import gtd_local
import gtd_provider
import gtd_sources

POSITIVE = ("accepted", "resolved")
NEGATIVE = ("dismissed",)
JUDGE = """你在核对两组待办候选是否指同一件事。
EXISTING 是用户当天已处理过的候选；PROPOSALS 是另一套系统对同一天提出的候选。
对每个 proposal，找出含义相同（同一件具体事情，措辞可以不同）的 existing key；没有就填 null。
只看事情本身是否相同，不要因为同一项目下的不同事情而匹配。
输出 {"matches":[{"proposal":"p1","existing":"<key 或 null>"}]}，覆盖每个 proposal。"""


def day_bounds(day: str) -> Tuple[dt.datetime, dt.datetime]:
    tz = dt.datetime.now().astimezone().tzinfo
    start = dt.datetime.fromisoformat(day).replace(tzinfo=tz)
    return start, start + dt.timedelta(days=1)


def chunks(events: List[Dict[str, Any]], limit: int) -> List[List[Dict[str, Any]]]:
    groups: List[List[Dict[str, Any]]] = [[]]
    size = 0
    for event in events:
        cost = len(event["text"]) + 120
        if groups[-1] and size + cost > limit:
            groups.append([])
            size = 0
        groups[-1].append(event)
        size += cost
    return [group for group in groups if group]


def extract(provider: Dict[str, str], group: List[Dict[str, Any]], end: dt.datetime) -> Dict[str, Any]:
    """Run the real GTD extractor prompt on one chunk with short event ids."""
    short = [{**event, "event_key": f"e{i}"} for i, event in enumerate(group)]
    context = {"now": end.isoformat(), "timezone": gtd_sources.timezone_name({}),
               "existing_candidates": [], "existing_questions": [], "system_permissions": {},
               "existing_reminders": [], "existing_calendar_events": [],
               "source_status": {"adapter": "local_history"}, "raw_event_count": len(short), "events": short}
    system = gtd_provider.schema_instruction(json.loads((gtd.SKILL / "references/analysis.schema.json").read_text()))
    user = (gtd.SKILL / "references/extractor.md").read_text() + "\n\nOBSERVED_DATA_JSON:\n" + gtd.encode(context)
    value = gtd_provider.chat_json(provider, system, user)
    try:
        gtd.validate_analysis(value, short)
    except ValueError as error:
        value = gtd_provider.chat_json(provider, system, user + f"\n\n上一次输出无效：{error}。请修正后重新输出完整 JSON。")
        gtd.validate_analysis(value, short)
    return value


def labeled(state: gtd.State, start: dt.datetime, end: dt.datetime) -> List[Dict[str, Any]]:
    result = []
    for row in state.rows("SELECT key, title, project, state, evidence FROM candidates"):
        times = [gtd_sources.parse_timestamp(e.get("timestamp")) for e in json.loads(row["evidence"] or "[]")]
        times = [t for t in times if t]
        if times and start <= min(times) < end:
            result.append({"key": row["key"], "title": row["title"], "project": row["project"], "state": row["state"]})
    return result


JUDGE_BATCH = 25


def judge(provider: Dict[str, str], existing: List[Dict[str, Any]], proposals: List[Dict[str, Any]],
          cache: Optional[Path] = None) -> Dict[str, Any]:
    """Match proposals to existing candidates in small batches; a failed batch leaves its proposals unjudged.

    Successful batches are cached (keyed by the batch's proposal titles) so reruns only retry failures."""
    catalogue = [{"key": c["key"], "title": c["title"], "project": c["project"]} for c in existing]
    valid = {c["key"] for c in existing}
    matches: Dict[str, Any] = {}
    for at in range(0, len(proposals), JUDGE_BATCH):
        batch = proposals[at:at + JUDGE_BATCH]
        payload = {"EXISTING": catalogue,
                   "PROPOSALS": [{"id": p["id"], "title": p["title"], "project": p.get("project")} for p in batch]}
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
        path = cache / f"judge-{digest}.json" if cache else None
        if path and path.exists():
            value = json.loads(path.read_text())
        else:
            try:
                value = gtd_provider.chat_json(provider, "只输出 JSON。",
                                               JUDGE + "\n\n" + json.dumps(payload, ensure_ascii=False))
            except gtd_provider.ProviderError as error:
                print(f"  judge batch {at // JUDGE_BATCH} failed: {str(error)[:120]}", flush=True)
                continue
            if path:
                path.write_text(json.dumps(value, ensure_ascii=False))
                os.chmod(path, 0o600)
        for m in value.get("matches", []):
            if isinstance(m, dict) and "proposal" in m:
                matches[m["proposal"]] = m["existing"] if m.get("existing") in valid else None
    return matches


def score(existing: List[Dict[str, Any]], proposals: List[Dict[str, Any]], matches: Dict[str, Any]) -> Dict[str, Any]:
    state = {c["key"]: c["state"] for c in existing}
    hit = {key for key in matches.values() if key}
    pos = {k for k, s in state.items() if s in POSITIVE}
    neg = {k for k, s in state.items() if s in NEGATIVE}
    ch_pos, ch_neg = len(pos), len(neg)
    return {
        "computer_history_labeled_precision": round(ch_pos / (ch_pos + ch_neg), 3) if ch_pos + ch_neg else None,
        "positives": ch_pos, "negatives": ch_neg, "local_proposals": len(proposals),
        "positives_recovered": len(hit & pos),
        "positive_recall": round(len(hit & pos) / ch_pos, 3) if ch_pos else None,
        "negatives_repeated": len(hit & neg),
        "local_labeled_precision": round(len(hit & pos) / len(hit & (pos | neg)), 3) if hit & (pos | neg) else None,
        "matched_unlabeled": len(hit - pos - neg),
        "novel_proposals": sum(1 for p in proposals if p["id"] in matches and not matches[p["id"]]),
        "unjudged_proposals": sum(1 for p in proposals if p["id"] not in matches),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--day", required=True, help="local date, e.g. 2026-11-04")
    parser.add_argument("--provider", type=Path, default=gtd.DEFAULT_STATE / "secrets" / "madmodel.json")
    parser.add_argument("--chunk-chars", type=int, default=20000)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    provider = gtd_provider.load(args.provider)
    state = gtd.State(gtd.DEFAULT_STATE)
    start, end = day_bounds(args.day)
    events = gtd_local.window(start, end)
    groups = chunks(events, args.chunk_chars)
    print(f"{args.day}: {len(events)} local events in {len(groups)} chunks", flush=True)
    # Successful chunks are cached so a rerun only retries the failed ones.
    cache = gtd.DEFAULT_STATE / "experiments" / "cache"
    cache.mkdir(parents=True, mode=0o700, exist_ok=True)
    results: List[Dict[str, Any]] = []
    pending: Dict[int, List[Dict[str, Any]]] = {}
    for i, group in enumerate(groups):
        path = cache / f"{args.day}-{args.chunk_chars}-{i}.json"
        if path.exists():
            results.append(json.loads(path.read_text()))
        else:
            pending[i] = group
    print(f"  cached {len(results)}, to run {len(pending)}", flush=True)
    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(extract, provider, group, end): i for i, group in pending.items()}
        for future in concurrent.futures.as_completed(futures):
            i = futures[future]
            try:
                value = future.result()
            except (gtd_provider.ProviderError, ValueError) as error:
                print(f"  chunk {i} failed: {str(error)[:160]}", flush=True)
                continue
            path = cache / f"{args.day}-{args.chunk_chars}-{i}.json"
            path.write_text(json.dumps(value, ensure_ascii=False))
            os.chmod(path, 0o600)
            results.append(value)
            print(f"  chunk {i} ok", flush=True)
    proposals = [{"id": f"p{i}", "title": c["title"], "bucket": c["bucket"], "project": c.get("project")}
                 for i, c in enumerate(c for r in results for c in r.get("candidates", []))]
    existing = labeled(state, start, end)
    matches = judge(provider, existing, proposals, cache) if proposals and existing else {}
    if len(results) < len(groups):
        print(f"incomplete: {len(results)}/{len(groups)} chunks; rerun to retry the rest", flush=True)
    report = {"day": args.day, "model": provider["model"], "chunks": len(groups), "chunks_ok": len(results),
              "events": len(events), "usage": gtd_provider.usage_total(results),
              "metrics": score(existing, proposals, matches),
              "proposals": [{**p, "matched": matches.get(p["id"]),
                             "matched_state": next((c["state"] for c in existing if c["key"] == matches.get(p["id"])), None)}
                            for p in proposals],
              "missed_positives": [c for c in existing if c["state"] in POSITIVE and c["key"] not in set(matches.values())]}
    out = gtd.DEFAULT_STATE / "experiments"
    out.mkdir(mode=0o700, exist_ok=True)
    path = out / f"local-vs-ch-{args.day}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    os.chmod(path, 0o600)
    print(json.dumps({"report": str(path), **report["metrics"], "usage": report["usage"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
