from server.core.agent.loop import AgentLoop
from server.core.agent.state import RunStatus
from server.core.chain.context import TurnContext
from server.core.chain.types import NodeResult


class LLMNode(AgentLoop):
    """Chain adapter for agent execution."""

    name = "LLM"

    async def process(self, ctx: TurnContext) -> NodeResult:
        try:
            outcome = await self.run(ctx)
        finally:
            await ctx.save_checkpoint("model_stage_end")
        return (
            NodeResult.CONTINUE
            if outcome.status is RunStatus.COMPLETED
            else NodeResult.STOP
        )
