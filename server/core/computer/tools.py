"""Register local computer tools into ToolRegistry."""

from __future__ import annotations

from typing import Any
import json

from server.core.chain.context import TurnContext
from server.core.execution.service import execute
from server.core.tools.registry import ToolRegistry
from server.core.tools.types import ToolSpec


def _settings(ctx: TurnContext):
    return ctx.extras.get("settings")


def register_computer_tools(registry: ToolRegistry) -> None:
    async def command_control(args, ctx):
        import asyncio
        from server.core.execution.command_session import progress
        entries = ctx.extras.setdefault("command_sessions", {})
        action = args["action"]
        if action == "start":
            settings = _settings(ctx)
            if settings is None or settings.execution_require_approval or args.get("permissions"):
                return json.dumps({"ok": False, "error": "use synchronous tools for interactive approval"})
            if not args.get("command", "").strip():
                return json.dumps({"ok": False, "error": "command required"})
            if sum(not e["task"].done() for e in entries.values()) >= 4:
                return json.dumps({"ok": False, "error": "at most four active commands per turn"})
            identifier = ctx.extras.get("execution_id")
            if not identifier or identifier in entries:
                return json.dumps({"ok": False, "error": "missing or reused execution identity"})
            entry = {"stdout": "", "stderr": ""}
            from dataclasses import replace
            command_ctx = replace(ctx, extras=dict(ctx.extras))
            async def launch():
                token = progress.set(entry)
                try:
                    return await _dispatch("run_shell", {"command": args["command"]}, command_ctx)
                finally:
                    progress.reset(token)
            entry["task"] = asyncio.create_task(launch())
            entries[identifier] = entry
        else:
            identifier = args.get("execution_id")
            entry = entries.get(identifier)
            if entry is None:
                return json.dumps({"ok": False, "error": "unknown command in this turn; do not replay"})
            if action == "cancel" and not entry["task"].done():
                entry["task"].cancel()
        task = entry["task"]
        try:
            result = await asyncio.wait_for(asyncio.shield(task), args.get("wait_ms", 1000) / 1000)
            return result
        except asyncio.TimeoutError:
            def safe(text):
                return text.replace(ctx.llm_api_key, "[redacted]") if ctx.llm_api_key else text
            return json.dumps({"ok": True, "status": "running", "execution_id": identifier,
                               "stdout": safe(entry["stdout"]), "stderr": safe(entry["stderr"]), "output_window": "last 16000 characters per stream"})
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
            return json.dumps({"ok": False, "status": "result_unknown", "execution_id": identifier,
                               "error": "cancelled; effects are not rolled back; do not replay"})

    registry.register(ToolSpec("command_session", "启动长命令、读取中途输出或取消。同轮内使用句柄；结束本轮会取消未结束命令。需审批时使用 run_shell。", {
        "type": "object", "properties": {"action": {"type": "string", "enum": ["start", "poll", "cancel"]},
        "command": {"type": "string"}, "execution_id": {"type": "string"}, "wait_ms": {"type": "integer", "minimum": 1, "maximum": 10000}},
        "required": ["action"], "additionalProperties": False}, command_control))
    async def read_output(args, ctx):
        import asyncio
        from server.core.execution.artifacts import OutputStore
        settings = _settings(ctx)
        if settings is None:
            return '{"ok":false,"error":"execution settings missing"}'
        try:
            result = await asyncio.to_thread(OutputStore(settings.execution_output_dir, settings.execution_output_storage_bytes).read,
                args.get("execution_id", ""), ctx.session_id, args.get("channel", "stdout"), args.get("offset", 0), args.get("limit", 12000))
            return json.dumps(result, ensure_ascii=False)
        except (ValueError, OSError) as exc:
            return json.dumps({"ok": False, "error": str(exc)})
    registry.register(ToolSpec(name="read_execution_output", description="分页读取已执行命令保留的输出，不重新执行命令。",
        parameters={"type": "object", "properties": {"execution_id": {"type": "string"}, "channel": {"type": "string", "enum": ["stdout", "stderr"]},
            "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 16000}}, "required": ["execution_id"], "additionalProperties": False}, handler=read_output))
    async def _dispatch(operation: str, args: dict[str, Any], ctx: TurnContext) -> str:
        settings = _settings(ctx)
        if settings is None:
            return '{"ok": false, "error": "execution settings missing"}'
        store = ctx.extras.get("execution_store")
        execution_id = ctx.extras.get("execution_id")
        if store is None or execution_id is None:
            return '{"ok": false, "error": "durable execution context missing; workload not started"}'
        snapshots = ctx.extras.get("snapshots") if settings.execution_snapshots_enabled else None
        if settings.execution_snapshots_enabled and snapshots is None:
            return '{"ok": false, "error": "file checkpoint context unavailable; workload not started"}'
        async def request_approval(identifier, digest, target, permission_scope=None):
            from server.core.agent.state import RunPhase
            coordinator = ctx.extras.get("approvals")
            if coordinator is None or ctx.agent_run is None or ctx.emit is None:
                raise PermissionError("interactive approval channel unavailable; workload not started")
            review = json.dumps(args, ensure_ascii=False)
            if len(review) > 16000:
                raise PermissionError("request exceeds interactive review budget; split the operation")
            if ctx.llm_api_key:
                review = review.replace(ctx.llm_api_key, "[redacted]")
            async def publish(approval_id):
                ctx.agent_run.enter(RunPhase.AWAITING_APPROVAL)
                await ctx.save_checkpoint("approval_required")
                await ctx.publish("approval_required", {
                    "approval_id": approval_id, "execution_id": identifier,
                    "target": target, "operation": operation, "request": review,
                    "scope": permission_scope or {"host_access": False, "extra_permissions": False},
                })
            try:
                return await coordinator.wait(execution_id=identifier, session_id=ctx.session_id,
                                              digest=digest, target=target, publish=publish)
            finally:
                if ctx.agent_run.outcome is None:
                    ctx.agent_run.enter(RunPhase.EXECUTING_TOOLS)
        return await execute(settings, ctx.session_id, operation, args, store=store,
                             execution_id=execution_id,
                             run_id=ctx.agent_run.run_id if ctx.agent_run else None,
                             redact=(ctx.llm_api_key,), request_approval=request_approval, snapshots=snapshots)

    async def _run_shell(args, ctx):
        return await _dispatch("run_shell", args, ctx)

    async def _run_python(args, ctx):
        return await _dispatch("run_python", args, ctx)

    async def _read_file(args, ctx):
        return await _dispatch("read_file", args, ctx)

    async def _write_file(args, ctx):
        return await _dispatch("write_file", args, ctx)

    async def _list_dir(args, ctx):
        return await _dispatch("list_dir", args, ctx)

    registry.register(
        ToolSpec(
            name="run_shell",
            description="在隔离工作区执行命令（Windows 原生默认 Windows PowerShell 5.1，不支持 &&/||，请用分号及退出码判断；配置 cmd 时使用 cmd；Linux/容器为 Bash）；沙盒不可用时拒绝执行。",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            handler=_run_shell,
        )
    )
    registry.register(
        ToolSpec(
            name="run_python",
            description="在当前会话 workspace 中运行一段 Python 代码。",
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python 源码"},
                },
                "required": ["code"],
                "additionalProperties": False,
            },
            handler=_run_python,
        )
    )
    registry.register(
        ToolSpec(
            name="read_file",
            description="读取会话 workspace 内文件（Skills 路径也可用此工具打开）。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对 workspace 的路径"},
                    "offset": {"type": "integer", "minimum": 0, "maximum": 8000000},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100000},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=_read_file,
        )
    )
    registry.register(
        ToolSpec(
            name="write_file",
            description="写入会话 workspace 内文件（自动创建父目录）。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=_write_file,
        )
    )
    registry.register(
        ToolSpec(
            name="list_dir",
            description="列出会话 workspace 目录内容。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对路径，默认 .",
                    },
                },
                "additionalProperties": False,
            },
            handler=_list_dir,
        )
    )
    from server.core.execution.permissions import SCHEMA
    for name in ("run_shell", "run_python", "read_file", "write_file", "list_dir"):
        registry.get(name).parameters["properties"]["permissions"] = SCHEMA
