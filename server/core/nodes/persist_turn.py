from __future__ import annotations

import logging

from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult
from server.core.computer.attachments import diff_attachments
from server.core.conversation.store import ConversationStore
from server.core.memory.client import AtomMemoryClient
from server.core.memory.consolidate import ConsolidateJob
from server.core.memory.write_gate import decide_memory_write
from server.core.reminders.text import finalize_reminder_reply

logger = logging.getLogger(__name__)


class PersistTurnNode:
    name = "PersistTurn"

    def __init__(
        self,
        store: ConversationStore,
        memory: AtomMemoryClient,
        consolidate_job: ConsolidateJob,
        *,
        consolidate_every_n: int = 3,
    ) -> None:
        self.store = store
        self.memory = memory
        self.consolidate_job = consolidate_job
        self.consolidate_every_n = consolidate_every_n

    async def process(self, ctx: TurnContext) -> NodeResult:
        proactive = bool(ctx.extras.get("proactive"))
        source = "reminder" if proactive else "chat"
        if proactive:
            note = ""
            meta = ctx.request.client_meta or {}
            if isinstance(meta.get("reminder_note"), str):
                note = meta["reminder_note"]
            ctx.assistant_text = finalize_reminder_reply(
                note, ctx.assistant_text
            )
        attachments: list[dict] = []
        settings = ctx.extras.get("settings")
        if settings is not None and getattr(settings, "computer_enabled", False):
            snap = ctx.extras.get("ws_snapshot")
            if isinstance(snap, dict) or snap is None:
                try:
                    attachments = diff_attachments(
                        settings.workspaces_dir,
                        ctx.session_id,
                        snap if isinstance(snap, dict) else None,
                    )
                except Exception:
                    logger.exception("attachment diff failed")
                    attachments = []

        user_turn = None
        if not proactive:
            # Proactive wakes are outbound: do not persist a fake "user" bubble.
            user_turn = await self.store.add_turn(
                ctx.session_id,
                "user",
                ctx.request.user_text,
                source=source,
            )
        asst_turn = await self.store.add_turn(
            ctx.session_id,
            "assistant",
            ctx.assistant_text,
            source=source,
            attachments=attachments,
        )
        ctx.turn_id = asst_turn.id
        ctx.extras["attachments"] = attachments

        decision = decide_memory_write(
            ctx.request.user_text,
            is_correction=ctx.is_correction,
            proactive=proactive,
        )
        if not decision.write:
            await ctx.publish(
                "persisted",
                {
                    "turn_id": ctx.turn_id,
                    "memory_wrote": False,
                    "consolidated": False,
                    "memory_skipped_reason": decision.reason,
                    "attachments": attachments,
                },
            )
            return NodeResult.CONTINUE

        if proactive:
            content = f"提醒：{ctx.assistant_text}"
        else:
            content = f"用户：{ctx.request.user_text}\nAI：{ctx.assistant_text}"
        if ctx.is_correction:
            written = await self.memory.add_source(
                ctx.memory_space_uid,
                kind="correction",
                content=ctx.request.user_text,
                salience=0.9,
                external_ref={
                    "system": "velora",
                    "session_id": ctx.session_id,
                    "turn_id": (user_turn.id if user_turn else asst_turn.id),
                },
            )
            if written is not None:
                ctx.memory_wrote = True
                ctx.consolidated = await self.consolidate_job.run_now(
                    ctx.memory_space_uid, trigger="correction", source_id=written["id"]
                )
        else:
            written = await self.memory.add_source(
                ctx.memory_space_uid,
                kind="turn",
                content=content,
                salience=0.2,
                external_ref={
                    "system": "velora",
                    "session_id": ctx.session_id,
                    "turn_id": asst_turn.id,
                },
            )
            if written is not None:
                ctx.memory_wrote = True
                count = await self.store.count_turns(ctx.session_id)
                # count includes user+assistant; every N assistant turns ≈ 2N rows
                if self.consolidate_every_n > 0 and (count // 2) % self.consolidate_every_n == 0:
                    await self.consolidate_job.enqueue(
                        ctx.memory_space_uid, trigger="scheduled"
                    )

        await ctx.publish(
            "persisted",
            {
                "turn_id": ctx.turn_id,
                "memory_wrote": ctx.memory_wrote,
                "consolidated": ctx.consolidated,
                "memory_write_reason": decision.reason,
                "attachments": attachments,
            },
        )
        return NodeResult.CONTINUE
