#!/usr/bin/env python3
"""Start or resume a read-only reviewer through the codex, claude or pi CLI.

The feature-loop skill calls this for its plan and code reviews. The reviewer
is picked from ``$XDG_CONFIG_HOME/feature-loop/reviewers`` (one
``<caller family> <backend:model>`` per line, ``*`` as the catch-all) or from
``FEATURE_LOOP_REVIEWER``. Nothing is shipped as a default, so code only goes
to a provider the user chose. Any reviewer is allowed, the caller's own
provider included; with ``--caller-model`` it warns when the reviewer is the
very model the main session runs.

Each backend runs with flags that make it read-only and MCP-free, built by one
function for both start and resume so a resume cannot drop a restriction:

- codex: OS sandbox (``sandbox_mode="read-only"``), user config ignored.
- claude: ``--restricted`` with only ``Read``, ``Grep`` and ``Glob``, no MCP
  servers. The init event is checked on every call and the run is killed if it
  shows anything else. It can read any file the user can, like the other two,
  unless ``FEATURE_LOOP_CLAUDE_CONFINED=1`` keeps it to the working directory
  and ``<dir>``.
- pi: read/grep/find/ls only, extensions, MCP and project files off.

Each call holds ``<dir>/locks/<session>.lock`` and passes the fd to the child,
so the lock outlives this script if the platform kills it and the reviewer
keeps running; a resume in that state exits 4 instead of starting a second
turn on the same session.

Exit codes: 0 ok, 1 failure, 2 config or usage error, 3 reviewer session not
found, 4 an earlier call on the session is still running.

Stdlib-only and Python 3.9-compatible, so plain ``python3`` runs it from an
installed skill directory with no uv environment; that is also why it uses
argparse rather than cyclopts.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 30 * 60
BACKENDS = ("codex", "claude", "pi")
EXIT_FAIL, EXIT_CONFIG, EXIT_GONE, EXIT_BUSY = 1, 2, 3, 4
TOKEN_PREFIX = "fl1."
# No leading `-`: the id is passed to the CLI as an argument.
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
EXAMPLE_CONFIG = """\
# main session's model family   reviewer as <backend:model>
claude    codex:gpt-6.1-sol
gpt       claude:claude-opus-5-5
*         claude:claude-opus-5-5"""
# What each CLI prints when asked to resume a session it does not have.
GONE_MESSAGES = {
    "claude": "No conversation found with session ID",
    "codex": "no rollout found for thread id",
    "pi": "No session found matching",
}
CLAUDE_TOOLS = ["Read", "Grep", "Glob"]
CLAUDE_CONFINED = "FEATURE_LOOP_CLAUDE_CONFINED"

_child: list[subprocess.Popen] = []


class Fail(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


# --- config ------------------------------------------------------------------


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "feature-loop" / "reviewers"


def parse_value(value: str, where: str) -> tuple[str, str]:
    backend, sep, model = value.partition(":")
    if not sep or backend not in BACKENDS or not model:
        raise Fail(
            EXIT_CONFIG,
            f"{where}: {value!r} is not <backend:model> with backend one of "
            f"{', '.join(BACKENDS)}",
        )
    if backend == "pi":
        provider, slash, name = model.partition("/")
        if not slash or not provider or not name:
            raise Fail(
                EXIT_CONFIG,
                f"{where}: a pi reviewer is pi:<provider>/<model>, got {value!r}",
            )
    return backend, model


def model_id(model: str) -> str:
    """The bare model id: no pi provider prefix, no `:thinking` suffix."""
    return model.rsplit("/", 1)[-1].split(":", 1)[0].strip().lower()


def warn_if_same_model(caller_model: str | None, model: str) -> bool:
    """Warn when the reviewer is exactly the main session's model."""
    if not caller_model or model_id(caller_model) != model_id(model):
        return False
    print(
        "reviewer.py: warning: the reviewer is the same model as the main "
        f"session ({model_id(model)})",
        file=sys.stderr,
        flush=True,
    )
    return True


def resolve(caller: str) -> tuple[str, str]:
    env = os.environ.get("FEATURE_LOOP_REVIEWER", "").strip()
    if env:
        return parse_value(env, "FEATURE_LOOP_REVIEWER")
    path = config_path()
    if not path.is_file():
        raise Fail(
            EXIT_CONFIG,
            f"no reviewer configured. Create {path} with lines like:\n\n"
            f"{EXAMPLE_CONFIG}\n\nor set FEATURE_LOOP_REVIEWER=<backend:model>.",
        )
    table: dict[str, str] = {}
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise Fail(
                EXIT_CONFIG,
                f"{path}:{n}: expected '<family> <backend:model>', got {raw.strip()!r}",
            )
        family, value = parts[0].lower(), parts[1]
        if family in table:
            raise Fail(EXIT_CONFIG, f"{path}:{n}: a second line for {family!r}")
        parse_value(value, f"{path}:{n}")
        table[family] = value
    family = caller.strip().lower()
    value = table.get(family) or table.get("*")
    if not value:
        raise Fail(
            EXIT_CONFIG,
            f"{path} has no line for model family {family!r} and no '*' line. "
            f"Add one, for example:\n\n{family}    <backend:model>",
        )
    return parse_value(value, str(path))


# --- argv --------------------------------------------------------------------


def pi_session_dir(topic_dir: Path) -> Path:
    return topic_dir / "pi-sessions"


def restriction_flags(backend: str, model: str, topic_dir: Path) -> list[str]:
    """The flags that keep a reviewer read-only; shared by start and resume."""
    if backend == "codex":
        return [
            "--json",
            "-m",
            model,
            # `codex exec resume` has no -s, so the sandbox is set by config.
            "-c",
            'sandbox_mode="read-only"',
            # Drops the user's and a trusted repo's MCP servers.
            "--ignore-user-config",
            "--skip-git-repo-check",
        ]
    if backend == "claude":
        return [
            "-p",
            "--model",
            model,
            # Without --restricted claude 2.1.327 has no Grep or Glob.
            "--restricted",
            "--tools",
            ",".join(CLAUDE_TOOLS),
            "--strict-mcp-config",
            "--permission-prompts",
            "none",
            # --restricted keeps the file tools to the working directory and
            # --add-dir. `/` lets claude read what a codex or pi reviewer can.
            "--add-dir",
            str(topic_dir) if os.environ.get(CLAUDE_CONFINED) == "1" else "/",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
    provider, name = model.split("/", 1)
    return [
        "-p",
        "--mode",
        "json",
        "--provider",
        provider,
        "--model",
        name,
        "--tools",
        "read,grep,find,ls",
        "--no-extensions",
        "--no-mcp",
        "--no-approve",
        "--session-dir",
        str(pi_session_dir(topic_dir)),
    ]


def start_argv(backend: str, model: str, topic_dir: Path, session_id: str) -> list[str]:
    flags = restriction_flags(backend, model, topic_dir)
    if backend == "codex":
        return ["codex", "exec", *flags, "-"]
    return [backend, *flags, "--session-id", session_id]


def resume_argv(
    backend: str, model: str, topic_dir: Path, session_id: str, pi_file: str = ""
) -> list[str]:
    flags = restriction_flags(backend, model, topic_dir)
    if backend == "codex":
        return ["codex", "exec", "resume", *flags, session_id, "-"]
    if backend == "claude":
        return ["claude", *flags, "--resume", session_id]
    # By file path: `--session-id` from another cwd silently makes a blank
    # session with the same id, and `--session <id>` may then pick either.
    return ["pi", *flags, "--session", pi_file]


# --- token -------------------------------------------------------------------


def encode_token(backend: str, model: str, session_id: str) -> str:
    raw = json.dumps({"b": backend, "m": model, "id": session_id}).encode()
    return TOKEN_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_token(token: str) -> tuple[str, str, str]:
    bad = Fail(EXIT_CONFIG, f"not a reviewer session token: {token!r}")
    if not token.startswith(TOKEN_PREFIX):
        raise bad
    body = token[len(TOKEN_PREFIX) :]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        backend, model, session_id = data["b"], data["m"], data["id"]
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise bad from None
    parse_value(f"{backend}:{model}", "session token")
    if not isinstance(session_id, str) or not SAFE_ID.match(session_id):
        raise bad
    return backend, model, session_id


# --- files and lock ----------------------------------------------------------


def open_reply(topic_dir: Path) -> Path:
    """Create the call's partial reply file; doubles as the sandbox check."""
    try:
        (topic_dir / "locks").mkdir(parents=True, exist_ok=True)
        pi_session_dir(topic_dir).mkdir(exist_ok=True)
        fd, partial = tempfile.mkstemp(
            dir=topic_dir, prefix=".reply-", suffix=".partial"
        )
    except OSError as exc:
        raise Fail(
            EXIT_FAIL,
            f"cannot write {topic_dir} ({exc.strerror}). In codex, run this call "
            "with escalated permissions; elsewhere check the directory.",
        ) from None
    os.close(fd)
    return Path(partial)


def finish_reply(partial: Path, reply: str) -> Path:
    partial.write_text(reply if reply.endswith("\n") else reply + "\n")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    unique = partial.name[len(".reply-") : -len(".partial")]
    final = partial.with_name(f"reply-{stamp}-{unique}.md")
    os.rename(partial, final)
    return final


def acquire_lock(path: Path) -> int:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise Fail(
            EXIT_BUSY,
            f"an earlier reviewer call on this session is still running (it "
            f"holds {path}). Wait for it to finish and retry, or stop it.",
        ) from None
    return fd


def announce(topic_dir: Path, backend: str, model: str, token: str) -> None:
    print(f"reviewer-session: {token}", flush=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(topic_dir / "reviewer-sessions", "a") as log:
        log.write(f"{stamp} {backend}:{model} {token}\n")


# --- running -----------------------------------------------------------------


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _on_signal(signum: int, _frame: object) -> None:
    for proc in _child:
        _kill(proc)
    sys.stderr.write(f"reviewer.py: stopped by signal {signum}; killed the reviewer\n")
    os._exit(EXIT_FAIL)


def run_child(
    argv: list[str],
    prompt_file: Path,
    lock_fd: int,
    timeout: float,
    on_event: Callable[[dict], None],
) -> tuple[int, str]:
    """Run the reviewer CLI, feeding each JSON stdout line to ``on_event``."""
    with open(prompt_file, "rb") as stdin:
        proc = subprocess.Popen(
            argv,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            # Own group so the timeout kills the CLI's children too.
            start_new_session=True,
            # The child holds the session lock for as long as it runs.
            pass_fds=(lock_fd,),
        )
    _child.append(proc)
    stderr: list[str] = []
    reader = threading.Thread(
        target=lambda: stderr.append(proc.stderr.read()), daemon=True
    )
    reader.start()
    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        _kill(proc)

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                on_event(event)
    except Fail:
        _kill(proc)
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        # An event of a shape the collector does not expect. Without the kill
        # the reviewer would run on, holding the session lock.
        _kill(proc)
        raise Fail(
            EXIT_FAIL,
            f"could not read the reviewer's output ({exc!r}); killed it. The CLI "
            "may have changed its JSON events.",
        ) from None
    finally:
        # The timer stays armed until the child is reaped: a CLI can close
        # stdout and still hang.
        proc.wait()
        timer.cancel()
        reader.join(5)
    if timed_out.is_set():
        raise Fail(
            EXIT_FAIL,
            f"the reviewer did not finish within {timeout:g}s and was killed. "
            "If this ran inside a sandbox (codex), rerun it with escalated permissions.",
        )
    return proc.returncode, "".join(stderr)


def _text_parts(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


class Collector:
    """Pulls the session id, the reply and any error out of a backend's events."""

    def __init__(
        self,
        backend: str,
        on_session: Callable[[str], None] | None,
        caller_model: str | None = None,
        warned: bool = False,
    ) -> None:
        self.backend = backend
        self.on_session = on_session
        self.caller_model = caller_model
        self.warned = warned
        self.reply = ""
        self.error = ""
        self.started = False

    def __call__(self, ev: dict) -> None:
        kind = ev.get("type")
        if self.backend == "claude":
            if kind == "system" and ev.get("subtype") == "init":
                tools, mcp = ev.get("tools"), ev.get("mcp_servers")
                # A missing field fails too: the check must not pass by absence.
                # claude lists the tools sorted, not in --tools order.
                # Non-string entries fail here rather than raise from set().
                if (
                    not isinstance(tools, list)
                    or not all(isinstance(t, str) for t in tools)
                    or set(tools) != set(CLAUDE_TOOLS)
                    or mcp != []
                ):
                    raise Fail(
                        EXIT_FAIL,
                        f"claude started with tools {tools} and MCP servers {mcp}, "
                        f"expected only {CLAUDE_TOOLS} and none; killed it. The "
                        "claude CLI may have changed how its flags apply.",
                    )
                self.started = True
                # An alias such as `opus` resolves only here.
                resolved = ev.get("model")
                if not self.warned and isinstance(resolved, str):
                    self.warned = warn_if_same_model(self.caller_model, resolved)
            elif kind == "result":
                self.reply = ev.get("result") or ""
                if ev.get("is_error"):
                    self.error = self.reply or str(ev.get("subtype"))
        elif self.backend == "codex":
            if kind == "thread.started":
                self.started = True
                if self.on_session:
                    self.on_session(str(ev.get("thread_id", "")))
            elif kind == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") == "agent_message":
                    self.reply = item.get("text") or ""
            elif kind == "turn.completed":
                # An `error` before this was a retry codex recovered from
                # (it reports stream reconnects that way).
                self.error = ""
            elif kind in ("turn.failed", "error"):
                err = ev.get("error")
                self.error = (
                    err.get("message") if isinstance(err, dict) else None
                ) or (ev.get("message") or json.dumps(ev))
        else:
            if kind == "session":
                self.started = True
            elif kind == "agent_end":
                for msg in reversed(ev.get("messages") or []):
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        self.reply = _text_parts(msg.get("content"))
                        self.error = msg.get("errorMessage") or ""
                        break


def review(
    backend: str,
    model: str,
    topic_dir: Path,
    prompt_file: Path,
    timeout: float,
    resume_id: str | None,
    caller_model: str | None = None,
) -> int:
    if not prompt_file.is_file():
        raise Fail(EXIT_CONFIG, f"prompt file not found: {prompt_file}")
    if not shutil.which(backend):
        raise Fail(EXIT_FAIL, f"{backend} is not installed or not on PATH")
    topic_dir = topic_dir.resolve()
    partial = open_reply(topic_dir)
    try:
        return _review(
            backend,
            model,
            topic_dir,
            prompt_file,
            timeout,
            resume_id,
            partial,
            caller_model,
        )
    finally:
        partial.unlink(missing_ok=True)


def _review(
    backend: str,
    model: str,
    topic_dir: Path,
    prompt_file: Path,
    timeout: float,
    resume_id: str | None,
    partial: Path,
    caller_model: str | None = None,
) -> int:
    locks = topic_dir / "locks"
    print(f"reviewer: {backend}:{model}", flush=True)
    warned = warn_if_same_model(caller_model, model)
    on_session = None
    if resume_id is not None:
        pi_file = ""
        if backend == "pi":
            matches = sorted(pi_session_dir(topic_dir).glob(f"*_{resume_id}.jsonl"))
            if not matches:
                raise Fail(
                    EXIT_GONE,
                    f"no pi session {resume_id} in {pi_session_dir(topic_dir)}",
                )
            if len(matches) > 1:
                raise Fail(
                    EXIT_FAIL,
                    f"more than one pi session file for {resume_id}: {matches}",
                )
            pi_file = str(matches[0])
        lock_fd = acquire_lock(locks / f"{resume_id}.lock")
        print(
            f"reviewer-session: {encode_token(backend, model, resume_id)}", flush=True
        )
        argv = resume_argv(backend, model, topic_dir, resume_id, pi_file)
    elif backend == "codex":
        # codex names the thread itself; the lock moves to that name once known.
        start_lock = locks / f"start-{uuid.uuid4()}.lock"
        lock_fd = acquire_lock(start_lock)

        def on_session(thread_id: str) -> None:
            if not SAFE_ID.match(thread_id):
                raise Fail(
                    EXIT_FAIL, f"codex reported an unusable thread id {thread_id!r}"
                )
            os.rename(start_lock, locks / f"{thread_id}.lock")
            announce(topic_dir, backend, model, encode_token(backend, model, thread_id))

        argv = start_argv(backend, model, topic_dir, "")
    else:
        session_id = str(uuid.uuid4())
        lock_fd = acquire_lock(locks / f"{session_id}.lock")
        announce(topic_dir, backend, model, encode_token(backend, model, session_id))
        argv = start_argv(backend, model, topic_dir, session_id)

    collect = Collector(backend, on_session, caller_model, warned)
    returncode, stderr = run_child(argv, prompt_file, lock_fd, timeout, collect)
    # `started` is also what proves claude's restriction check ran.
    if returncode != 0 or collect.error or not collect.reply or not collect.started:
        if resume_id is not None and GONE_MESSAGES[backend] in stderr + collect.error:
            raise Fail(EXIT_GONE, f"{backend} has no session {resume_id}")
        lines = stderr.strip().splitlines()
        detail = collect.error or (lines[-1] if lines else f"exit {returncode}")
        if not collect.started:
            detail += " (the reviewer never started; is the CLI logged in?)"
        raise Fail(EXIT_FAIL, f"{backend} reviewer failed: {detail}")
    final = finish_reply(partial, collect.reply)
    print(collect.reply.rstrip("\n"))
    print(f"reviewer-reply: {final}", flush=True)
    return 0


def show(caller: str, caller_model: str | None = None) -> int:
    backend, model = resolve(caller)
    warn_if_same_model(caller_model, model)
    topic = Path("<dir>")
    sid = "<thread_id>" if backend == "codex" else "<uuid>"
    pi_file = "<dir>/pi-sessions/<file for uuid>.jsonl"
    print(f"reviewer: {backend}:{model}")
    print(
        "start:  "
        + shlex.join(start_argv(backend, model, topic, sid))
        + " < <prompt-file>"
    )
    print(
        "resume: "
        + shlex.join(resume_argv(backend, model, topic, sid, pi_file))
        + " < <prompt-file>"
    )
    return 0


def _user_path(value: str) -> Path:
    return Path(value).expanduser()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_start = sub.add_parser("start", help="start a new reviewer session")
    p_start.add_argument(
        "--caller", required=True, help="the main session's model family"
    )
    p_start.add_argument(
        "--caller-model", help="the main session's exact model id, for a warning"
    )
    p_resume = sub.add_parser("resume", help="send a prompt to an existing session")
    p_resume.add_argument(
        "--session", required=True, help="token from reviewer-session:"
    )
    for p in (p_start, p_resume):
        # A quoted `~` reaches us unexpanded and would make a `~` directory
        # in the caller's working directory.
        p.add_argument(
            "--dir", required=True, type=_user_path, help="the loop's topic directory"
        )
        p.add_argument("--prompt-file", required=True, type=_user_path)
        p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    p_show = sub.add_parser(
        "show", help="print the reviewer and its argv; runs nothing"
    )
    p_show.add_argument("--caller", required=True)
    p_show.add_argument("--caller-model")
    args = parser.parse_args(argv)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    try:
        if args.command == "show":
            return show(args.caller, args.caller_model)
        if args.command == "start":
            backend, model = resolve(args.caller)
            return review(
                backend,
                model,
                args.dir,
                args.prompt_file,
                args.timeout,
                None,
                args.caller_model,
            )
        backend, model, session_id = decode_token(args.session)
        return review(
            backend, model, args.dir, args.prompt_file, args.timeout, session_id
        )
    except Fail as exc:
        print(f"reviewer.py: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
