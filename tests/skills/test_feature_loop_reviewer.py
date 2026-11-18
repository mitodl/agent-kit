"""Tests for skills/workflow/feature-loop/scripts/reviewer.py.

The script runs as a subprocess against stub ``codex``/``claude``/``pi``
binaries: PATH holds only the stub directory, and HOME and XDG_CONFIG_HOME are
throwaway, so no real CLI or config is ever reached. Stdlib-only
(``python3 -m unittest``) because the script itself is.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "skills/workflow/feature-loop/scripts/reviewer.py"
)

# No __pycache__ beside the script: agent-kit copies the whole skill directory.
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("reviewer", SCRIPT)
reviewer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reviewer)

# One stub serves all three CLIs; it branches on its own file name and
# STUB_MODE (ok, badtools, noinit, nomcp, gone, fail, hang, closeout).
STUB = r"""
import json, os, sys, time, uuid
name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
mode = os.environ.get("STUB_MODE", "ok")
prompt = sys.stdin.read()
with open(os.path.join(os.environ["STUB_LOG"], name + ".jsonl"), "a") as f:
    f.write(json.dumps({"argv": args, "prompt": prompt}) + "\n")
if os.environ.get("STUB_PIDFILE"):
    with open(os.environ["STUB_PIDFILE"], "w") as f:
        f.write(str(os.getpid()))

def out(ev):
    print(json.dumps(ev), flush=True)

def flag(f):
    return args[args.index(f) + 1] if f in args else None

reply = "LGTM from " + name
if mode == "gone":
    print({
        "claude": "No conversation found with session ID: x",
        "codex": "Error: thread/resume: thread/resume failed: no rollout found for thread id x",
        "pi": "No session found matching 'x'",
    }[name], file=sys.stderr)
    sys.exit(1)
if mode == "fail":
    print("not logged in", file=sys.stderr)
    sys.exit(1)
if mode == "closeout":
    sys.stdout.flush()
    os.close(1)
    time.sleep(60)
if name == "claude":
    sid = flag("--session-id") or flag("--resume")
    init = {"type": "system", "subtype": "init", "session_id": sid, "tools": ["Glob", "Grep", "Read"], "mcp_servers": []}
    if mode == "badtools":
        init["tools"] = ["Bash", "Glob", "Grep", "Read"]
    if mode == "nomcp":
        del init["mcp_servers"]
    if mode != "noinit":
        out(init)
    if mode == "hang":
        time.sleep(60)
    out({"type": "result", "subtype": "success", "is_error": False, "result": reply})
elif name == "codex":
    tid = args[-2] if "resume" in args else str(uuid.uuid4())
    out({"type": "thread.started", "thread_id": tid})
    if mode == "hang":
        time.sleep(60)
    out({"type": "item.completed", "item": {"type": "agent_message", "text": reply}})
    out({"type": "turn.completed"})
else:
    if flag("--session-id"):
        sid = flag("--session-id")
        path = os.path.join(flag("--session-dir"), "2026-11-10T00-00-00-000Z_" + sid + ".jsonl")
        with open(path, "w") as f:
            f.write("{}\n")
    else:
        sid = os.path.basename(flag("--session")).split("_", 1)[1][: -len(".jsonl")]
    out({"type": "session", "id": sid})
    if mode == "hang":
        time.sleep(60)
    out({"type": "agent_end", "messages": [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": [{"type": "text", "text": reply}]},
    ]})
"""

CONFIG = """\
# family  reviewer
claude    codex:gpt-test
gpt       claude:opus-test   # trailing comment
*         pi:prov/model-test
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_dead(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


class ReviewerTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.xdg = self.root / "xdg"
        self.log = self.root / "log"
        self.log.mkdir()
        self.topic = self.root / "topic"
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("Review `plan.md` for $HOME-style problems.\n")
        for name in ("codex", "claude", "pi"):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n{STUB}")
            path.chmod(0o755)

    def env(self, **extra: str) -> dict[str, str]:
        return {
            "PATH": str(self.bin),
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.xdg),
            "STUB_LOG": str(self.log),
            **extra,
        }

    def write_config(self, text: str = CONFIG) -> None:
        path = self.xdg / "feature-loop" / "reviewers"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_script(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=self.env(**env),
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def start(self, **env: str) -> subprocess.CompletedProcess:
        return self.run_script(
            "start",
            "--caller",
            "claude",
            "--dir",
            str(self.topic),
            "--prompt-file",
            str(self.prompt),
            **env,
        )

    def resume(self, token: str, **env: str) -> subprocess.CompletedProcess:
        return self.run_script(
            "resume",
            "--session",
            token,
            "--dir",
            str(self.topic),
            "--prompt-file",
            str(self.prompt),
            **env,
        )

    def calls(self, name: str) -> list[dict]:
        path = self.log / f"{name}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    @staticmethod
    def token_of(out: str) -> str:
        for line in out.splitlines():
            if line.startswith("reviewer-session: "):
                return line.split(": ", 1)[1]
        raise AssertionError(f"no reviewer-session line in {out!r}")

    # --- config ---------------------------------------------------------

    def test_no_config_exits_2_with_example_lines(self) -> None:
        out = self.run_script("show", "--caller", "claude")
        self.assertEqual(out.returncode, 2)
        self.assertIn("claude    codex:", out.stderr)
        self.assertIn("FEATURE_LOOP_REVIEWER", out.stderr)

    def test_family_line_wins_then_catch_all(self) -> None:
        self.write_config()
        for caller, expected in [
            ("claude", "codex:gpt-test"),
            ("GPT", "claude:opus-test"),
            ("glm", "pi:prov/model-test"),
        ]:
            out = self.run_script("show", "--caller", caller)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.splitlines()[0], f"reviewer: {expected}")

    def test_no_matching_line_and_no_catch_all_exits_2(self) -> None:
        self.write_config("claude codex:gpt-test\n")
        out = self.run_script("show", "--caller", "glm")
        self.assertEqual(out.returncode, 2)
        self.assertIn("'glm'", out.stderr)

    def test_env_overrides_file(self) -> None:
        self.write_config()
        out = self.run_script(
            "show", "--caller", "claude", FEATURE_LOOP_REVIEWER="claude:other"
        )
        self.assertEqual(out.stdout.splitlines()[0], "reviewer: claude:other")

    def test_bad_config_lines_exit_2(self) -> None:
        for text in [
            "claude openrouter:x\n",
            "claude codex\n",
            "claude codex:\n",
            "claude pi:no-provider\n",
            "claude codex:a extra\n",
            "claude codex:a\nclaude claude:b\n",
        ]:
            self.write_config(text)
            out = self.run_script("show", "--caller", "claude")
            self.assertEqual(out.returncode, 2, text)

    def test_show_runs_nothing(self) -> None:
        self.write_config()
        out = self.run_script("show", "--caller", "claude")
        self.assertIn("codex exec resume", out.stdout)
        self.assertEqual(list(self.log.iterdir()), [])

    # --- argv and token -------------------------------------------------

    def test_start_and_resume_share_the_restrictions(self) -> None:
        topic = Path("/t")
        required = {
            "codex": ['sandbox_mode="read-only"', "--ignore-user-config"],
            "claude": ["--restricted", "--strict-mcp-config", "--permission-prompts"],
            "pi": ["--no-extensions", "--no-mcp", "--no-approve", "read,grep,find,ls"],
        }
        models = {"codex": "m", "claude": "m", "pi": "p/m"}
        for backend, model in models.items():
            flags = reviewer.restriction_flags(backend, model, topic)
            for flag in required[backend]:
                self.assertIn(flag, flags, backend)
            for argv in (
                reviewer.start_argv(backend, model, topic, "id"),
                reviewer.resume_argv(backend, model, topic, "id", "/t/f.jsonl"),
            ):
                n = len(flags)
                self.assertTrue(
                    any(argv[i : i + n] == flags for i in range(len(argv))),
                    f"{backend}: {argv} lacks {flags}",
                )
        claude = reviewer.restriction_flags("claude", "m", topic)
        self.assertEqual(claude[claude.index("--tools") + 1], "Read,Grep,Glob")

    def test_token_round_trip_and_garbage(self) -> None:
        token = reviewer.encode_token("pi", "openrouter/x:high", "abc-123")
        self.assertEqual(
            reviewer.decode_token(token), ("pi", "openrouter/x:high", "abc-123")
        )
        self.write_config()
        for bad in [
            "nope",
            "fl1.!!!",
            reviewer.encode_token("codex", "m", "../../etc"),
        ]:
            self.assertEqual(self.resume(bad).returncode, 2, bad)

    # --- runs -----------------------------------------------------------

    def assert_reply(self, out: subprocess.CompletedProcess, backend: str) -> Path:
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        self.assertTrue(lines[0].startswith(f"reviewer: {backend}:"), lines)
        self.assertTrue(lines[1].startswith("reviewer-session: fl1."), lines)
        self.assertIn(f"LGTM from {backend}", lines)
        self.assertTrue(lines[-1].startswith("reviewer-reply: "), lines)
        reply = Path(lines[-1].split(": ", 1)[1])
        self.assertEqual(reply.read_text(), f"LGTM from {backend}\n")
        return reply

    def test_claude_start_then_resume(self) -> None:
        env = {"FEATURE_LOOP_REVIEWER": "claude:opus-test"}
        first = self.start(**env)
        reply1 = self.assert_reply(first, "claude")
        token = self.token_of(first.stdout)
        _, _, sid = reviewer.decode_token(token)
        self.assertIn(token, (self.topic / "reviewer-sessions").read_text())
        second = self.resume(token, **env)
        reply2 = self.assert_reply(second, "claude")
        self.assertNotEqual(reply1, reply2)
        self.assertTrue(reply1.exists())
        start_call, resume_call = self.calls("claude")
        self.assertEqual(start_call["argv"][-2:], ["--session-id", sid])
        self.assertEqual(resume_call["argv"][-2:], ["--resume", sid])
        self.assertEqual(start_call["prompt"], self.prompt.read_text())

    def test_codex_start_then_resume(self) -> None:
        env = {"FEATURE_LOOP_REVIEWER": "codex:gpt-test"}
        first = self.start(**env)
        self.assert_reply(first, "codex")
        token = self.token_of(first.stdout)
        _, _, tid = reviewer.decode_token(token)
        self.assertTrue((self.topic / "locks" / f"{tid}.lock").exists())
        self.assert_reply(self.resume(token, **env), "codex")
        start_call, resume_call = self.calls("codex")
        self.assertEqual(start_call["argv"][:1], ["exec"])
        self.assertEqual(resume_call["argv"][:2], ["exec", "resume"])
        self.assertEqual(resume_call["argv"][-2:], [tid, "-"])

    def test_pi_start_then_resume_by_file(self) -> None:
        env = {"FEATURE_LOOP_REVIEWER": "pi:prov/model-test"}
        first = self.start(**env)
        self.assert_reply(first, "pi")
        token = self.token_of(first.stdout)
        self.assert_reply(self.resume(token, **env), "pi")
        start_call, resume_call = self.calls("pi")
        self.assertIn("--session-id", start_call["argv"])
        session_file = resume_call["argv"][resume_call["argv"].index("--session") + 1]
        self.assertTrue(Path(session_file).is_file())
        self.assertEqual(
            start_call["argv"][start_call["argv"].index("--provider") + 1], "prov"
        )

    def test_pi_resume_without_session_file_exits_3_without_running_pi(self) -> None:
        token = reviewer.encode_token("pi", "prov/m", "missing-id")
        out = self.resume(token)
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertEqual(self.calls("pi"), [])

    def test_session_gone_exits_3(self) -> None:
        sessions = self.topic / "pi-sessions"
        sessions.mkdir(parents=True)
        (sessions / "x_pid-1.jsonl").write_text("{}\n")
        for backend, model in [("claude", "m"), ("codex", "m"), ("pi", "p/m")]:
            token = reviewer.encode_token(backend, model, "pid-1")
            out = self.resume(token, STUB_MODE="gone")
            self.assertEqual(out.returncode, 3, f"{backend}: {out.stderr}")

    def test_claude_with_extra_tools_is_killed(self) -> None:
        out = self.start(FEATURE_LOOP_REVIEWER="claude:m", STUB_MODE="badtools")
        self.assertEqual(out.returncode, 1)
        self.assertIn("'Bash'", out.stderr)
        self.assertEqual(list(self.topic.glob("reply-*")), [])

    def test_claude_restriction_check_fails_closed(self) -> None:
        for mode in ("noinit", "nomcp"):
            out = self.start(FEATURE_LOOP_REVIEWER="claude:m", STUB_MODE=mode)
            self.assertEqual(out.returncode, 1, f"{mode}: {out.stdout}")
            self.assertEqual(list(self.topic.glob("reply-*")), [], mode)

    def test_timeout_covers_a_cli_that_closed_stdout(self) -> None:
        pidfile = self.root / "pid"
        out = self.run_script(
            "start",
            "--caller",
            "x",
            "--dir",
            str(self.topic),
            "--prompt-file",
            str(self.prompt),
            "--timeout",
            "1",
            FEATURE_LOOP_REVIEWER="codex:m",
            STUB_MODE="closeout",
            STUB_PIDFILE=str(pidfile),
        )
        self.assertEqual(out.returncode, 1)
        self.assertIn("did not finish", out.stderr)
        self.assertTrue(_wait_dead(int(pidfile.read_text())))

    def test_failed_start_is_not_session_gone(self) -> None:
        out = self.start(FEATURE_LOOP_REVIEWER="codex:m", STUB_MODE="fail")
        self.assertEqual(out.returncode, 1)
        self.assertIn("never started", out.stderr)
        self.assertEqual(list(self.topic.glob(".reply-*")), [])

    def test_cli_missing_exits_1(self) -> None:
        (self.bin / "codex").unlink()
        out = self.start(FEATURE_LOOP_REVIEWER="codex:m")
        self.assertEqual(out.returncode, 1)
        self.assertIn("not installed", out.stderr)

    @unittest.skipIf(os.geteuid() == 0, "root can write a read-only directory")
    def test_unwritable_dir_exits_1_before_running(self) -> None:
        self.topic.mkdir()
        self.topic.chmod(0o500)
        self.addCleanup(self.topic.chmod, 0o700)
        out = self.start(FEATURE_LOOP_REVIEWER="claude:m")
        self.assertEqual(out.returncode, 1)
        self.assertIn("escalated permissions", out.stderr)
        self.assertEqual(self.calls("claude"), [])

    def test_timeout_kills_the_reviewer(self) -> None:
        pidfile = self.root / "pid"
        out = self.run_script(
            "start",
            "--caller",
            "x",
            "--dir",
            str(self.topic),
            "--prompt-file",
            str(self.prompt),
            "--timeout",
            "1",
            FEATURE_LOOP_REVIEWER="claude:m",
            STUB_MODE="hang",
            STUB_PIDFILE=str(pidfile),
        )
        self.assertEqual(out.returncode, 1)
        self.assertIn("did not finish", out.stderr)
        self.assertTrue(_wait_dead(int(pidfile.read_text())))

    def _hanging_resume(
        self, token: str, pidfile: Path
    ) -> tuple[subprocess.Popen, int]:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT),
                "resume",
                "--session",
                token,
                "--dir",
                str(self.topic),
                "--prompt-file",
                str(self.prompt),
            ],
            env=self.env(STUB_MODE="hang", STUB_PIDFILE=str(pidfile)),
            cwd=self.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 10
        while not (pidfile.exists() and pidfile.read_text()):
            self.assertLess(time.monotonic(), deadline, "stub never started")
            time.sleep(0.05)
        child = int(pidfile.read_text())
        self.addCleanup(lambda: _alive(child) and os.kill(child, signal.SIGKILL))
        return proc, child

    def test_lock_outlives_a_killed_script(self) -> None:
        token = self.token_of(self.start(FEATURE_LOOP_REVIEWER="claude:m").stdout)
        proc, child = self._hanging_resume(token, self.root / "pid")
        # SIGKILL leaves the script no chance to clean up; only the child,
        # through the fd it inherited, still holds the lock.
        proc.send_signal(signal.SIGKILL)
        proc.wait(5)
        self.assertTrue(_alive(child))
        busy = self.resume(token)
        self.assertEqual(busy.returncode, 4, busy.stderr)
        os.kill(child, signal.SIGKILL)
        self.assertTrue(_wait_dead(child))
        self.assertEqual(self.resume(token).returncode, 0)

    def test_sigterm_kills_the_reviewer(self) -> None:
        token = self.token_of(self.start(FEATURE_LOOP_REVIEWER="claude:m").stdout)
        proc, child = self._hanging_resume(token, self.root / "pid")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(5), 1)
        self.assertTrue(_wait_dead(child))


if __name__ == "__main__":
    unittest.main()
