from __future__ import annotations

from server.config import Settings
from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult
from server.core.computer.attachments import snapshot_workspace
from server.core.conversation.store import ConversationStore
from server.core.memory.client import AtomMemoryClient
from server.core.persona.resolve import prepare_persona_for_prompt, resolve_persona
from server.core.persona.style import merge_style_knobs


class LoadContextNode:
    name = "LoadContext"

    def __init__(
        self,
        store: ConversationStore,
        settings: Settings,
        memory: AtomMemoryClient | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.memory = memory

    async def process(self, ctx: TurnContext) -> NodeResult:
        session = await self.store.get_session(ctx.session_id)
        if session is None:
            ctx.stop_reason = "session_not_found"
            await ctx.publish("error", {"message": "Session not found"})
            return NodeResult.STOP

        persona_id = ctx.request.persona_id or session.persona_id
        raw = await resolve_persona(self.store, persona_id)
        ctx.persona = prepare_persona_for_prompt(raw)
        ctx.memory_space_uid = session.memory_space_uid
        # Settings are the live source of truth for model routing; session
        # only seeds defaults when a field was never configured globally.
        ctx.model = self.settings.llm_model or session.model
        ctx.llm_base_url = self.settings.llm_base_url
        ctx.llm_api_key = self.settings.llm_api_key
        ctx.tts_enabled = session.tts_enabled
        ctx.style_knobs = merge_style_knobs(
            ctx.persona.style_defaults if ctx.persona else None,
            session.style_knobs,
            default_warmth=self.settings.default_warmth,
        )
        ctx.extras["store"] = self.store
        ctx.extras["settings"] = self.settings
        if self.memory is not None:
            ctx.extras["memory"] = self.memory

        meta = ctx.request.client_meta or {}
        proactive = bool(meta.get("proactive"))
        ctx.extras["proactive"] = proactive
        if proactive and self.settings.computer_enabled:
            ctx.extras["ws_snapshot"] = snapshot_workspace(
                self.settings.workspaces_dir, ctx.session_id
            )
        elif self.settings.computer_enabled:
            # Ordinary turns also snapshot so write_file cards work.
            ctx.extras["ws_snapshot"] = snapshot_workspace(
                self.settings.workspaces_dir, ctx.session_id
            )

        turns = await self.store.list_turns(
            ctx.session_id, limit=self.settings.history_max_messages
        )
        # Hide synthetic reminder wakes from model history; keep assistant nudges.
        ctx.history = [
            {"role": t.role, "content": t.content}
            for t in turns
            if not (t.source == "reminder" and t.role == "user")
        ]

        text = ctx.request.user_text.strip()
        ctx.is_correction = text.startswith("请记住") or text.startswith("记住：")
        return NodeResult.CONTINUE
