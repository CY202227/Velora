"""Interactive terminal client for the local Agent service."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import httpx


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "velora_data" / "terminal.json"


def clean(value: object) -> str:
    text = str(value)
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32 and ord(c) != 127)


def error_message(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            exc.response.read()
            body = exc.response.json()
            detail = body.get("detail") or body.get("result", {}).get("message")
            if isinstance(detail, str):
                return clean(detail[:1000])
        except (ValueError, httpx.HTTPError):
            pass
    return clean(str(exc)[:1000]) if isinstance(exc, ValueError) else type(exc).__name__


def events(lines):
    """Decode complete SSE frames, including multiline data and comments."""
    kind, data = "message", []
    for line in lines:
        if not line:
            if data:
                yield kind, json.loads("\n".join(data))
            kind, data = "message", []
        elif line.startswith("event:"):
            kind = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield kind, json.loads("\n".join(data))


class Terminal:
    def __init__(self, client: httpx.Client, url: str, *, color: bool = True, state_file: Path = STATE, token_file=None):
        self.client, self.url = client, url
        self.color, self.state_file = color, state_file
        self.session: str | None = None
        self.run_id: str | None = None
        self.task_id: str | None = None
        self.cursor = 0
        self.control_token = ""
        self.acceptance = []
        default_token = ROOT / "velora_data/local-control.token"
        if token_file is not None or (url.rstrip("/") in ("http://127.0.0.1:8030", "http://localhost:8030") and default_token.exists()):
            from server.core.control import read_token
            if str(client.base_url).rstrip("/") != url.rstrip("/"):
                raise ValueError("client origin does not match configured service")
            self.control_token = read_token(token_file or default_token)
            self.client.headers["X-Velora-Token"] = self.control_token

    def note(self, text: str):
        print(clean(text), file=sys.stderr, flush=True)

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    def remember(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"url": self.url, "session": self.session, "run_id": self.run_id,
                                         "task_id": self.task_id, "cursor": self.cursor}), encoding="utf-8")
        temporary.replace(self.state_file)

    def connect(self, session=None, persona=None, fresh=False):
        if not session and not fresh and not persona and self.state_file.exists():
            try:
                previous = json.loads(self.state_file.read_text(encoding="utf-8"))
                if previous.get("url") == self.url:
                    session = previous.get("session")
                    self.run_id = previous.get("run_id")
                    self.task_id = previous.get("task_id")
                    self.cursor = max(0, int(previous.get("cursor", 0)))
            except (ValueError, OSError):
                pass
        if session:
            try:
                self.request("GET", f"/api/sessions/{session}")
                self.session = session
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                self.note("上次会话不存在，将创建新会话。")
        if not self.session:
            self.session = self.request("POST", "/api/sessions", json={"persona_id": persona, "tts_enabled": False})["id"]
            self.run_id = None
            self.task_id, self.cursor = None, 0
        self.remember()

    def banner(self):
        heading = "\033[38;5;173mVelora\033[0m" if self.color else "Velora"
        print(f"\n  {heading}  /  个人 Agent 终端\n", flush=True)
        self.note(f"  服务  {self.url}\n  会话  {self.session}\n  /help 查看命令 · Ctrl+C 停止当前请求 · /exit 退出\n")

    def result(self, data):
        result = data.get("result") or {}
        self.run_id = result.get("run_id") or data.get("run_id")
        self.remember()
        status = result.get("status") or data.get("status") or ("failed" if data.get("stop_reason") else "completed")
        if status != "completed":
            self.note(result.get("message") or f"任务未完成：{data.get('stop_reason') or status}")
        if result.get("verification", {}).get("status") == "passed":
            self.note("指定验收条件已通过。")
        self.note(f"  [{status}]" + (f" · {result['step']} 步" if "step" in result else "") + (f" · run {self.run_id}" if self.run_id else ""))
        return status == "completed"

    def send(self, text, *, watch=False, resume_run_id=None):
        draft, terminal, failed = "", False, False
        request_task_id = self.task_id if watch else None
        print("\nVelora › ", end="", flush=True)
        try:
            method = "GET" if watch else "POST"
            path = f"/api/chat/tasks/{self.task_id}/events?after={self.cursor}" if watch else "/api/chat/stream"
            options = {} if watch else {"json": {"session_id": self.session, "text": text}}
            if not watch and self.acceptance:
                options["json"]["acceptance"] = self.acceptance
            if resume_run_id is not None:
                path = "/api/chat/resume/stream"
                options = {"json": {"run_id": resume_run_id, "text": text}}
            with self.client.stream(method, path, **options) as response:
                response.raise_for_status()
                for kind, data in events(response.iter_lines()):
                    if data.get("task_id"):
                        if kind == "task_started" and not watch and resume_run_id is None:
                            self.acceptance = []
                        if self.task_id != data["task_id"]:
                            self.cursor = 0
                        self.task_id = data["task_id"]
                        request_task_id = self.task_id
                        self.cursor = int(data.get("cursor", self.cursor))
                    if kind == "token":
                        piece = clean(data.get("text", ""))
                        draft += piece
                        print(piece, end="", flush=True)
                    elif kind == "assistant_draft_reset":
                        if draft:
                            print()
                            self.note("  · 上段为中间草稿，任务继续")
                        draft = ""
                    elif kind == "tool_call":
                        self.note(f"\n  → {data.get('name', 'tool')}")
                    elif kind == "tool_result":
                        self.note(f"  ✓ {data.get('name', 'tool')} 已返回（不代表任务已验收）")
                        if data.get("checkpoint_id"):
                            self.note(f"    文件检查点 {data['checkpoint_id']} · {data.get('checkpoint_coverage')}")
                    elif kind == "approval_required":
                        scope = data.get("scope", {})
                        host = isinstance(scope, dict) and scope.get("host_access") is True
                        self.note("\n  需要单次本机执行授权" if host else "\n  需要单次执行确认（仍在沙盒内）")
                        if host:
                            self.note("  将直接使用本机账户权限；文件和网络访问不受沙盒限制。检查点仅覆盖会话工作区，外部操作不能自动撤销。")
                        self.note(f"  目标：{data.get('target')} · 工具：{data.get('operation')}")
                        self.note("  权限范围：" + json.dumps(data.get("scope", {}), ensure_ascii=False))
                        self.note(str(data.get("request", "")))
                        accepted = False
                        if sys.stdin.isatty():
                            try:
                                accepted = input("允许本次执行？输入 yes 批准，其他输入拒绝：").strip().lower() == "yes"
                            except EOFError:
                                pass
                        try:
                            self.request("POST", f"/api/chat/approvals/{data['approval_id']}/decision",
                                         json={"session_id": self.session, "approve": accepted})
                        except httpx.HTTPStatusError as exc:
                            if exc.response.status_code != 409:
                                raise
                            self.note("审批已处理或失效，继续读取后续进度。")
                    elif kind == "task_interrupted":
                        failed = True
                        self.note(data.get("message", "任务进程不可确认，请检查记录。"))
                    elif kind == "message" and data.get("role") == "assistant":
                        answer = clean(data.get("content", ""))
                        if answer.strip() != draft.strip():
                            print("\n" + answer, end="", flush=True)
                    elif kind == "error":
                        failed = True
                        self.note("\n  ! " + str(data.get("message", "请求失败")))
                    elif kind == "turn_end":
                        if not data.get("task_id") and not watch:
                            self.task_id, self.cursor = None, 0
                        terminal = True
                        print()
                        failed = not self.result(data) or failed
                    self.remember()
            if not terminal:
                if watch and self.task_id:
                    snapshot = self.request("GET", f"/api/chat/tasks/{self.task_id}")
                    if snapshot.get("result"):
                        terminal = True
                        final = snapshot["result"]
                        if final.get("assistant_text"):
                            print(clean(final["assistant_text"]))
                        failed = not self.result(final) or failed
                if not terminal:
                    self.note("\n未收到结束确认，完成状态未知。请用 /status 查询或 /watch 重连，勿盲目重发外部操作。")
            return terminal and not failed
        except httpx.HTTPStatusError as exc:
            self.note("请求未完成：" + error_message(exc))
            return False
        except KeyboardInterrupt:
            self.note("\n正在停止请求…")
            try:
                if request_task_id:
                    self.request("POST", f"/api/chat/tasks/{request_task_id}/cancel")
                else:
                    self.request("POST", "/api/chat/cancel", json={"session_id": self.session})
                self.note("已发送取消；已发生的外部操作不会自动撤销。")
            except httpx.HTTPError:
                self.note("取消未确认，请用 /status 查询工具状态。")
            return False

    def command(self, text):
        command, _, argument = text.partition(" ")
        argument = argument.strip()
        if command in {"/exit", "/quit"}:
            return False
        if command == "/help":
            self.note("/new 新会话\n/sessions 列出会话\n/use <id> 切换会话\n/history 最近消息\n/status 当前运行状态\n/tasks 当前会话任务\n/expect <JSON数组> 设置下一任务验收\n/capabilities 查询执行能力\n/execution <ID> 执行状态\n/output <ID> [偏移] [流] 读取输出\n/cancel-execution <ID> 停止单次执行\n/reconcile <ID> 核对完成回执\n/changes [检查点] 查看文件改动\n/undo <检查点> 撤销文件改动\n/changes-run <运行ID> 查看整轮改动\n/undo-run <运行ID> 撤销整轮改动\n/watch [task-id] 重连任务\n/cancel 取消当前会话\n/resume <run-id> [补充输入] 恢复\n/exit 退出\n输入末尾用反斜杠续行。")
        elif command == "/new":
            self.acceptance = []
            self.session = None
            self.connect(fresh=True)
            self.note(f"新会话：{self.session}")
        elif command == "/sessions":
            for item in self.request("GET", "/api/sessions"):
                self.note(f"{item['id']}  {item.get('persona_name', item.get('persona_id', ''))}")
        elif command == "/use" and argument:
            self.acceptance = []
            self.request("GET", f"/api/sessions/{argument}")
            self.session, self.run_id = argument, None
            self.task_id, self.cursor = None, 0
            self.remember()
            self.note(f"当前会话：{argument}")
        elif command == "/history":
            for item in self.request("GET", f"/api/sessions/{self.session}/turns"):
                print(f"\n{clean(item['role'])} › {clean(item['content'])}")
        elif command == "/status":
            if self.task_id:
                self.note(json.dumps(self.request("GET", f"/api/chat/tasks/{self.task_id}"), ensure_ascii=False, indent=2))
                return True
            data = self.request("GET", "/api/debug/runtime")
            matches = [item for item in data.get("active", []) + data.get("recent", []) if item['session_id'] == self.session]
            self.note(json.dumps(matches[:1], ensure_ascii=False, indent=2) if matches else "本服务进程暂无当前会话记录。")
        elif command == "/cancel":
            path = f"/api/chat/tasks/{self.task_id}/cancel" if self.task_id else "/api/chat/cancel"
            self.note(json.dumps(self.request("POST", path, json={"session_id": self.session}), ensure_ascii=False))
        elif command == "/tasks":
            self.note(json.dumps(self.request("GET", "/api/chat/tasks", params={"session_id": self.session}), ensure_ascii=False, indent=2))
        elif command == "/expect":
            from server.core.agent.acceptance import AcceptanceCheck
            if argument == "clear":
                self.acceptance = []
            elif argument:
                values = json.loads(argument)
                if not isinstance(values, list) or len(values) > 20:
                    raise ValueError("验收条件必须是最多 20 项的 JSON 数组")
                self.acceptance = [AcceptanceCheck.model_validate(value).model_dump() for value in values]
            self.note("下一任务验收条件：" + json.dumps(self.acceptance, ensure_ascii=False))
        elif command == "/capabilities":
            self.note(json.dumps(self.request("GET", "/api/execution/capabilities"), ensure_ascii=False, indent=2))
        elif command in ("/execution", "/cancel-execution", "/reconcile") and argument:
            suffix = "/cancel" if command == "/cancel-execution" else "/reconcile" if command == "/reconcile" else ""
            self.note(json.dumps(self.request("POST" if suffix else "GET", f"/api/execution/{argument}" + suffix), ensure_ascii=False, indent=2))
        elif command == "/output" and argument:
            parts = argument.split()
            params = {"offset": int(parts[1]) if len(parts) > 1 else 0, "channel": parts[2] if len(parts) > 2 else "stdout"}
            page = self.request("GET", f"/api/execution/{parts[0]}/artifacts", params=params)
            self.note(page["content"])
            if page["next_offset"] is not None:
                self.note(f"继续读取：/output {parts[0]} {page['next_offset']} {params['channel']}")
            if page.get("capture_truncated"):
                self.note("原输出超过保留上限；此记录不能用于恢复未保留部分，请勿重跑有副作用的命令。")
        elif command in ("/changes", "/undo", "/changes-run", "/undo-run"):
            if not argument:
                self.note(json.dumps(self.request("GET", "/api/files/checkpoints", params={"session_id": self.session}), ensure_ascii=False, indent=2))
                return True
            base = f"/api/files/checkpoints/runs/{argument}" if command.endswith("-run") else f"/api/files/checkpoints/{argument}"
            preview = self.request("GET", base + "/changes", params={"session_id": self.session})
            for item in preview["changes"]:
                self.note(f"{item['kind']}: {item['path']}" + (" · 后续改动冲突" if item['conflict'] else ""))
                if item.get("diff"):
                    self.note(item["diff"])
            if preview.get("excluded"):
                self.note("以下文件或目录不在撤销覆盖内：" + json.dumps(preview["excluded"], ensure_ascii=False))
            if command in ("/undo", "/undo-run"):
                if preview["conflicts"]:
                    self.note("存在后续修改，拒绝自动覆盖。")
                    return True
                accepted = False
                if sys.stdin.isatty():
                    try:
                        accepted = input("撤销上述文件改动？新增文件会移除，输入 yes 确认：").strip().lower() == "yes"
                    except EOFError:
                        pass
                if accepted:
                    self.note(json.dumps(self.request("POST", base + "/restore", json={"session_id": self.session, "confirm": True}), ensure_ascii=False, indent=2))
                else:
                    self.note("未执行文件撤销。")
        elif command == "/watch":
            identifier = argument or self.task_id
            if not identifier:
                self.note("暂无任务 ID，可用 /tasks 查询当前会话记录。")
                return True
            snapshot = self.request("GET", f"/api/chat/tasks/{identifier}")
            if snapshot["session_id"] != self.session:
                self.note("任务不属于当前会话，请先切换会话。")
                return True
            if identifier != self.task_id:
                self.cursor = 0
            self.task_id = identifier
            self.send("", watch=True)
        elif command == "/resume" and argument:
            run_id, _, feedback = argument.partition(" ")
            checkpoint = self.request("GET", f"/api/debug/runtime/checkpoints/{run_id}")
            if checkpoint["session_id"] != self.session:
                self.note("检查点不属于当前会话，请先用 /use 切换到对应会话。")
                return True
            self.note("正在从已验证边界恢复；Ctrl+C 可取消，断线后可 /watch 重连。")
            self.send(feedback, resume_run_id=run_id)
        else:
            self.note("命令无效，输入 /help 查看用法。")
        return True


def start_backend(url):
    """Start only the default local service, and only on explicit request."""
    if url.rstrip("/") != "http://127.0.0.1:8030":
        raise ValueError("--start-server 仅支持默认本机地址；其他服务请单独启动。")
    with httpx.Client(timeout=2, trust_env=False) as client:
        try:
            if client.get(url + "/health").status_code == 200:
                return None
        except httpx.HTTPError:
            pass
    log_path = ROOT / "velora_data" / "terminal-server.log"
    log_path.parent.mkdir(exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-m", "server.main"], cwd=ROOT, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        with httpx.Client(timeout=2, trust_env=False) as client:
            for _ in range(60):
                if process.poll() is not None:
                    break
                try:
                    if client.get(url + "/health").status_code == 200:
                        return process
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
        raise RuntimeError(f"服务启动未成功，请查看 {log_path}")
    except BaseException:
        process.terminate()
        process.wait(timeout=10)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="?")
    parser.add_argument("--url", default="http://127.0.0.1:8030")
    parser.add_argument("--session")
    parser.add_argument("--persona")
    parser.add_argument("--new", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--start-server", action="store_true")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--show-access-token", action="store_true")
    args = parser.parse_args(argv)
    if args.session and (args.new or args.persona):
        parser.error("--session 不能与 --new/--persona 一起使用")
    process = None
    terminal = None
    try:
        if args.show_access_token:
            from server.core.control import read_token
            print(read_token(args.token_file or ROOT / "velora_data/local-control.token"))
            return 0
        if args.start_server:
            process = start_backend(args.url)
        with httpx.Client(base_url=args.url, timeout=httpx.Timeout(None, connect=5), trust_env=False) as client:
            terminal = Terminal(client, args.url, color=not args.no_color and sys.stdout.isatty(), token_file=args.token_file)
            terminal.connect(args.session, args.persona, args.new)
            terminal.banner()
            if args.text is not None:
                return 0 if terminal.send(args.text) else 1
            while True:
                try:
                    text = input("\n你 › ").strip()
                    while text.endswith("\\"):
                        text = text[:-1] + "\n" + input("  … ")
                    if not text:
                        continue
                    if text.startswith("/"):
                        if not terminal.command(text):
                            break
                    else:
                        terminal.send(text)
                except KeyboardInterrupt:
                    terminal.note("\n输入已取消；/exit 退出。")
                except EOFError:
                    break
                except (httpx.HTTPError, ValueError, OSError) as exc:
                    terminal.note(f"操作失败：{error_message(exc)}；请检查服务或使用 /status。")
        return 0
    except (httpx.HTTPError, ValueError, RuntimeError, OSError) as exc:
        print(clean(f"无法连接 Velora：{type(exc).__name__}。先启动 python -m server.main，或使用 --start-server。"), file=sys.stderr)
        return 1
    finally:
        if process is not None:
            keep_running = False
            if terminal is not None and terminal.task_id and process.poll() is None:
                try:
                    with httpx.Client(base_url=args.url, timeout=3, trust_env=False, headers={"X-Velora-Token": terminal.control_token} if terminal.control_token else {}) as probe:
                        snapshot = probe.get(f"/api/chat/tasks/{terminal.task_id}")
                        snapshot.raise_for_status()
                        keep_running = snapshot.json().get("status") == "running"
                except (httpx.HTTPError, ValueError):
                    keep_running = True
            if keep_running:
                terminal.note("任务尚在运行或状态未确认，保留本次启动的后台服务；重新连接后可用 /watch 或 /cancel。")
            else:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
