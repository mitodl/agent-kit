"""Harness adapters against recorded output.

The fixtures under ``fixtures/harness/`` are real runs, scrubbed: a trivial
prompt ("create hello.txt containing hi", then on resume "what file did you
create?") in a throwaway git worktree, captured on 2026-11-25 from Claude Code
2.1.332 (``--model haiku``) and pi 1.5.2. ``pi_quota_error.jsonl`` is a Pi run
whose provider answered 429, which Pi reports in its output and then exits 0.
Paths were rewritten to ``/work/wt``, hook output, tool and MCP lists and the
system prompt were blanked, an internal provider name was replaced with
``example-provider``, and the Bedrock account id was zeroed.

The codec tests replay those lines directly. The session tests run the
adapters against a stand-in executable that prints a fixture, so the command
line, stdin, environment, event log and resume handling are exercised without
a model. ``test_live_*`` runs the real CLIs and only when asked (see
:data:`LIVE_ENV`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from testsupport.hermetic import REAL_HOME

from witan.harness import (
    ADAPTERS,
    Capability,
    ClaudeAdapter,
    PiAdapter,
    ResumeUnavailable,
    SessionRef,
    SessionSpec,
    Unsupported,
    read_event_log,
)
from witan.harness.acp import (
    AgentMessageChunk,
    CurrentModeUpdate,
    Notice,
    SessionNotification,
    SessionUpdate,
    ToolCall,
    ToolCallUpdate,
    UsageUpdate,
    UserMessageChunk,
)
from witan.harness.claude import ClaudeCodec
from witan.harness.pi import PiCodec
from witan.harness.transport import EventLog, this_host

FIXTURES = Path(__file__).parent / "fixtures" / "harness"
FIXTURE_CWD = Path("/work/wt")
CLAUDE_SESSION = "2357f57c-ad23-47bc-a7fb-cdf48afc889d"
PI_SESSION = "3e7972f6-ba04-4716-b069-acbb4fea4fc3"
LIVE_ENV = "WITAN_HARNESS_LIVE"


def replay(codec, fixture: str) -> list[SessionUpdate]:
    updates: list[SessionUpdate] = []
    for line in (FIXTURES / fixture).read_text().splitlines():
        updates.extend(codec.decode(json.loads(line)))
    return updates


def agent_text(updates) -> str:
    return "".join(u.content.text for u in updates if isinstance(u, AgentMessageChunk))


def only(updates, kind):
    found = [u for u in updates if isinstance(u, kind)]
    assert len(found) == 1, found
    return found[0]


# ── codecs ────────────────────────────────────────────────────────────────


def test_claude_start_normalizes_to_acp_updates():
    codec = ClaudeCodec(FIXTURE_CWD)
    updates = replay(codec, "claude_start.jsonl")

    assert codec.session_id == CLAUDE_SESSION
    assert only(updates, CurrentModeUpdate).current_mode_id == "acceptEdits"
    call = only(updates, ToolCall)
    assert (call.name, call.kind, call.status) == ("Write", "edit", "in_progress")
    assert [loc.path for loc in call.locations] == ["/work/wt/hello.txt"]
    assert call.title == "Write: /work/wt/hello.txt"
    result = only(updates, ToolCallUpdate)
    assert (result.tool_call_id, result.status) == (call.tool_call_id, "completed")
    assert "File created successfully" in result.content[0].content.text
    assert agent_text(updates) == "done"
    usage = only(updates, UsageUpdate)
    assert usage.size == 1_000_000
    assert usage.used > 0
    assert usage.cost.currency == "USD"
    assert (codec.stop_reason, codec.error) == ("end_turn", None)
    assert codec.usage["output_tokens"] == 377
    assert codec.usage["cost_usd"] == pytest.approx(0.00917321)


def test_claude_resume_reports_the_same_session():
    codec = ClaudeCodec(FIXTURE_CWD)
    updates = replay(codec, "claude_resume.jsonl")

    assert codec.session_id == CLAUDE_SESSION
    assert agent_text(updates) == "hello.txt"
    assert codec.stop_reason == "end_turn"


def test_claude_hooks_that_succeed_are_not_events_and_failures_are():
    codec = ClaudeCodec(FIXTURE_CWD)
    ok = {
        "type": "system",
        "subtype": "hook_response",
        "hook_name": "SessionStart:startup",
        "exit_code": 0,
        "outcome": "success",
    }
    failed = {**ok, "exit_code": 2, "outcome": "error", "stderr": "boom"}

    assert codec.decode(ok) == []
    notice = only(codec.decode(failed), Notice)
    assert (notice.severity, notice.description) == ("warning", "boom")


def test_claude_error_result_sets_error_and_maps_max_turns():
    codec = ClaudeCodec(FIXTURE_CWD)
    codec.decode(
        {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "session_id": "s",
        }
    )

    assert codec.stop_reason == "max_turn_requests"
    assert codec.error == "error_max_turns"


def test_claude_todowrite_is_also_a_plan():
    codec = ClaudeCodec(FIXTURE_CWD)
    todos = [
        {
            "content": "write tests",
            "status": "in_progress",
            "activeForm": "Writing tests",
        }
    ]
    updates = codec.decode(
        {
            "type": "assistant",
            "message": {
                "id": "m",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "TodoWrite",
                        "input": {"todos": todos},
                    }
                ],
            },
        }
    )

    plan = updates[1]
    assert plan.session_update == "plan"
    assert [(e.content, e.status) for e in plan.entries] == [
        ("write tests", "in_progress")
    ]


def test_large_tool_input_is_clipped_not_logged_whole():
    codec = ClaudeCodec(FIXTURE_CWD)
    block = {
        "type": "tool_use",
        "id": "t1",
        "name": "Write",
        "input": {"file_path": "big.txt", "content": "x" * 10_000},
    }
    call = codec.decode(
        {"type": "assistant", "message": {"id": "m", "content": [block]}}
    )[0]

    assert call.raw_input == {"witan_clipped_chars": len(json.dumps(block["input"]))}
    assert call.locations[0].path == "/work/wt/big.txt"


def test_pi_start_normalizes_to_acp_updates():
    codec = PiCodec(FIXTURE_CWD)
    updates = replay(codec, "pi_start.jsonl")

    assert codec.session_id == PI_SESSION
    call = only(updates, ToolCall)
    assert (call.name, call.kind, call.status) == ("write", "edit", "in_progress")
    # Pi reports the path as given; ACP locations are absolute.
    assert [loc.path for loc in call.locations] == ["/work/wt/hello.txt"]
    result = only(updates, ToolCallUpdate)
    assert (result.tool_call_id, result.status) == (call.tool_call_id, "completed")
    assert agent_text(updates) == "done"
    assert (codec.stop_reason, codec.error) == ("end_turn", None)
    assert codec.usage["output_tokens"] == 71 + 4


def test_pi_resume_reports_the_same_session():
    codec = PiCodec(FIXTURE_CWD)
    updates = replay(codec, "pi_resume.jsonl")

    assert codec.session_id == PI_SESSION
    assert agent_text(updates) == "hello.txt"
    assert codec.stop_reason == "end_turn"


def test_pi_provider_error_is_an_error_not_a_stop_reason():
    codec = PiCodec(FIXTURE_CWD)
    updates = replay(codec, "pi_quota_error.jsonl")

    assert codec.stop_reason is None
    assert codec.error == "429 quota exceeded"
    notice = only(updates, Notice)
    assert (notice.severity, notice.description) == ("error", "429 quota exceeded")


def test_pi_successful_retry_clears_the_earlier_error():
    codec = PiCodec(FIXTURE_CWD)
    replay(codec, "pi_quota_error.jsonl")
    codec.decode(
        {
            "type": "message_end",
            "message": {"role": "assistant", "content": [], "stopReason": "stop"},
        }
    )

    assert (codec.stop_reason, codec.error) == ("end_turn", None)


def test_updates_serialize_as_acp_wire_json():
    codec = ClaudeCodec(FIXTURE_CWD)
    call = only(replay(codec, "claude_start.jsonl"), ToolCall)
    wire = call.wire()

    assert wire["sessionUpdate"] == "tool_call"
    assert wire["toolCallId"] == call.tool_call_id
    assert wire["rawInput"]["file_path"] == "/work/wt/hello.txt"
    assert "_meta" not in wire


# ── sessions, against a stand-in executable ───────────────────────────────

STAND_IN = """\
import json, os, sys
from pathlib import Path
record = Path(os.environ["STAND_IN_RECORD"])
calls = json.loads(record.read_text()) if record.exists() else []
calls.append({
    "argv": sys.argv[1:],
    "stdin": sys.stdin.read(),
    "cwd": os.getcwd(),
    "env": {k: os.environ.get(k) for k in ("CLAUDECODE", "PI_SESSION_ID", "WITAN_SESSION_ID")},
})
record.write_text(json.dumps(calls))
fixtures = os.environ["STAND_IN_FIXTURES"].split(",")
# Enough output to fill a pipe, in lines the codecs ignore.
for _ in range(int(os.environ.get("STAND_IN_PAD_KIB", "0"))):
    print(json.dumps({"type": "rate_limit_event", "pad": "x" * 1024}))
sys.stdout.write(Path(fixtures[len(calls) - 1]).read_text())
print("not json")
sys.exit(int(os.environ.get("STAND_IN_EXIT", "0")))
"""


@pytest.fixture
def stand_in(tmp_path, monkeypatch):
    script = tmp_path / "harness"
    script.write_text(f"#!{sys.executable}\n{STAND_IN}")
    script.chmod(0o755)
    record = tmp_path / "calls.json"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    # What a runner started from inside a Claude Code or Pi session inherits.
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("PI_SESSION_ID", "outer")

    def spec(*fixtures: str, **overrides) -> SessionSpec:
        env = {
            "STAND_IN_RECORD": str(record),
            "STAND_IN_FIXTURES": ",".join(str(FIXTURES / f) for f in fixtures),
            "WITAN_SESSION_ID": "runner/slot-1",
        }
        return SessionSpec(
            prompt="Create hello.txt",
            cwd=worktree,
            event_log=tmp_path / "logs" / "session.jsonl",
            env=env,
            meta={"task_slug": "tk-example"},
            **overrides,
        )

    def calls() -> list[dict]:
        return json.loads(record.read_text())

    return str(script), spec, calls


def test_claude_session_starts_streams_to_the_log_and_resumes(stand_in):
    executable, spec, calls = stand_in
    adapter = ClaudeAdapter(executable)
    start_spec = spec(
        "claude_start.jsonl",
        "claude_resume.jsonl",
        session_id=CLAUDE_SESSION,
        model="haiku",
    )

    session = adapter.start(start_spec)
    events = list(session.events())
    first = session.wait()

    assert first.ok
    assert first.ref == SessionRef(
        "claude", CLAUDE_SESSION, this_host(), start_spec.cwd.resolve()
    )
    assert agent_text(e.update for e in events) == "done"
    call = calls()[0]
    assert call["argv"][:5] == [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--session-id",
    ]
    assert call["argv"][5] == CLAUDE_SESSION
    assert ["--permission-prompts", "none"] == call["argv"][8:10]
    assert call["argv"][-2:] == ["--model", "haiku"]
    assert call["stdin"] == "Create hello.txt"
    assert call["cwd"] == str(start_spec.cwd.resolve())
    assert call["env"] == {
        "CLAUDECODE": None,
        "PI_SESSION_ID": None,
        "WITAN_SESSION_ID": "runner/slot-1",
    }

    second = adapter.resume(first.ref, "What file?", start_spec).wait()

    assert second.ok
    assert second.ref == first.ref
    call = calls()[1]
    assert call["argv"][4:6] == ["--resume", CLAUDE_SESSION]
    assert call["stdin"] == "What file?"


def test_the_event_log_is_acp_notifications_bracketed_by_lifecycle(stand_in):
    executable, spec, _ = stand_in
    start_spec = spec("claude_start.jsonl", session_id=CLAUDE_SESSION)
    ClaudeAdapter(executable).start(start_spec).wait()

    log = read_event_log(start_spec.event_log)
    lines = [json.loads(line) for line in start_spec.event_log.read_text().splitlines()]

    assert all(isinstance(n, SessionNotification) for n in log)
    assert {n.session_id for n in log} == {CLAUDE_SESSION}
    assert [line["_meta"]["witan"]["seq"] for line in lines] == list(
        range(1, len(lines) + 1)
    )
    assert {line["_meta"]["witan"]["task_slug"] for line in lines} == {"tk-example"}
    assert {line["_meta"]["witan"]["harness"] for line in lines} == {"claude"}
    started, prompt, *_, unparsed, exited = log
    assert started.update.meta["witan"]["lifecycle"] == "started"
    assert started.update.meta["witan"]["protocol_version"] == 1
    assert isinstance(prompt.update, UserMessageChunk)
    assert prompt.update.content.text == "Create hello.txt"
    assert isinstance(unparsed.update, Notice)
    assert unparsed.update.description == "not json"
    assert exited.update.meta["witan"] | {"usage": None} == {
        "lifecycle": "exited",
        "returncode": 0,
        "stop_reason": "end_turn",
        "error": None,
        "usage": None,
    }


def test_the_log_fills_while_nobody_reads_events(stand_in):
    executable, spec, _ = stand_in
    start_spec = spec("claude_start.jsonl")
    start_spec = replace(start_spec, env={**start_spec.env, "STAND_IN_PAD_KIB": "256"})
    session = ClaudeAdapter(executable).start(start_spec)

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if '"agent_message_chunk"' in start_spec.event_log.read_text():
            break
        time.sleep(0.05)
    else:
        pytest.fail("the harness's output never reached the log")
    assert session.wait().ok


def test_a_resume_continues_the_log_numbering(stand_in):
    executable, spec, _ = stand_in
    adapter = PiAdapter(executable)
    start_spec = spec("pi_start.jsonl", "pi_resume.jsonl")
    first = adapter.start(start_spec).wait()
    adapter.resume(first.ref, "again", start_spec).wait()

    lines = [json.loads(line) for line in start_spec.event_log.read_text().splitlines()]
    assert [line["_meta"]["witan"]["seq"] for line in lines] == list(
        range(1, len(lines) + 1)
    )
    resumed = [
        line
        for line in lines
        if line["update"].get("_meta", {}).get("witan", {}).get("resumed")
    ]
    assert len(resumed) == 1


def test_a_reported_session_id_replaces_the_minted_one(stand_in):
    executable, spec, _ = stand_in
    session = ClaudeAdapter(executable).start(spec("claude_start.jsonl"))

    assert session.ref.harness_session_ref != CLAUDE_SESSION
    assert session.wait().ref.harness_session_ref == CLAUDE_SESSION
    assert session.ref.harness_session_ref == CLAUDE_SESSION


def test_pi_session_starts_and_resumes_with_the_prompt_on_stdin(stand_in):
    executable, spec, calls = stand_in
    adapter = PiAdapter(executable)
    start_spec = spec(
        "pi_start.jsonl",
        "pi_resume.jsonl",
        session_id=PI_SESSION,
        allowed_tools=("read", "write"),
    )

    first = adapter.start(start_spec).wait()
    # In argv, Pi would read "@reviewer" as a file to attach.
    second = adapter.resume(first.ref, "@reviewer what file?", start_spec).wait()

    assert first.ok and second.ok
    assert first.ref.harness_session_ref == second.ref.harness_session_ref == PI_SESSION
    start_argv, resume_argv = (c["argv"] for c in calls())
    assert start_argv == [
        "-p",
        "--mode",
        "json",
        "--session-id",
        PI_SESSION,
        "--tools",
        "read,write",
    ]
    assert resume_argv[3:5] == ["--session", PI_SESSION]
    assert [c["stdin"] for c in calls()] == ["Create hello.txt", "@reviewer what file?"]
    assert calls()[0]["env"]["PI_SESSION_ID"] is None


def test_a_harness_error_after_exit_zero_is_not_ok(stand_in):
    executable, spec, _ = stand_in
    result = PiAdapter(executable).start(spec("pi_quota_error.jsonl")).wait()

    assert result.returncode == 0
    assert result.error == "429 quota exceeded"
    assert not result.ok


def test_a_nonzero_exit_is_not_ok(stand_in):
    executable, spec, _ = stand_in
    result = (
        PiAdapter(executable)
        .start(
            replace(
                spec("pi_start.jsonl"),
                env={**spec("pi_start.jsonl").env, "STAND_IN_EXIT": "3"},
            )
        )
        .wait()
    )

    assert result.returncode == 3
    assert not result.ok


@pytest.mark.parametrize(
    "change",
    [
        {"host": "elsewhere"},
        {"cwd": Path("/somewhere/else")},
        {"harness": "pi"},
    ],
)
def test_resume_refuses_a_session_this_host_cannot_reach(stand_in, change):
    executable, spec, _ = stand_in
    start_spec = spec("claude_start.jsonl")
    ref = replace(
        SessionRef("claude", CLAUDE_SESSION, this_host(), start_spec.cwd.resolve()),
        **change,
    )

    with pytest.raises(ResumeUnavailable):
        ClaudeAdapter(executable).resume(ref, "again", start_spec)


def test_native_sessions_take_no_live_input(stand_in):
    executable, spec, _ = stand_in
    session = ClaudeAdapter(executable).start(spec("claude_start.jsonl"))

    with pytest.raises(Unsupported):
        session.send("more")
    session.wait()


def test_native_adapters_declare_oneshot_resume_and_events():
    for adapter in ADAPTERS.values():
        assert adapter.capabilities == {
            Capability.ONESHOT,
            Capability.RESUME,
            Capability.EVENTS,
        }


# ── the real CLIs, opt-in ─────────────────────────────────────────────────

live = pytest.mark.skipif(
    not os.environ.get(LIVE_ENV),
    reason=f"runs a real harness against a model; set {LIVE_ENV}=1",
)


def _live_spec(tmp_path: Path, model: str | None) -> SessionSpec:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        [*git, "commit", "-q", "--allow-empty", "-m", "init"], cwd=repo, check=True
    )
    worktree = tmp_path / "wt"
    subprocess.run([*git, "worktree", "add", "-q", str(worktree)], cwd=repo, check=True)
    return SessionSpec(
        prompt="Create a file named hello.txt containing the single word hi. Then reply with only: done",
        cwd=worktree,
        event_log=tmp_path / "session.jsonl",
        model=model,
        # The harness's credentials and session store live in the real home,
        # which the test environment hides.
        env={"HOME": str(REAL_HOME)},
        unset_env=(
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
        ),
    )


def _live_round_trip(adapter, spec: SessionSpec) -> None:
    first = adapter.start(spec).wait()
    assert first.ok, first
    assert (spec.cwd / "hello.txt").read_text().strip() == "hi"
    second_session = adapter.resume(
        first.ref,
        "What is the name of the file you created? Reply with only the file name.",
        spec,
    )
    second = second_session.wait()
    assert second.ok, second
    assert second.ref.harness_session_ref == first.ref.harness_session_ref
    replies = [n.update for n in read_event_log(spec.event_log)]
    assert "hello.txt" in agent_text(replies)


@live
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude not on PATH")
def test_live_claude(tmp_path):
    _live_round_trip(
        ClaudeAdapter(),
        _live_spec(
            tmp_path, os.environ.get("WITAN_HARNESS_LIVE_CLAUDE_MODEL", "haiku")
        ),
    )


@live
@pytest.mark.skipif(shutil.which("pi") is None, reason="pi not on PATH")
def test_live_pi(tmp_path):
    _live_round_trip(
        PiAdapter(), _live_spec(tmp_path, os.environ.get("WITAN_HARNESS_LIVE_PI_MODEL"))
    )


def _bad_todowrite_then(fixture: str, tmp_path: Path, pad_kib: int) -> Path:
    """``fixture`` preceded by a TodoWrite the codec rejects and enough
    output after it to fill a pipe nobody drains."""
    bad = {
        "type": "assistant",
        "message": {
            "id": "m",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t-bad",
                    "name": "TodoWrite",
                    "input": {"todos": [{"content": "x", "status": "bogus"}]},
                }
            ],
        },
    }
    pad = json.dumps({"type": "rate_limit_event", "pad": "x" * 1024}) + "\n"
    path = tmp_path / "bad_then_start.jsonl"
    path.write_text(
        json.dumps(bad) + "\n" + pad * pad_kib + (FIXTURES / fixture).read_text()
    )
    return path


def _wait_within(session, seconds: float):
    outcome: dict = {}

    def run() -> None:
        try:
            outcome["exit"] = session.wait()
        except Exception as exc:  # noqa: BLE001 -- handed back to the test
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        pytest.fail(f"wait() did not return within {seconds}s")
    return outcome


def test_a_line_the_codec_rejects_is_a_notice_and_draining_continues(
    stand_in, tmp_path
):
    executable, spec, _ = stand_in
    fixture = _bad_todowrite_then("claude_start.jsonl", tmp_path, pad_kib=256)
    session = ClaudeAdapter(executable).start(spec(str(fixture)))

    outcome = _wait_within(session, 30)

    assert outcome["exit"].ok, outcome["exit"]
    notices = [
        n.update
        for n in read_event_log(spec(str(fixture)).event_log)
        if isinstance(n.update, Notice)
    ]
    assert notices[0].severity == "error"
    assert notices[0].title == "harness output the codec could not decode"
    assert "ValidationError" in notices[0].description


def test_a_pump_that_cannot_write_the_log_reaps_the_harness_and_raises(
    stand_in, monkeypatch
):
    executable, spec, _ = stand_in
    start_spec = spec("claude_start.jsonl")
    start_spec = replace(start_spec, env={**start_spec.env, "STAND_IN_PAD_KIB": "256"})
    original = EventLog.write

    def write(self, session_id, update):
        # started and the prompt are written before the pump runs.
        if self._seq >= 2:
            raise OSError(28, "No space left on device")
        return original(self, session_id, update)

    monkeypatch.setattr(EventLog, "write", write)
    session = ClaudeAdapter(executable).start(start_spec)

    outcome = _wait_within(session, 30)

    assert isinstance(outcome["error"], OSError)
    assert session._transport._proc.returncode is not None


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(
            lambda s, tmp: replace(s, event_log=tmp / "a-file" / "session.jsonl"),
            id="unwritable-log",
        ),
        pytest.param(
            lambda s, tmp: replace(s, meta={"seq": 7}),
            id="reserved-meta-key",
        ),
    ],
)
def test_a_session_that_cannot_log_never_launches_the_harness(
    stand_in, tmp_path, monkeypatch, change
):
    executable, spec, _ = stand_in
    (tmp_path / "a-file").write_text("")

    def popen(*args, **kwargs):
        pytest.fail("the harness was launched")

    monkeypatch.setattr(subprocess, "Popen", popen)

    with pytest.raises((OSError, ValueError)):
        ClaudeAdapter(executable).start(change(spec("claude_start.jsonl"), tmp_path))


def test_events_alone_see_the_exit_and_so_does_the_log(stand_in):
    executable, spec, _ = stand_in
    start_spec = spec("claude_start.jsonl")
    session = ClaudeAdapter(executable).start(start_spec)

    events = list(session.events())

    assert events[-1].update.meta["witan"]["lifecycle"] == "exited"
    logged = read_event_log(start_spec.event_log)
    assert logged[-1].update.meta["witan"]["lifecycle"] == "exited"
    assert session.wait().ok


def test_a_line_separator_inside_a_record_does_not_split_it(stand_in):
    executable, spec, _ = stand_in
    start_spec = replace(spec("claude_start.jsonl"), prompt="one two\x85three")
    ClaudeAdapter(executable).start(start_spec).wait()

    prompts = [
        n.update
        for n in read_event_log(start_spec.event_log)
        if isinstance(n.update, UserMessageChunk)
    ]
    assert prompts[0].content.text == "one two\x85three"
