"""A chat with Claude Code, run in this repository, for the web UI's dashboard.

Each user message starts one headless `claude -p` run (prompt on stdin, `cwd` = the
repo, `--output-format stream-json`), resuming the chat's Claude Code session so the
conversation continues. The run's events become chat messages, stored in SQLite and
streamed live to the browser. Only the official CLI is used, with the user's own login
or CLAUDE_CODE_OAUTH_TOKEN (see CLAUDE.md).

One run at a time across all chats: the subscription's usage limits are shared, and
two Claudes editing one checkout would step on each other.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobsearcher.config import ChatConfig
from jobsearcher.store import Store

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the assistant built into Jobsearcher's web UI, talking with {name}, who is using \
this personal job-search tool: it finds jobs, ranks them against her CV, and (soon) \
drafts applications. She isn't necessarily a programmer, so explain things plainly and \
briefly, and say what you did in terms of what changes for her.

What she may ask, and how to handle it:
- Explanations (how the tool works, why a job scored what it did): read the code, the \
CLAUDE.md and REMAINING_WORK.md files, and the database at data/jobsearcher.db \
(open it read-only, e.g. sqlite3 'file:data/jobsearcher.db?mode=ro').
- Drafts (cover letters, tailored CVs, notes): write them into data/drafts/ and tell her \
where they are. Ground every claim in cvs/master.md and the other CVs in cvs/; never \
invent experience, employers, dates or contacts. If something is missing, say so.
- Features and changes: follow CLAUDE.md, add tests, keep `pytest` and `ruff check .` \
passing, and commit with git. The daemon and web UI run from this checkout and only pick \
up code changes when restarted, so say what needs a restart instead of restarting them \
yourself, unless she asks.

Care: ask before changing config files, deleting data or CVs, or touching the daemon; \
don't run `jobsearcher rank`, `search` or `daemon` yourself while the daemon is running; \
never print or commit anything from .env or the personal gitignored files.
"""

# Suggested first questions on the dashboard.
SUGGESTIONS = [
    "What does this tool do, and what can I ask you?",
    "Why is my top job ranked first?",
    "Which of my top five jobs mention a contact person?",
    "Draft a cover letter for my top job, based on my CV.",
]


class ChatBusy(RuntimeError):
    pass


class ChatUnavailable(RuntimeError):
    pass


@dataclass
class Run:
    chat_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    cancelled: bool = False
    proc: subprocess.Popen[str] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.events.append(event)

    def since(self, index: int) -> tuple[list[dict[str, Any]], bool]:
        with self.lock:
            return self.events[index:], self.done


def tool_summary(block: dict[str, Any]) -> str:
    """One line for a tool call: "Reading cv.md", "Running: pytest -q", ..."""
    name, args = block.get("name", "tool"), block.get("input") or {}

    def short(value: Any, n: int = 90) -> str:
        text = " ".join(str(value).split())
        return text if len(text) <= n else text[: n - 1] + "…"

    verbs = {
        "Read": ("Reading", "file_path"),
        "Edit": ("Editing", "file_path"),
        "MultiEdit": ("Editing", "file_path"),
        "Write": ("Writing", "file_path"),
        "NotebookEdit": ("Editing", "notebook_path"),
        "Bash": ("Running:", "command"),
        "Grep": ("Searching for", "pattern"),
        "Glob": ("Looking for files", "pattern"),
        "WebFetch": ("Fetching", "url"),
        "WebSearch": ("Searching the web:", "query"),
    }
    if name in verbs:
        verb, key = verbs[name]
        value = args.get(key, "")
        if key.endswith("path") and value:
            value = Path(str(value)).name or value
        return f"{verb} {short(value)}".strip()
    detail = next((v for v in args.values() if isinstance(v, str)), "")
    return f"{name} {short(detail)}".strip()


def parse_event(line: str) -> list[dict[str, Any]]:
    """Chat events (text / tool / result) from one line of Claude Code's stream-json."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    kind = event.get("type")
    if kind == "assistant" and not event.get("parent_tool_use_id"):
        out = []
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                out.append({"type": "text", "text": block["text"].strip()})
            elif block.get("type") == "tool_use":
                out.append({"type": "tool", "text": tool_summary(block)})
        return out
    if kind == "result":
        return [
            {
                "type": "result",
                "error": (event.get("result") or "Claude Code reported an error.")
                if event.get("is_error")
                else None,
                "cost": event.get("total_cost_usd"),
            }
        ]
    return []


def available(workdir: Path, executable: str = "claude") -> str | None:
    """Why the chat can't run here, or None when it can."""
    if shutil.which(executable) is None:
        return "The Claude Code CLI (`claude`) isn't installed on this machine."
    if not workdir.is_dir():
        return (
            f"The repository folder {workdir} isn't available (is the web UI running in Docker?)."
        )
    return None


class ChatManager:
    def __init__(
        self,
        config: ChatConfig,
        db_path: Path,
        user_name: str = "",
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        executable: str = "claude",
    ):
        self.config = config
        self.db_path = db_path
        self.user_name = user_name
        self._popen = popen
        self.executable = executable
        self._lock = threading.Lock()
        self._runs: dict[str, Run] = {}
        self._active: Run | None = None

    @property
    def workdir(self) -> Path:
        return self.config.workdir or Path.cwd()

    def unavailable(self) -> str | None:
        if not self.config.enabled:
            return "The chat is turned off. Set chat.enabled: true in config.yaml to use it."
        return available(self.workdir, self.executable)

    def run_for(self, chat_id: str) -> Run | None:
        return self._runs.get(chat_id)

    def busy(self) -> bool:
        active = self._active
        return active is not None and not active.done

    def command(self, claude_session_id: str, started: bool) -> list[str]:
        cmd = [
            self.executable,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", self.config.permission_mode,
            "--model", self.config.model,
            "--append-system-prompt",
            SYSTEM_PROMPT.format(name=self.user_name or "the user"),
            "--resume" if started else "--session-id",
            claude_session_id,
        ]  # fmt: skip
        if self.config.effort:
            cmd += ["--effort", self.config.effort]
        return cmd

    def start(self, chat_id: str | None, prompt: str) -> tuple[str, Run]:
        """Send a message (creating the chat if needed) and start Claude Code on it."""
        problem = self.unavailable()
        if problem:
            raise ChatUnavailable(problem)
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("Write a message first.")
        with self._lock:
            if self.busy():
                raise ChatBusy("Claude is busy with another message; try again in a moment.")
            store = Store(self.db_path)
            try:
                chat = store.get_chat(chat_id) if chat_id else None
                if chat is None:
                    chat_id = uuid.uuid4().hex[:12]
                    store.create_chat(chat_id, str(uuid.uuid4()), _title(prompt))
                    chat = store.get_chat(chat_id)
                assert chat is not None and chat_id is not None
                store.add_chat_message(chat_id, "user", prompt)
                run = Run(chat_id)
                self._runs[chat_id] = run
                self._active = run
                cmd = self.command(chat["claude_session_id"], bool(chat["started"]))
            finally:
                store.close()
            threading.Thread(
                target=self._run, args=(run, cmd, prompt), name=f"chat-{chat_id}", daemon=True
            ).start()
        return chat_id, run

    def cancel(self, chat_id: str) -> bool:
        run = self._runs.get(chat_id)
        if run is None or run.done:
            return False
        run.cancelled = True  # also covers a run whose process hasn't started yet
        if run.proc is not None:
            run.proc.terminate()
        return True

    def _run(self, run: Run, cmd: list[str], prompt: str) -> None:
        store = Store(self.db_path)  # this thread's own connection
        error: str | None = None
        cost: float | None = None
        tail: list[str] = []
        timer: threading.Timer | None = None
        try:
            run.proc = self._popen(
                cmd,
                cwd=self.workdir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                env=os.environ.copy(),
            )
            assert run.proc.stdin and run.proc.stdout
            if run.cancelled:
                run.proc.terminate()
            run.proc.stdin.write(prompt)
            run.proc.stdin.close()
            timer = threading.Timer(self.config.timeout_s, run.proc.kill)
            timer.start()
            store.mark_chat_started(run.chat_id)  # a --session-id now exists
            for line in run.proc.stdout:
                events = parse_event(line)
                if not events and line.strip() and not line.lstrip().startswith("{"):
                    tail = (tail + [line.strip()])[-5:]  # error text from the CLI itself
                for event in events:
                    if event["type"] == "result":
                        error, cost = event["error"], event["cost"]
                    else:
                        role = "assistant" if event["type"] == "text" else "tool"
                        event["id"] = store.add_chat_message(run.chat_id, role, event["text"])
                        run.emit(event)
            code = run.proc.wait()
            if run.cancelled:
                error = "Stopped."
            elif error is None and code != 0:
                error = " ".join(tail) or f"Claude Code exited with status {code}."
        except OSError as exc:
            error = f"Couldn't start Claude Code: {exc}"
        except Exception as exc:  # the run thread must always finish the run
            log.exception("Chat run failed")
            error = f"Something went wrong: {exc}"
        finally:
            if timer:
                timer.cancel()
            done: dict[str, Any] = {"type": "done", "error": error, "cost": cost}
            if error:
                done["id"] = store.add_chat_message(run.chat_id, "error", error)
            store.close()
            run.emit(done)
            with run.lock:
                run.done = True


def _title(prompt: str) -> str:
    text = " ".join(prompt.split())
    return text if len(text) <= 48 else text[:47] + "…"


# Hosts that can't be reached through public DNS, so a DNS-rebinding page can't use
# them: IP addresses, localhost, one-word names, and the usual private suffixes.
_PRIVATE_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".ts.net")


def host_allowed(host_header: str, allowed: list[str]) -> bool:
    """Whether the chat accepts a request addressed to `host_header`. Guards against
    DNS rebinding (a web page making the browser reach this server under the
    attacker's own name), which matters because the UI has no login."""
    if "*" in allowed:
        return True
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8080
        return True
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if not host or "." not in host or host.replace(".", "").isdigit():
        return True  # localhost, a one-word name, or an IPv4 address
    return host.endswith(_PRIVATE_SUFFIXES) or host in {a.lower() for a in allowed}
