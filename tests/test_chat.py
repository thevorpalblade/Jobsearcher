import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_assessment, make_job
from test_web import web  # noqa: F401  (the `web` fixture)

from jobsearcher import chat
from jobsearcher.config import ChatConfig
from jobsearcher.models import ApplicationState
from jobsearcher.store import Store

HX = {"HX-Request": "true"}


def stream_line(**event):
    return json.dumps(event) + "\n"


def text(message):
    return stream_line(type="assistant", message={"content": [{"type": "text", "text": message}]})


def tool(name, **args):
    block = {"type": "tool_use", "name": name, "input": args}
    return stream_line(type="assistant", message={"content": [block]})


def result(error=None, cost=0.02):
    return stream_line(
        type="result", is_error=error is not None, result=error or "ok", total_cost_usd=cost
    )


# --- parsing -----------------------------------------------------------------------


def test_parse_event_text_tools_and_results():
    assert chat.parse_event(text("  Hello  ")) == [{"type": "text", "text": "Hello"}]
    both = stream_line(
        type="assistant",
        message={
            "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/repo/cvs/master.md"}},
            ]
        },
    )
    assert chat.parse_event(both) == [
        {"type": "text", "text": "Let me look."},
        {"type": "tool", "text": "Reading master.md"},
    ]
    assert chat.parse_event(result()) == [{"type": "result", "error": None, "cost": 0.02}]
    assert chat.parse_event(result("Usage limit reached"))[0]["error"] == "Usage limit reached"
    # A sub-agent's messages, system events and noise are ignored.
    sub = stream_line(
        type="assistant",
        parent_tool_use_id="t1",
        message={"content": [{"type": "text", "text": "x"}]},
    )
    assert chat.parse_event(sub) == []
    assert chat.parse_event(stream_line(type="system", subtype="init")) == []
    assert chat.parse_event("not json") == []


def test_tool_summaries():
    assert (
        chat.tool_summary({"name": "Bash", "input": {"command": "pytest -q\n  -x"}})
        == "Running: pytest -q -x"
    )
    assert (
        chat.tool_summary({"name": "Edit", "input": {"file_path": "/a/b/ranker.py"}})
        == "Editing ranker.py"
    )
    assert (
        chat.tool_summary({"name": "Grep", "input": {"pattern": "swedish"}})
        == "Searching for swedish"
    )
    assert (
        chat.tool_summary({"name": "Agent", "input": {"description": "plan it"}}) == "Agent plan it"
    )
    long = chat.tool_summary({"name": "Bash", "input": {"command": "x" * 300}})
    assert len(long) < 110 and long.endswith("…")


def test_host_check_blocks_public_names_only():
    ok = chat.host_allowed
    for host in ("127.0.0.1:8080", "192.168.1.20", "localhost:8080", "[::1]:8080", "jobserver:8080",
                 "box.local", "box.lan:8080", "host.tailnet.ts.net", ""):  # fmt: skip
        assert ok(host, []), host
    for host in ("evil.example.com", "evil.example.com:8080", "1.2.3.4.evil.com"):
        assert not ok(host, []), host
    assert ok("jobs.example.com", ["Jobs.Example.com"]) and ok("anything.com", ["*"])


# --- the manager, with a fake Claude Code process --------------------------------------


class FakeProc:
    """Stands in for `claude -p`: writes `lines` to stdout, optionally waiting on `gate`."""

    def __init__(self, lines, code=0, gate=None):
        self.lines, self.code, self.gate = lines, code, gate
        self.stdin = self
        self.stdout = self._read()
        self.prompt = ""
        self.terminated = False

    def write(self, data):
        self.prompt += data

    def close(self):
        pass

    def _read(self):
        for line in self.lines:
            if self.gate and not self.gate.is_set():
                self.gate.wait(5)
            if self.terminated:
                return
            yield line

    def wait(self):
        return self.code

    def terminate(self):
        self.terminated = True
        if self.gate:
            self.gate.set()

    kill = terminate


class Launcher:
    def __init__(self, *procs):
        self.procs, self.calls = list(procs), []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return self.procs.pop(0)


def wait_done(run, timeout=5):
    deadline = time.time() + timeout
    while not run.done and time.time() < deadline:
        time.sleep(0.01)
    assert run.done


@pytest.fixture
def manager(tmp_path):
    Store(tmp_path / "chat.db").close()
    config = ChatConfig(enabled=True, workdir=tmp_path, model="opus", effort="medium")
    manager = chat.ChatManager(config, tmp_path / "chat.db", "Jenny", executable="sh")
    return manager


def messages(manager, chat_id):
    store = Store(manager.db_path)
    try:
        return [(m["role"], m["text"]) for m in store.chat_messages(chat_id)]
    finally:
        store.close()


def test_a_message_runs_claude_and_stores_the_reply(manager, tmp_path):
    proc = FakeProc(
        [text("Hi Jenny!"), tool("Read", file_path="/r/CLAUDE.md"), text("Done."), result()]
    )
    manager._popen = Launcher(proc)
    chat_id, run = manager.start(None, "  Hello  ")
    wait_done(run)

    assert [e["type"] for e in run.events] == ["text", "tool", "text", "done"]
    assert run.events[-1]["error"] is None and run.events[-1]["cost"] == 0.02
    assert messages(manager, chat_id) == [
        ("user", "Hello"), ("assistant", "Hi Jenny!"), ("tool", "Reading CLAUDE.md"), ("assistant", "Done."),
    ]  # fmt: skip
    assert proc.prompt == "Hello"  # the prompt goes in on stdin
    cmd, kwargs = manager._popen.calls[0]
    assert kwargs["cwd"] == tmp_path
    assert cmd[:4] == ["sh", "-p", "--output-format", "stream-json"]
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert cmd[cmd.index("--model") + 1] == "opus" and "--effort" in cmd
    assert "Jenny" in cmd[cmd.index("--append-system-prompt") + 1]
    first_id = cmd[cmd.index("--session-id") + 1]

    # The next message resumes the same Claude Code session.
    manager._popen = Launcher(FakeProc([text("Sure."), result()]))
    again, run2 = manager.start(chat_id, "And another thing")
    wait_done(run2)
    assert again == chat_id
    cmd2 = manager._popen.calls[0][0]
    assert cmd2[cmd2.index("--resume") + 1] == first_id and "--session-id" not in cmd2


def test_errors_are_reported_and_stored(manager):
    manager._popen = Launcher(FakeProc([result("You've hit your usage limit")]))
    chat_id, run = manager.start(None, "hi")
    wait_done(run)
    assert run.events[-1]["error"] == "You've hit your usage limit"
    assert messages(manager, chat_id)[-1] == ("error", "You've hit your usage limit")

    manager._popen = Launcher(FakeProc(["claude: not logged in\n"], code=1))
    _, run = manager.start(chat_id, "again")
    wait_done(run)
    assert run.events[-1]["error"] == "claude: not logged in"

    def boom(*args, **kwargs):
        raise FileNotFoundError("claude")

    manager._popen = boom
    _, run = manager.start(chat_id, "third")
    wait_done(run)
    assert "Couldn't start Claude Code" in run.events[-1]["error"]


def test_one_run_at_a_time_and_stop(manager):
    gate = threading.Event()
    proc = FakeProc([text("working"), result()], gate=gate)
    manager._popen = Launcher(proc)
    chat_id, run = manager.start(None, "long task")
    assert manager.busy()
    with pytest.raises(chat.ChatBusy):
        manager.start(None, "me too")
    assert manager.cancel(chat_id) is True
    wait_done(run)
    assert run.events[-1]["error"] == "Stopped."
    assert not manager.busy() and manager.cancel(chat_id) is False
    with pytest.raises(ValueError):
        manager.start(chat_id, "   ")


def test_unavailable_when_off_or_missing(manager, tmp_path):
    manager.config.enabled = False
    with pytest.raises(chat.ChatUnavailable, match="turned off"):
        manager.start(None, "hi")
    manager.config.enabled = True
    manager.executable = "no-such-claude-binary"
    assert "isn't installed" in manager.unavailable()
    manager.executable, manager.config.workdir = "sh", tmp_path / "missing"
    assert "isn't available" in manager.unavailable()


# --- routes and the dashboard ----------------------------------------------------------


def enable_chat(web, tmp_path, *procs):  # noqa: F811
    manager = web.client.app.state.web.chat
    manager.config.enabled, manager.config.workdir, manager.executable = True, tmp_path, "sh"
    manager._popen = Launcher(*procs)
    return manager


def test_dashboard_greets_and_lists_the_top_five(web):  # noqa: F811
    web.client.app.state.web.config.web.user_name = "Jenny"
    now = datetime.now(UTC)
    scores = [(1, 90), (2, 85), (3, 80), (4, 75), (5, 70), (6, 65), (7, 95)]
    for n, fit in scores:
        web.add(
            make_job(n, f"Role {n}", deadline=now + timedelta(days=9)), make_assessment(fit, fit)
        )
    web.store.set_application(make_job(7, "Role 7").id, ApplicationState.APPLIED, "")  # acted on
    web.store.set_application(
        make_job(2, "Role 2").id, ApplicationState.SHORTLISTED, ""
    )  # still counts
    page = web.client.get("/").text
    assert "Welcome, Jenny" in page and "Your top five" in page
    titles = [
        t
        for t in ("Role 1", "Role 2", "Role 3", "Role 4", "Role 5", "Role 6", "Role 7")
        if f">{t}<" in page
    ]
    assert titles == ["Role 1", "Role 2", "Role 3", "Role 4", "Role 5"]
    assert page.index(">Role 1<") < page.index(">Role 2<") < page.index(">Role 5<")
    assert "7 open jobs, 7 scored" in page and "in 9 days" in page


def test_empty_dashboard_and_no_name(web):  # noqa: F811
    page = web.client.get("/").text
    assert "<h1>Welcome</h1>" in page and "Nothing scored yet" in page
    assert "The chat isn't available" in page and "turned off" in page  # off by default


def test_send_stream_and_history(web, tmp_path):  # noqa: F811
    manager = enable_chat(
        web, tmp_path, FakeProc([text("Hello!"), tool("Bash", command="ls"), result()])
    )
    assert (
        web.client.post("/chat/send", data={"message": "hi"}).status_code == 403
    )  # needs HX-Request
    sent = web.client.post("/chat/send", data={"message": "hi"}, headers=HX)
    assert sent.status_code == 200
    chat_id = sent.json()["chat_id"]
    wait_done(manager.run_for(chat_id))

    stream = web.client.get(f"/chat/{chat_id}/stream?after=0")
    assert stream.headers["content-type"].startswith("text/event-stream")
    events = [
        json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")
    ]
    assert [e["type"] for e in events] == ["text", "tool", "done"]
    resumed = web.client.get(f"/chat/{chat_id}/stream?after=2").text  # reconnect: only what's new
    assert '"type": "done"' in resumed and "Hello!" not in resumed

    page = web.client.get(f"/?chat={chat_id}").text
    assert '"text": "Hello!"' in page and f'data-chat-id="{chat_id}"' in page
    assert "data-running" not in page  # finished
    assert 'value="new"' in web.client.get("/?chat=new").text


def test_send_errors_busy_and_host_guard(web, tmp_path):  # noqa: F811
    web.client.post("/settings/files/config/check", data={"text": ""}, headers=HX)  # warm up
    assert web.client.post("/chat/send", data={"message": "hi"}, headers=HX).status_code == 503
    gate = threading.Event()
    manager = enable_chat(web, tmp_path, FakeProc([result()], gate=gate))
    first = web.client.post("/chat/send", data={"message": "one"}, headers=HX)
    busy = web.client.post("/chat/send", data={"message": "two"}, headers=HX)
    assert (first.status_code, busy.status_code) == (200, 409) and "busy" in busy.json()["error"]
    assert web.client.post("/chat/send", data={"message": " "}, headers=HX).status_code in (
        409,
        422,
    )
    stopped = web.client.post(f"/chat/{first.json()['chat_id']}/cancel", headers=HX)
    assert stopped.json() == {"stopped": True}
    wait_done(manager.run_for(first.json()["chat_id"]))

    rebinding = {**HX, "Host": "evil.example.com"}
    assert (
        web.client.post("/chat/send", data={"message": "x"}, headers=rebinding).status_code == 403
    )
    assert (
        web.client.get("/chat/abc/stream", headers={"Host": "evil.example.com"}).status_code == 403
    )
    allowed = web.client.app.state.web.config.web
    allowed.allowed_hosts = ["evil.example.com"]
    assert (
        web.client.get("/chat/abc/stream", headers={"Host": "evil.example.com"}).status_code == 200
    )


def test_delete_a_conversation(web, tmp_path):  # noqa: F811
    manager = enable_chat(web, tmp_path, FakeProc([text("ok"), result()]))
    chat_id = web.client.post("/chat/send", data={"message": "hi"}, headers=HX).json()["chat_id"]
    wait_done(manager.run_for(chat_id))
    deleted = web.client.post(f"/chat/{chat_id}/delete", headers=HX)
    assert deleted.status_code == 204 and deleted.headers["HX-Redirect"] == "/?chat=new"
    assert web.store.get_chat(chat_id) is None and web.store.chat_messages(chat_id) == []


def test_chat_settings_reload_with_config(web):  # noqa: F811
    state = web.client.app.state.web
    text = "web:\n  user_name: Jenny\nchat:\n  enabled: true\n  model: sonnet\n"
    web.client.post("/settings/files/config", data={"text": text}, headers=HX)
    assert (state.chat.user_name, state.chat.config.model) == ("Jenny", "sonnet")
