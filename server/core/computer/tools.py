"""Register local computer tools into ToolRegistry."""

from __future__ import annotations

from typing import Any

from server.core.chain.context import TurnContext
from server.core.computer import fs as fs_ops
from server.core.computer.python_run import run_python
from server.core.computer.shell import run_shell
from server.core.tools.registry import ToolRegistry
from server.core.tools.types import ToolSpec


def _settings(ctx: TurnContext):
    return ctx.extras.get("settings")


def register_computer_tools(registry: ToolRegistry) -> None:
    async def _run_shell(args: dict[str, Any], ctx: TurnContext) -> str:
        s = _settings(ctx)
        if s is None or not getattr(s, "computer_enabled", True):
            return '{"ok": false, "error": "computer tools disabled"}'
        return await run_shell(
            str(args.get("command") or ""),
            workspaces_dir=s.workspaces_dir,
            session_id=ctx.session_id,
            shell=getattr(s, "computer_shell", "powershell"),
            timeout=int(getattr(s, "computer_timeout_seconds", 60)),
        )

    async def _run_python(args: dict[str, Any], ctx: TurnContext) -> str:
        s = _settings(ctx)
        if s is None or not getattr(s, "computer_enabled", True):
            return '{"ok": false, "error": "computer tools disabled"}'
        return await run_python(
            str(args.get("code") or ""),
            workspaces_dir=s.workspaces_dir,
            session_id=ctx.session_id,
            timeout=int(getattr(s, "computer_timeout_seconds", 60)),
        )

    async def _read_file(args: dict[str, Any], ctx: TurnContext) -> str:
        s = _settings(ctx)
        if s is None or not getattr(s, "computer_enabled", True):
            return '{"ok": false, "error": "computer tools disabled"}'
        skills = getattr(s, "skills_dir", None)
        return fs_ops.read_file(
            str(args.get("path") or ""),
            workspaces_dir=s.workspaces_dir,
            session_id=ctx.session_id,
            allow_abs=bool(getattr(s, "computer_allow_abs_paths", False)),
            extra_roots=[skills] if skills else None,
        )

    async def _write_file(args: dict[str, Any], ctx: TurnContext) -> str:
        s = _settings(ctx)
        if s is None or not getattr(s, "computer_enabled", True):
            return '{"ok": false, "error": "computer tools disabled"}'
        return fs_ops.write_file(
            str(args.get("path") or ""),
            str(args.get("content") if args.get("content") is not None else ""),
            workspaces_dir=s.workspaces_dir,
            session_id=ctx.session_id,
            allow_abs=bool(getattr(s, "computer_allow_abs_paths", False)),
        )

    async def _list_dir(args: dict[str, Any], ctx: TurnContext) -> str:
        s = _settings(ctx)
        if s is None or not getattr(s, "computer_enabled", True):
            return '{"ok": false, "error": "computer tools disabled"}'
        return fs_ops.list_dir(
            str(args.get("path") or "."),
            workspaces_dir=s.workspaces_dir,
            session_id=ctx.session_id,
            allow_abs=bool(getattr(s, "computer_allow_abs_paths", False)),
        )

    registry.register(
        ToolSpec(
            name="run_shell",
            description="在当前会话 workspace 中执行 shell 命令（有危险命令拦截与超时）。",
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
