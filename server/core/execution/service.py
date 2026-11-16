"""Run bounded computer operations through an explicitly selected sandbox."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import subprocess
import uuid
import hashlib
import logging
from copy import deepcopy
from pathlib import Path

from server.core.execution.sandbox import SandboxUnavailable, build_launch
from server.core.execution.permissions import resolve
from server.core.agent.storage_budget import StorageBudgetExceeded


# This program runs inside the execution boundary, including filesystem operations.
FILE_PROGRAM = r'''
import json, pathlib, sys
request = json.load(sys.stdin)
root = pathlib.Path('/workspace').resolve()
READ_PATHS = []
op = request['operation']
target = (root / request.get('path', '.')).resolve()
if not target.is_relative_to(root) and not (op in ('read_file', 'list_dir') and any(target == pathlib.Path(p) or target.is_relative_to(pathlib.Path(p)) for p in READ_PATHS)):
    raise ValueError('path escapes workspace')
if target.is_relative_to(root) and any(part in ('.git', '.agents', '.codex') for part in target.relative_to(root).parts):
    raise ValueError('protected metadata path')
op = request['operation']
if op == 'read_file':
    offset, limit = request.get('offset', 0), request.get('limit', 100000)
    if type(offset) is not int or not 0 <= offset <= 8000000 or type(limit) is not int or not 1 <= limit <= 100000:
        raise ValueError('invalid file page')
    with target.open(encoding='utf-8', errors='replace') as f:
        f.read(offset)
        text = f.read(limit + 1)
    result = dict(ok=True, path=str(target), content=text[:limit], offset=offset,
                  next_offset=offset + limit if len(text)>limit else None, truncated=len(text)>limit)
elif op == 'write_file':
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(request.get('content', ''), encoding='utf-8')
    result = dict(ok=True, path=str(target), bytes=target.stat().st_size)
elif op == 'list_dir':
    import itertools
    entries = []
    for p in itertools.islice(target.iterdir(), 200):
        entries.append(dict(name=p.name, type='dir' if p.is_dir() else 'file'))
    result = dict(ok=True, path=str(target), entries=entries)
else:
    raise ValueError('unknown file operation')
print(json.dumps(result, ensure_ascii=False))
'''


async def execute(settings, session_id: str, operation: str, arguments: dict, *,
                  execution_id: str | None = None, store=None, run_id: str | None = None,
                  redact: tuple[str, ...] = (), request_approval=None, snapshots=None) -> str:
    settings = deepcopy(settings)
    arguments = deepcopy(arguments)
    execution_id = execution_id or uuid.uuid4().hex
    try:
        permissions = resolve(settings, arguments, operation)
    except (OSError, ValueError) as exc:
        return json.dumps({"execution_id": execution_id, "ok": False, "status": "denied",
                           "reason": "permission_denied", "error": str(exc)})
    confirmation = settings.execution_require_approval or permissions.elevated
    if confirmation and (store is None or request_approval is None):
        return json.dumps({"execution_id": execution_id, "ok": False, "status": "denied",
                           "reason": "approval_unavailable", "error": "approval context required; workload not started"})
    if store is None:
        return await _execute(settings, session_id, operation, arguments, execution_id=execution_id)
    fingerprint = json.dumps({
        "session_id": session_id, "run_id": run_id, "operation": operation, "arguments": arguments,
        "location": settings.execution_location, "backend": settings.execution_backend,
        "workspace": str(Path(settings.workspaces_dir).resolve()), "shell": settings.computer_shell,
        "image": settings.execution_container_image, "runtime": settings.execution_windows_runtime,
        "timeout": settings.computer_timeout_seconds, "enabled": settings.computer_enabled,
        "policy_version": 2,
        "require_approval": settings.execution_require_approval,
        "snapshots": snapshots is not None,
        "permissions": permissions.review(),
        "read_allowlist": settings.execution_read_allowlist,
        "allow_public_network": settings.execution_allow_public_network,
        "allow_host": settings.execution_allow_host,
    }, sort_keys=True, ensure_ascii=False)
    base = {"execution_id": execution_id, "location": settings.execution_location, "ok": False}
    try:
        claimed = await store.claim(execution_id, session_id=session_id, run_id=run_id,
                                    operation=operation, digest=hashlib.sha256(fingerprint.encode()).hexdigest(),
                                    location=settings.execution_location, backend=settings.execution_backend)
        if not claimed:
            previous = await store.read(execution_id, include_result=True)
            if previous and previous.get("result") is not None:
                return previous["result"]
            status = (previous or {}).get("status", "result_unknown")
            return json.dumps({**base, "status": status, "reason": "execution_not_replayed",
                               "error": "execution is in progress or has an unknown result; not replayed"})
    except StorageBudgetExceeded as exc:
        return json.dumps({**base, "status": "denied", "reason": "runtime_storage_budget_exhausted", "error": str(exc)})
    except ValueError as exc:
        return json.dumps({**base, "status": "denied", "reason": "execution_conflict", "error": str(exc)})
    except Exception:
        logging.getLogger(__name__).exception("Execution claim could not be persisted")
        return json.dumps({**base, "status": "denied", "reason": "execution_store_unavailable",
                           "error": "execution could not be recorded; no workload was started"})
    try:
        approval_id = None
        if confirmation:
            try:
                options = {"permission_scope": permissions.review()} if permissions.elevated else {}
                approval_id = await request_approval(execution_id, hashlib.sha256(fingerprint.encode()).hexdigest(), settings.execution_location, **options)
                if not isinstance(approval_id, str) or not approval_id:
                    raise PermissionError("approval callback did not provide a single-use grant")
            except PermissionError as exc:
                denied = json.dumps({**base, "status": "denied", "reason": "approval_denied", "error": str(exc)})
                await store.finish(execution_id, denied)
                return denied
        async def start_and_execute():
            if approval_id is None:
                await store.start(execution_id)
            else:
                await store.start(execution_id, approval_id=approval_id)
            options = {"permissions": permissions} if permissions.elevated else {}
            task = asyncio.create_task(_execute(settings, session_id, operation, arguments, execution_id=execution_id, **options))
            store.workloads[execution_id] = task
            try:
                return await task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                return json.dumps({**base, "status": "result_unknown", "reason": "execution_cancelled",
                                   "error": "execution cancelled; effects and child exit must be reconciled before retry"})
            finally:
                store.workloads.pop(execution_id, None)
        if snapshots is not None and operation in ("run_shell", "run_python", "write_file"):
            from server.core.computer.workspace import workspace_root
            root = workspace_root(settings.workspaces_dir, session_id)
            async with snapshots.lock(root):
                from server.core.execution.snapshots import SnapshotError
                try:
                    baseline = await asyncio.to_thread(snapshots.begin, root, execution_id, session_id, run_id)
                except (SnapshotError, OSError) as exc:
                    denied = json.dumps({**base, "status": "denied", "reason": "file_checkpoint_unavailable",
                                         "error": "workload not started: " + str(exc)})
                    await store.finish(execution_id, denied)
                    return denied
                try:
                    result = await start_and_execute()
                finally:
                    cancellation = sys.exc_info()[0] is asyncio.CancelledError
                    try:
                        await asyncio.shield(asyncio.to_thread(snapshots.finish, baseline))
                    except Exception:
                        if not cancellation:
                            raise
                        logging.getLogger(__name__).exception("Post-cancellation file checkpoint unavailable")
                payload = json.loads(result)
                payload["checkpoint_id"] = execution_id
                payload["workspace_changed"] = baseline["before"]["files"] != baseline["after"]["files"]
                payload["checkpoint_coverage"] = ("workspace_only_host_execution" if permissions.host_access else
                    "partial" if baseline["before"]["excluded"] or baseline["after"]["excluded"] else "covered_workspace_files")
                result = json.dumps(payload, ensure_ascii=False)
        else:
            result = await start_and_execute()
        def scrub(value):
            if isinstance(value, str):
                for secret in redact:
                    if secret:
                        value = value.replace(secret, "[redacted]")
                return value
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value
        payload = scrub(json.loads(result))
        output = payload.pop("_output", None)
        if output is not None:
            payload["stdout"], payload["stderr"] = output["stdout"][:12000], output["stderr"][:12000]
            from server.core.execution.artifacts import OutputStore
            try:
                await asyncio.to_thread(OutputStore(settings.execution_output_dir, settings.execution_output_storage_bytes).put,
                                        execution_id, session_id, output)
                payload["output_available"] = True
                payload["output_hint"] = "Read retained output with read_execution_output; do not rerun the command."
            except (OSError, ValueError):
                payload["output_available"] = False
                payload["output_storage_error"] = "output could not be retained; do not rerun side effects to recover output"
                payload.update(ok=False, status="failed", reason="output_storage_unavailable")
        persisted = json.dumps(payload, ensure_ascii=False)
        await store.finish(execution_id, persisted)
        return persisted
    except asyncio.CancelledError:
        try:
            await asyncio.shield(store.finish(execution_id, json.dumps({
                **base, "status": "result_unknown", "reason": "execution_cancelled",
                "error": "execution interrupted; cancellation is not rollback",
            })))
        except Exception:
            logging.getLogger(__name__).exception("Interrupted execution status could not be persisted")
        raise
    except Exception:
        logging.getLogger(__name__).exception("Execution result could not be committed")
        return json.dumps({**base, "status": "result_unknown", "reason": "execution_result_uncommitted",
                           "error": "execution result could not be confirmed; do not replay"})
    finally:
        store.active.discard(execution_id)


async def _execute(settings, session_id: str, operation: str, arguments: dict, *, execution_id: str, permissions=None) -> str:
    from server.core.computer.policy import check_shell_command
    from server.core.computer.workspace import workspace_root
    base = {"execution_id": execution_id, "location": settings.execution_location}
    if not settings.computer_enabled:
        return json.dumps({**base, "ok": False, "error": "computer tools disabled"})
    windows_native = (sys.platform == "win32" and settings.execution_location == "local"
                      and settings.execution_backend in ("auto", "native"))
    python = sys.executable if windows_native else "python3"
    if operation == "run_shell":
        command = str(arguments.get("command") or "")
        denied = check_shell_command(command)
        if denied:
            return json.dumps({**base, "ok": False, "error": denied})
        argv, data = ["/bin/bash", "-c", command], None
        if windows_native:
            system = os.environ.get("SystemRoot")
            if not system:
                return json.dumps({**base, "ok": False, "error": "Windows system directory unavailable"})
            if settings.computer_shell == "cmd":
                argv = [str(Path(system) / "System32/cmd.exe"), "/d", "/s", "/c", command]
            elif settings.computer_shell == "powershell":
                argv = [str(Path(system) / "System32/WindowsPowerShell/v1.0/powershell.exe"),
                        "-NoProfile", "-NonInteractive", "-Command", command]
            else:
                return json.dumps({**base, "ok": False, "error": "native Windows supports powershell or cmd"})
    elif operation == "run_python":
        code = str(arguments.get("code") or "")
        if not code.strip():
            return json.dumps({**base, "ok": False, "error": "empty code"})
        # Isolated Python omits cwd from sys.path. Restore only the sandbox's
        # working directory so local modules work without inheriting host paths.
        program = ("import sys, os; sys.path.insert(0, os.getcwd());\n"
                   + "exec(compile(" + repr(code) + ", '<velora-python>', 'exec'))")
        argv, data = [python, "-I", "-c", program], None
    elif operation in ("read_file", "write_file", "list_dir"):
        program = FILE_PROGRAM.replace("pathlib.Path('/workspace')", "pathlib.Path.cwd()") if windows_native else FILE_PROGRAM
        if permissions:
            program = program.replace("READ_PATHS = []", "READ_PATHS = " + repr(list(permissions.read_paths)))
        argv = [python, "-I", "-c", program]
        data = json.dumps({**arguments, "operation": operation}).encode()
    else:
        return json.dumps({**base, "ok": False, "error": "unsupported operation"})
    try:
        root = workspace_root(settings.workspaces_dir, session_id)
        launch = build_launch(settings, root, argv, permissions=permissions)
    except (SandboxUnavailable, ValueError, OSError) as exc:
        return json.dumps({**base, "ok": False, "status": "denied",
                           "reason": "sandbox_unavailable", "error": str(exc)})
    base["backend"] = launch.backend
    limit = 650000 if operation in ("read_file", "write_file", "list_dir") else settings.execution_output_capture_bytes
    async def drain(stream):
        kept, total = bytearray(), 0
        while chunk := await stream.read(8192):
            total += len(chunk)
            kept.extend(chunk[:max(0, limit - len(kept))])
        return kept.decode("utf-8", errors="replace"), total > limit

    proc = None
    async def cleanup():
        confirmed = launch.backend != "host-approved"
        if launch.container_name:
            # Killing the client alone does not stop the daemon-owned workload.
            try:
                remover = await asyncio.create_subprocess_exec(
                    launch.argv[0], "rm", "--force", launch.container_name,
                    env=launch.env, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                try:
                    await asyncio.wait_for(remover.wait(), 10)
                    confirmed = remover.returncode == 0
                except asyncio.TimeoutError:
                    remover.kill()
                    await remover.wait()
                    confirmed = False
            except OSError:
                confirmed = False
        if proc and proc.returncode is None:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        return confirmed

    tasks = []
    try:
        if launch.probe_argv:
            from server.core.execution.windows import validate_probe
            probe = await asyncio.create_subprocess_exec(
                *launch.probe_argv, cwd=launch.cwd, env=launch.env,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                output, errors = await asyncio.wait_for(probe.communicate(), 15)
                if probe.returncode != 0:
                    raise SandboxUnavailable("native Windows capability probe failed: " + errors[:1000].decode(errors="replace"))
                validate_probe(json.loads(output))
            except (ValueError, SandboxUnavailable, asyncio.TimeoutError) as exc:
                return json.dumps({**base, "ok": False, "status": "denied",
                                   "reason": "sandbox_unavailable", "error": str(exc) or "capability probe timed out"})
            finally:
                if probe.returncode is None:
                    probe.kill()
                    await probe.wait()
        proc = await asyncio.create_subprocess_exec(
            *launch.argv, cwd=launch.cwd, env=launch.env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=os.name == "posix",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        async def feed():
            try:
                if data:
                    proc.stdin.write(data)
                    await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()
        tasks = [asyncio.create_task(drain(proc.stdout)), asyncio.create_task(drain(proc.stderr)),
                 asyncio.create_task(feed()), asyncio.create_task(proc.wait())]
        try:
            results = await asyncio.wait_for(asyncio.gather(*tasks), settings.computer_timeout_seconds)
        except asyncio.TimeoutError:
            stopped = await cleanup()
            return json.dumps({**base, "ok": False, "status": "failed" if stopped else "result_unknown",
                               "reason": "execution_timeout", "error": "execution timed out"})
        stdout, stderr = results[0], results[1]
        if operation in ("read_file", "write_file", "list_dir") and proc.returncode == 0:
            if stdout[1]:
                return json.dumps({**base, "ok": False, "error": "file response exceeds output budget"})
            return json.dumps({**json.loads(stdout[0]), **base}, ensure_ascii=False)
        result = {**base, "ok": proc.returncode == 0, "status": "succeeded" if proc.returncode == 0 else "failed",
                  "exit_code": proc.returncode, "stdout": stdout[0][:12000], "stderr": stderr[0][:12000],
                  "stdout_truncated": stdout[1] or len(stdout[0]) > 12000, "stderr_truncated": stderr[1] or len(stderr[0]) > 12000}
        if result["stdout_truncated"] or result["stderr_truncated"]:
            result["_output"] = {"stdout": stdout[0], "stderr": stderr[0], "stdout_truncated": stdout[1], "stderr_truncated": stderr[1]}
        return json.dumps(result, ensure_ascii=False)
    except asyncio.CancelledError:
        await asyncio.shield(cleanup())
        raise
    except (OSError, ValueError) as exc:
        return json.dumps({**base, "ok": False, "status": "failed", "error": str(exc)})
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if launch.policy_file:
            launch.policy_file.unlink(missing_ok=True)
