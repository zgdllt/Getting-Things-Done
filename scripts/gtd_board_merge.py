"""Deterministic board identity merging, with aliases retained for user feedback."""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

import gtd_threads

if TYPE_CHECKING:
    from gtd_board import BoardItem

PRIORITY = {"reminder": 0, "event": 1, "project": 2, "task": 3}


def quadrant(item: BoardItem) -> int:
    """Derive a quadrant from the final, possibly merged scores."""
    return (1 if item["urgency"] >= 0.7 else 2) if item["importance"] >= 0.5 else (
        3 if item["urgency"] >= 0.7 else 4)


def bigrams(text: str) -> set[str]:
    """Use unique adjacent characters of the existing normalized title spelling."""
    text = gtd_threads.norm(text)
    return {text[index:index + 2] for index in range(len(text) - 1)}


def next_step(item: BoardItem, contexts: dict[str, Any]) -> str:
    """Read a project's confirmed next step without inferring task associations."""
    return contexts.get(item["project"], {}).get("memory", {}).get("next_step") or ""


def same_date_bigrams(left: BoardItem, right: BoardItem, contexts: dict[str, Any]) -> bool:
    """Match same-date bigram overlap, including shorter confirmed project plans."""
    texts = [next_step(item, contexts) or item["title"] if item["kind"] == "project" else item["title"]
             for item in (left, right)]
    a, b = (bigrams(text) for text in texts)
    if a | b and len(a & b) / len(a | b) >= 0.3:
        return True
    for project, other in ((left, right), (right, left)):
        if project["kind"] == "project":
            step = bigrams(next_step(project, contexts))
            if step and len(step & bigrams(other["title"])) / len(step) >= 0.6:
                return True
    return False


def duplicate(left: BoardItem, right: BoardItem, contexts: dict[str, Any]) -> bool:
    """Match explicit same-day titles, a project's next step, or close sibling tasks."""
    a, b = gtd_threads.norm(left["title"]), gtd_threads.norm(right["title"])
    if not a or not b:
        return False
    similarity = SequenceMatcher(None, a, b).ratio()
    same_date = bool(left["due"] and right["due"] and left["due"][:10] == right["due"][:10])
    if same_date:
        if similarity >= 0.6 or same_date_bigrams(left, right, contexts):
            return True
    if left["project"] and left["project"] == right["project"]:
        if left["kind"] == right["kind"] == "task" and similarity >= 0.85:
            return True
        if {left["kind"], right["kind"]} == {"task", "project"}:
            task = left if left["kind"] == "task" else right
            step = gtd_threads.norm(next_step(task, contexts))
            title = gtd_threads.norm(task["title"])
            return bool(step and (step in title or (same_date and len(step) >= 4 and step[-4:] in title)))
    return False


def merge(items: list[BoardItem], contexts: dict[str, Any]) -> list[BoardItem]:
    """Merge connected duplicate groups, retaining strongest scores and every source ID."""
    parents = list(range(len(items)))

    def root(index: int) -> int:
        """Find and compress one disjoint-set path."""
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for i, left in enumerate(items):
        for j in range(i):
            if duplicate(left, items[j], contexts):
                parents[root(i)] = root(j)
    groups: dict[int, list[BoardItem]] = {}
    for i, item in enumerate(items):
        groups.setdefault(root(i), []).append(item)
    survivors: list[BoardItem] = []
    for group in groups.values():
        survivor = min(group, key=lambda item: (PRIORITY[item["kind"]], item["id"]))
        survivor["merged_ids"] = sorted(item["id"] for item in group if item is not survivor)
        for field in ("importance", "urgency"):
            strongest = max(group, key=lambda item: item[field])
            survivor[field] = strongest[field]
            survivor[field + "_reasons"] = strongest[field + "_reasons"][:]
        survivor["activity_at"] = max((item["activity_at"] for item in group), default=0.0)
        pinned = sorted((item for item in group if item["pinned"]),
                        key=lambda item: (PRIORITY[item["kind"]], item["id"]))
        if pinned:
            survivor["quadrant"], survivor["pinned"] = pinned[0]["quadrant"], True
        else:
            survivor["quadrant"] = quadrant(survivor)
        survivors.append(survivor)
    aliases = {identity: item["id"] for item in survivors for identity in [item["id"], *item["merged_ids"]]}
    for item in survivors:
        parent = aliases.get(item["parent"])
        item["parent"] = parent if parent != item["id"] else None
    return survivors


def hierarchy(items: list[BoardItem]) -> None:
    """Mark nested tasks and collect their surviving IDs in final board order."""
    by_id = {item["id"]: item for item in items}
    for item in items:
        if item["parent"] not in by_id:
            item["parent"] = None
        item["top_level"] = not (item["kind"] == "task" and item["parent"] and not item["due"])
        item["children"], item["children_count"] = [], 0
    for item in items:
        if not item["top_level"]:
            parent = by_id[item["parent"]]
            parent["children"].append(item["id"])
            parent["children_count"] += 1


def find(items: list[BoardItem], identity: str) -> BoardItem | None:
    """Resolve either a surviving card ID or one of its merged source IDs."""
    return next((item for item in items if identity == item["id"] or identity in item.get("merged_ids", [])), None)
