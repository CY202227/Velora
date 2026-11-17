"""Turn-owned command handles. Pending commands never outlive their turn."""
import asyncio
from contextvars import ContextVar

progress = ContextVar("command_progress", default=None)


async def close_commands(ctx):
    tasks = [entry["task"] for entry in ctx.extras.get("command_sessions", {}).values()]
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def append_output(channel, text):
    target = progress.get()
    if target is not None:
        target[channel] = (target[channel] + text)[-16000:]
