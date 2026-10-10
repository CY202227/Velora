"""Parse SillyTavern chara_card_v2 (and flat V1) JSON into Persona fields."""

from __future__ import annotations

import re
from typing import Any

from server.core.persona.default import DEFAULT_PERSONA, Persona
from server.core.persona.macros import apply_macros


class CharaCardError(ValueError):
    """Invalid or unsupported character card."""


def _s(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").strip()


def _list_str(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_s(x) for x in value if _s(x)]


def build_setting_block(
    *,
    description: str = "",
    personality: str = "",
    scenario: str = "",
) -> str:
    parts: list[str] = []
    if description:
        parts.append(description)
    if personality:
        parts.append(f"Personality:\n{personality}")
    if scenario:
        parts.append(f"Scenario:\n{scenario}")
    return "\n\n".join(parts).strip()


def compose_system_prompt(
    *,
    description: str = "",
    personality: str = "",
    scenario: str = "",
    card_system_prompt: str = "",
    char_name: str = "",
) -> str:
    """Build Velora system_prompt from V2 card fields."""
    setting = build_setting_block(
        description=description, personality=personality, scenario=scenario
    )
    card_sys = card_system_prompt.strip()
    original = DEFAULT_PERSONA.system_prompt
    if card_sys:
        text = apply_macros(
            card_sys, char_name=char_name or "Character", original=original
        )
        if setting:
            return f"{text}\n\n{setting}".strip()
        return text
    if setting:
        return apply_macros(setting, char_name=char_name or "Character")
    return original


_USER_LINE = re.compile(
    r"^\s*(?:\{\{\s*user\s*\}\}|User)\s*:\s*(.*)$", re.I
)
_CHAR_LINE = re.compile(
    r"^\s*(?:\{\{\s*char\s*\}\}|Char)\s*:\s*(.*)$", re.I
)


def parse_mes_example(raw: str) -> list[str]:
    """Parse mes_example into even-length begin_dialogs when possible."""
    text = _s(raw)
    if not text:
        return []
    # Split on <START> blocks; take dialog lines.
    chunks = re.split(r"(?i)<\s*START\s*>", text)
    dialogs: list[str] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        pending_user: str | None = None
        for line in chunk.split("\n"):
            line = line.strip()
            if not line:
                continue
            um = _USER_LINE.match(line)
            cm = _CHAR_LINE.match(line)
            if um:
                pending_user = um.group(1).strip()
            elif cm and pending_user is not None:
                dialogs.extend([pending_user, cm.group(1).strip()])
                pending_user = None
    if len(dialogs) >= 2 and len(dialogs) % 2 == 0:
        return dialogs
    return []


def card_data_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize V1 flat or V2 envelope to a data dict."""
    if not isinstance(payload, dict):
        raise CharaCardError("card must be a JSON object")
    spec = _s(payload.get("spec"))
    if spec == "chara_card_v2" or "data" in payload:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise CharaCardError("chara_card_v2 requires data object")
        if spec and spec != "chara_card_v2":
            raise CharaCardError(f"unsupported spec: {spec}")
        return data
    # V1 flat
    if payload.get("name"):
        return payload
    raise CharaCardError("unrecognized character card format")


def persona_from_card_data(
    data: dict[str, Any],
    *,
    persona_id: str,
    source: str = "chara_v2",
) -> Persona:
    name = _s(data.get("name")) or "Unnamed"
    description = _s(data.get("description"))
    personality = _s(data.get("personality"))
    scenario = _s(data.get("scenario"))
    first_mes = _s(data.get("first_mes"))
    mes_example = _s(data.get("mes_example"))
    card_sys = _s(data.get("system_prompt"))
    post_hist = _s(data.get("post_history_instructions"))
    alts = _list_str(data.get("alternate_greetings"))
    book = data.get("character_book")
    if book is not None and not isinstance(book, dict):
        book = None

    begin = parse_mes_example(mes_example)
    import_meta: dict[str, Any] = {
        "creator_notes": _s(data.get("creator_notes")),
        "tags": data.get("tags") if isinstance(data.get("tags"), list) else [],
        "creator": _s(data.get("creator")),
        "character_version": _s(data.get("character_version")),
        "extensions": data.get("extensions")
        if isinstance(data.get("extensions"), dict)
        else {},
    }
    if mes_example and not begin:
        import_meta["mes_example_raw"] = mes_example

    system = compose_system_prompt(
        description=description,
        personality=personality,
        scenario=scenario,
        card_system_prompt=card_sys,
        char_name=name,
    )
    # Apply macros to opener / greetings / begin at resolve time too;
    # store raw with macros preserved in opener fields.
    avatar = _s(data.get("avatar")) or None

    return Persona(
        id=persona_id,
        name=name,
        system_prompt=system,
        style_defaults={},
        opener=first_mes or None,
        begin_dialogs=begin,
        tool_names=None,
        skill_names=None,
        import_meta=import_meta,
        source=source,
        description=description,
        personality=personality,
        scenario=scenario,
        post_history_instructions=post_hist,
        alternate_greetings=alts,
        character_book=book,
        avatar_path=None,
        avatar_url=avatar if avatar and avatar.startswith("http") else None,
    )


def parse_chara_card_json(
    payload: dict[str, Any],
    *,
    persona_id: str,
) -> Persona:
    data = card_data_from_payload(payload)
    return persona_from_card_data(data, persona_id=persona_id)
