"""Compatibility entry point for isolated execution."""
from server.config import Settings
from server.core.execution.service import execute

async def run_python(
    code: str, *, workspaces_dir: str, session_id: str,
    timeout: int = 60,
) -> str:
    settings = Settings(_env_file=None, computer_enabled=True,
                        workspaces_dir=workspaces_dir, computer_timeout_seconds=timeout)
    return await execute(settings, session_id, "run_python", {"code": code})
