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


def duplicate(left: BoardItem, right: BoardItem, contexts: dict[str, Any]) -> bool:
    """Match explicit same-day titles, a project's next step, or close sibling tasks."""
    a, b = gtd_threads.norm(left["title"]), gtd_threads.norm(right["title"])
    if not a or not b:
        return False
    similarity = SequenceMatcher(None, a, b).ratio()
    if left["due"] and right["due"] and left["due"][:10] == right["due"][:10] and similarity >= 0.6:
        return True
    if left["project"] and left["project"] == right["project"]:
        if left["kind"] == right["kind"] == "task" and similarity >= 0.85:
            return True
        if {left["kind"], right["kind"]} == {"task", "project"}:
            task = left if left["kind"] == "task" else right
            step = gtd_threads.norm(contexts.get(task["project"], {}).get("memory", {}).get("next_step") or "")
            return bool(step and step in gtd_threads.norm(task["title"]))
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


def find(items: list[BoardItem], identity: str) -> BoardItem | None:
    """Resolve either a surviving card ID or one of its merged source IDs."""
    return next((item for item in items if identity == item["id"] or identity in item.get("merged_ids", [])), None)
