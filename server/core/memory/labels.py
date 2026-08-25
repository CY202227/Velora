"""Human-readable labels and summary helpers for memory atoms."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

KIND_LABELS: dict[str, str] = {
    "person": "人物偏好",
    "preference": "偏好",
    "pref": "偏好",
    "fact": "事实",
    "event": "事件",
    "task": "待办",
    "self": "关于助手",
    "relation": "关系",
    "place": "地点",
    "time": "时间约定",
    "correction": "纠正",
}


def kind_label(kind: str | None) -> str:
    k = (kind or "other").strip() or "other"
    return KIND_LABELS.get(k, k)


def build_memory_summary(
    atoms: list[dict[str, Any]],
    *,
    per_kind: int = 8,
) -> dict[str, Any]:
    """Group atom statements by kind into a product-facing summary."""
    by_kind: dict[str, list[dict[str, str]]] = defaultdict(list)
    for atom in atoms:
        status = str(atom.get("status") or "active").lower()
        if status in ("archived", "deleted", "tombstone"):
            continue
        kind = str(atom.get("kind") or "other")
        key = str(atom.get("key") or "")
        statement = str(atom.get("statement") or "").strip()
        if not key or not statement:
            continue
        if len(by_kind[kind]) >= per_kind:
            continue
        by_kind[kind].append({"key": key, "statement": statement})

    sections: list[dict[str, Any]] = []
    counts: list[str] = []
    total = 0
    for kind in sorted(by_kind.keys()):
        items = by_kind[kind]
        # Approximate count as listed (capped); headline uses listed total
        n = len(items)
        total += n
        label = kind_label(kind)
        sections.append({"kind": kind, "label": label, "items": items, "count": n})
        counts.append(f"{label} {n}")

    if total == 0:
        headline = "还没有记住什么。可以说「请记住…」。"
    else:
        headline = f"已记住约 {total} 条" + (
            f"：{' · '.join(counts)}" if counts else ""
        )

    return {
        "total": total,
        "headline": headline,
        "sections": sections,
    }
