"""Resolve persona id to Persona (builtin or DB)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from server.core.persona.default import (
    BUILTIN_PERSONA_ID,
    DEFAULT_PERSONA,
    Persona,
)
from server.core.persona.macros import apply_macros

if TYPE_CHECKING:
    from server.core.conversation.store import ConversationStore, PersonaRow


def row_to_persona(row: PersonaRow) -> Persona:
    return Persona(
        id=row.id,
        name=row.name,
        system_prompt=row.system_prompt,
        style_defaults=dict(row.style_defaults or {}),
        opener=row.opener,
        begin_dialogs=list(row.begin_dialogs or []),
        tool_names=row.tool_names,
        skill_names=row.skill_names,
        import_meta=row.import_meta,
        source=row.source,
        description=row.description or "",
        personality=row.personality or "",
        scenario=row.scenario or "",
        post_history_instructions=row.post_history_instructions or "",
        alternate_greetings=list(row.alternate_greetings or []),
        character_book=row.character_book,
        avatar_path=row.avatar_path,
        avatar_url=row.avatar_url,
    )


def prepare_persona_for_prompt(persona: Persona) -> Persona:
    """Return a shallow copy with macros applied for prompt assembly."""
    name = persona.name
    system = apply_macros(persona.system_prompt, char_name=name)
    opener = (
        apply_macros(persona.opener, char_name=name) if persona.opener else None
    )
    begin = [
        apply_macros(x, char_name=name) for x in (persona.begin_dialogs or [])
    ]
    alts = [
        apply_macros(x, char_name=name)
        for x in (persona.alternate_greetings or [])
    ]
    post = apply_macros(persona.post_history_instructions, char_name=name)
    return Persona(
        id=persona.id,
        name=persona.name,
        system_prompt=system,
        style_defaults=dict(persona.style_defaults or {}),
        opener=opener,
        begin_dialogs=begin,
        tool_names=persona.tool_names,
        skill_names=persona.skill_names,
        examples=list(persona.examples or []),
        import_meta=persona.import_meta,
        source=persona.source,
        description=apply_macros(persona.description, char_name=name),
        personality=apply_macros(persona.personality, char_name=name),
        scenario=apply_macros(persona.scenario, char_name=name),
        post_history_instructions=post,
        alternate_greetings=alts,
        character_book=persona.character_book,
        avatar_path=persona.avatar_path,
        avatar_url=persona.avatar_url,
    )


async def resolve_persona(
    store: ConversationStore | None,
    persona_id: str | None,
) -> Persona:
    """Load persona by id; unknown ids fall back to builtin."""
    pid = (persona_id or "").strip() or BUILTIN_PERSONA_ID
    if pid == BUILTIN_PERSONA_ID:
        return DEFAULT_PERSONA
    if store is None:
        return DEFAULT_PERSONA
    row = await store.get_persona(pid)
    if row is None:
        return DEFAULT_PERSONA
    return row_to_persona(row)
