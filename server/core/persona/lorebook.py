"""Scan character_book (V2 lorebook) and build injection blocks."""

from __future__ import annotations

from typing import Any


def _norm(text: str, *, case_sensitive: bool) -> str:
    return text if case_sensitive else text.lower()


def _hit_keys(
    haystack: str,
    keys: list[str],
    *,
    case_sensitive: bool,
) -> bool:
    h = _norm(haystack, case_sensitive=case_sensitive)
    for key in keys:
        k = (key or "").strip()
        if not k:
            continue
        if _norm(k, case_sensitive=case_sensitive) in h:
            return True
    return False


def scan_character_book(
    book: dict[str, Any] | None,
    *,
    history_texts: list[str],
    user_text: str,
) -> tuple[str, str]:
    """Return (before_char, after_char) lore blocks to inject into system.

    Implements a practical subset of V2 CharacterBook rules.
    """
    if not book or not isinstance(book, dict):
        return "", ""
    entries = book.get("entries")
    if not isinstance(entries, list):
        return "", ""

    scan_depth = book.get("scan_depth")
    try:
        depth = int(scan_depth) if scan_depth is not None else 4
    except (TypeError, ValueError):
        depth = 4
    depth = max(0, min(depth, 40))

    recent = history_texts[-depth:] if depth else []
    scan_blob = "\n".join([*recent, user_text or ""])

    try:
        budget = int(book.get("token_budget") or 0)
    except (TypeError, ValueError):
        budget = 0
    # Rough chars ≈ tokens * 4 for CJK-ish mix.
    char_budget = budget * 4 if budget > 0 else 8000

    recursive = bool(book.get("recursive_scanning"))

    candidates: list[dict[str, Any]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        if raw.get("enabled") is False:
            continue
        content = str(raw.get("content") or "").strip()
        if not content:
            continue
        case_sensitive = bool(raw.get("case_sensitive"))
        keys = raw.get("keys") if isinstance(raw.get("keys"), list) else []
        keys_s = [str(k) for k in keys]
        constant = bool(raw.get("constant"))
        selective = bool(raw.get("selective"))
        secondary = (
            raw.get("secondary_keys")
            if isinstance(raw.get("secondary_keys"), list)
            else []
        )
        secondary_s = [str(k) for k in secondary]

        triggered = False
        if constant:
            triggered = True
        elif keys_s and _hit_keys(
            scan_blob, keys_s, case_sensitive=case_sensitive
        ):
            if selective and secondary_s:
                triggered = _hit_keys(
                    scan_blob, secondary_s, case_sensitive=case_sensitive
                )
            else:
                triggered = True
        if not triggered:
            continue

        try:
            order = int(raw.get("insertion_order") or 0)
        except (TypeError, ValueError):
            order = 0
        try:
            priority = int(raw.get("priority") or 10)
        except (TypeError, ValueError):
            priority = 10
        position = raw.get("position") or "after_char"
        if position not in ("before_char", "after_char"):
            position = "after_char"
        candidates.append(
            {
                "content": content,
                "insertion_order": order,
                "priority": priority,
                "position": position,
            }
        )

    if recursive and candidates:
        extra_blob = scan_blob + "\n" + "\n".join(c["content"] for c in candidates)
        seen = {c["content"] for c in candidates}
        for raw in entries:
            if not isinstance(raw, dict) or raw.get("enabled") is False:
                continue
            content = str(raw.get("content") or "").strip()
            if not content or content in seen:
                continue
            if bool(raw.get("constant")):
                continue
            case_sensitive = bool(raw.get("case_sensitive"))
            keys = raw.get("keys") if isinstance(raw.get("keys"), list) else []
            keys_s = [str(k) for k in keys]
            if keys_s and _hit_keys(
                extra_blob, keys_s, case_sensitive=case_sensitive
            ):
                try:
                    order = int(raw.get("insertion_order") or 0)
                except (TypeError, ValueError):
                    order = 0
                try:
                    priority = int(raw.get("priority") or 10)
                except (TypeError, ValueError):
                    priority = 10
                position = raw.get("position") or "after_char"
                if position not in ("before_char", "after_char"):
                    position = "after_char"
                candidates.append(
                    {
                        "content": content,
                        "insertion_order": order,
                        "priority": priority,
                        "position": position,
                    }
                )
                seen.add(content)

    # Sort: lower insertion_order first; then by priority (lower discarded first later)
    candidates.sort(key=lambda c: (c["insertion_order"], c["priority"]))

    before: list[str] = []
    after: list[str] = []
    used = 0
    # If over budget, drop highest priority number first (lower priority value kept).
    pool = list(candidates)
    while pool and used < char_budget:
        # Prefer keeping lower priority values when trimming: process in order,
        # skip if would exceed by adding lowest-priority-last.
        nxt = None
        for i, c in enumerate(pool):
            if used + len(c["content"]) <= char_budget or used == 0:
                nxt = pool.pop(i)
                break
        if nxt is None:
            # Drop highest priority number (least important)
            pool.sort(key=lambda c: -c["priority"])
            pool.pop(0)
            continue
        used += len(nxt["content"])
        if nxt["position"] == "before_char":
            before.append(nxt["content"])
        else:
            after.append(nxt["content"])

    return "\n\n".join(before).strip(), "\n\n".join(after).strip()
