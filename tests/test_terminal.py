import json

import httpx

from server.terminal import Terminal, clean, events


def stream(*frames):
    return "".join(f"event: {kind}\ndata: {json.dumps(data)}\n\n" for kind, data in frames)


def terminal(tmp_path, payload):
    client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(lambda req: httpx.Response(200, text=payload)))
    view = Terminal(client, "http://test", color=False, state_file=tmp_path / "terminal.json")
    view.session = "s"
    return view, client


def test_streamed_answer_is_not_printed_twice(tmp_path, capsys):
    view, client = terminal(tmp_path, stream(("token", {"text": "\nhello\n"}), ("message", {"role": "assistant", "content": "hello"}), ("turn_end", {"result": {"status": "completed", "run_id": "r", "step": 1}})))
    with client:
        assert view.send("test")
    assert capsys.readouterr().out.count("hello") == 1
    assert json.loads(view.state_file.read_text())["run_id"] == "r"


def test_budget_failure_is_visible_and_not_success(tmp_path, capsys):
    view, client = terminal(tmp_path, stream(("token", {"text": "draft"}), ("turn_end", {"result": {"status": "failed", "reason": "step_budget_exhausted", "message": "预算耗尽，未完成", "run_id": "r"}})))
    with client:
        assert not view.send("test")
    assert "预算耗尽，未完成" in capsys.readouterr().err


def test_connection_without_terminal_is_unknown(tmp_path, capsys):
    view, client = terminal(tmp_path, stream(("token", {"text": "unfinished"})))
    with client:
        assert not view.send("test")
    assert "完成状态未知" in capsys.readouterr().err


def test_sse_multiline_and_comments():
    assert list(events([": ping", "event: token", 'data: {"text":', 'data: "a"}', ""])) == [("token", {"text": "a"})]


def test_model_cannot_inject_terminal_control_sequences():
    assert clean("hello\x1b[2J\x1b]0;title\x07\x00") == "hello"


def test_connect_reuses_saved_session_without_creating_one(tmp_path):
    path = tmp_path / "terminal.json"
    path.write_text(json.dumps({"url": "http://test", "session": "saved", "run_id": "r"}))
    requests = []
    def handle(request):
        requests.append(request.method)
        return httpx.Response(200, json={"id": "saved"})
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client:
        view = Terminal(client, "http://test", state_file=path)
        view.connect()
        assert view.session == "saved"
    assert requests == ["GET"]


def test_terminal_requires_explicit_yes_for_single_execution(tmp_path, monkeypatch):
    decisions = []
    def handle(request):
        if "/approvals/" in request.url.path:
            decisions.append(json.loads(request.content))
            return httpx.Response(200, json={"decision": "approved"})
        return httpx.Response(200, text=stream(
            ("approval_required", {"approval_id": "a", "target": "local", "operation": "run_python", "request": '{"code":"print(1)"}'}),
            ("turn_end", {"result": {"status": "completed"}})))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "yes")
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client:
        view = Terminal(client, "http://test", state_file=tmp_path / "state.json")
        view.session = "s"
        assert view.send("task")
    assert decisions == [{"session_id": "s", "approve": True}]


def test_noninteractive_terminal_never_autoapproves(tmp_path, monkeypatch):
    decisions = []
    def handle(request):
        if "/approvals/" in request.url.path:
            decisions.append(json.loads(request.content))
            return httpx.Response(200, json={"decision": "denied"})
        return httpx.Response(200, text=stream(
            ("approval_required", {"approval_id": "a"}),
            ("turn_end", {"result": {"status": "completed"}})))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client:
        view = Terminal(client, "http://test", state_file=tmp_path / "state.json")
        view.session = "s"
        view.send("task")
    assert decisions == [{"session_id": "s", "approve": False}]


def test_terminal_saves_task_cursor_and_watch_does_not_resubmit(tmp_path):
    requests = []
    def handle(request):
        requests.append((request.method, request.url.path, request.url.query))
        if request.url.path.endswith("/events"):
            return httpx.Response(200, text=stream(("turn_end", {"task_id": "t", "cursor": 3, "result": {"status": "completed"}})))
        return httpx.Response(200, text=stream(("task_started", {"task_id": "t", "cursor": 1})))
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client:
        view = Terminal(client, "http://test", state_file=tmp_path / "state.json")
        view.session = "s"
        assert not view.send("task")
        assert json.loads(view.state_file.read_text())["cursor"] == 1
        assert view.send("", watch=True)
        assert view.cursor == 3
    assert requests == [("POST", "/api/chat/stream", b""), ("GET", "/api/chat/tasks/t/events", b"after=1")]
