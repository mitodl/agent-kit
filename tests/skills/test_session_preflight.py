"""Tests for skills/workflow/session-preflight/scripts/preflight.py.

The script runs as a subprocess against stub binaries: PATH holds only the
stub directory, and HOME, the witan config and the working tree are
throwaway, so no real gh/vault/kubectl/docker/git is ever reached.
Stdlib-only (``python3 -m unittest``) because the script itself is.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "skills/workflow/session-preflight/scripts/preflight.py"
)
# The script's own budget is 5s; this is that plus interpreter startup.
WALL_LIMIT_SECONDS = 6.5
# Before 3.11 there is no tomllib, so the script skips the witan check.
HAS_TOMLLIB = sys.version_info >= (3, 11)

GIT_OK = """\
case "$*" in
  "rev-parse --show-toplevel") echo "$REPO_ROOT" ;;
  "rev-parse --abbrev-ref origin/HEAD") echo origin/main ;;
  "rev-list --left-right --count HEAD...origin/main") echo "0 ${BEHIND:-3}" ;;
  "remote get-url origin")
    [ -n "$NO_ORIGIN" ] && exit 2
    echo "${ORIGIN_URL:-https://github.com/mitodl/agent-kit.git}" ;;
  "remote get-url alpha") echo git@github.com:mitodl/agent-kit.git ;;
  "remote get-url zeta") echo https://github.com/elsewhere/agent-kit.git ;;
  "remote") echo alpha; echo zeta ;;
  # The raw config, in config order and before url.*.insteadOf expansion.
  "config --get-regexp"*)
    echo remote.zeta.url https://github.com/elsewhere/agent-kit.git
    echo remote.alpha.url work:agent-kit.git
    [ -z "$NO_ORIGIN" ] && echo remote.origin.url work:agent-kit.git ;;
  *) exit 1 ;;
esac
"""

KUBECTL_OK = """\
case "$*" in
  "config current-context") echo test-ctx ;;
  *) echo yes ;;
esac
"""


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args: object) -> None:
        pass


class PreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.witan_config = self.root / "witan.toml"

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.health_url = f"http://127.0.0.1:{server.server_port}/mcp"
        # Bound and closed: nothing is listening there.
        closed = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
        self.dead_url = f"http://127.0.0.1:{closed.server_port}/mcp"
        closed.server_close()

    def stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}")
        path.chmod(0o755)

    def stub_all_passing(self) -> None:
        self.stub("gh", "exit 0\n")
        self.stub("vault", "exit 0\n")
        self.stub("kubectl", KUBECTL_OK)
        self.stub("docker", "exit 0\n")
        self.stub("git", GIT_OK)
        (self.repo / "Dockerfile").write_text("FROM scratch\n")
        self.witan_config.write_text(f'remote_url = "{self.health_url}"\n')

    def run_preflight(self, **env: str) -> tuple[str, float]:
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, write_end)
        base = {
            "PATH": str(self.bin),
            "HOME": str(self.home),
            "WITAN_CONFIG": str(self.witan_config),
            "CLAUDE_PROJECT_DIR": str(self.repo),
            "REPO_ROOT": str(self.repo),
            "VAULT_ADDR": "https://vault.invalid",
        }
        start = time.monotonic()
        # stdin is a pipe that is never written or closed: a script that
        # read it would block here until the timeout.
        out = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=self.repo,
            env={**base, **env},
            stdin=read_end,
            capture_output=True,
            text=True,
            timeout=WALL_LIMIT_SECONDS * 2,
            check=False,
        )
        os.close(read_end)
        elapsed = time.monotonic() - start
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stderr, "")
        return out.stdout, elapsed

    def test_silent_when_every_check_passes(self) -> None:
        self.stub_all_passing()
        stdout, _ = self.run_preflight()
        self.assertEqual(stdout, "")

    def test_one_line_per_failure_with_its_fix(self) -> None:
        self.stub_all_passing()
        self.stub("gh", "echo 'token invalid' >&2; exit 1\n")
        self.stub("vault", "echo 'permission denied' >&2; exit 2\n")
        self.stub(
            "kubectl",
            KUBECTL_OK.replace(
                "*) echo yes ;;", "*) echo 'error: token expired' >&2; exit 1 ;;"
            ),
        )
        self.stub("docker", "exit 1\n")
        self.witan_config.write_text(f'remote_url = "{self.dead_url}"\n')
        stdout, _ = self.run_preflight(BEHIND="120")
        lines = stdout.splitlines()
        self.assertEqual(
            [line.split(":")[0] for line in lines],
            ["gh", "vault", "kubectl", "docker", "git", "witan"][
                : 6 if HAS_TOMLLIB else 5
            ],
        )
        for line in lines:
            self.assertIn("Run: ", line)
        self.assertIn("gh auth login", lines[0])
        self.assertIn("permission denied", lines[1])
        self.assertIn("test-ctx", lines[2])
        self.assertIn("120 commits behind origin/main", lines[4])

    def test_missing_tools_and_unconfigured_witan_are_skipped(self) -> None:
        stdout, _ = self.run_preflight()
        self.assertEqual(stdout, "")

    def test_preconditions_skip_present_tools(self) -> None:
        self.stub_all_passing()
        failing = "echo should-not-run >&2; exit 1\n"
        self.stub("vault", failing)
        self.stub("docker", failing)
        (self.repo / "Dockerfile").unlink()
        self.stub(
            "kubectl",
            'case "$*" in "config current-context") exit 1 ;; esac\n' + failing,
        )
        stdout, _ = self.run_preflight(VAULT_ADDR="")
        self.assertEqual(stdout, "")

    def test_kubectl_no_is_an_authenticated_answer(self) -> None:
        self.stub_all_passing()
        self.stub(
            "kubectl", KUBECTL_OK.replace("*) echo yes ;;", "*) echo no; exit 1 ;;")
        )
        stdout, _ = self.run_preflight()
        self.assertEqual(stdout, "")

    def test_drift_under_threshold_is_silent(self) -> None:
        self.stub_all_passing()
        stdout, _ = self.run_preflight(BEHIND="50")
        self.assertEqual(stdout, "")

    @unittest.skipUnless(HAS_TOMLLIB, "needs tomllib")
    def test_witan_uses_the_target_matched_by_org(self) -> None:
        self.stub_all_passing()
        self.witan_config.write_text(
            f'remote_url = "{self.dead_url}"\n'
            "[targets.other]\n"
            f'remote_url = "{self.dead_url}"\n'
            'match_orgs = ["someone-else"]\n'
            "[targets.work]\n"
            f'remote_url = "{self.health_url}"\n'
            'match_orgs = ["mitodl"]\n'
        )
        stdout, _ = self.run_preflight()
        self.assertEqual(stdout, "")

    @unittest.skipUnless(HAS_TOMLLIB, "needs tomllib")
    def test_witan_normalises_the_remote_like_witan(self) -> None:
        self.stub_all_passing()
        self.witan_config.write_text(
            f'remote_url = "{self.dead_url}"\n'
            "[targets.work]\n"
            f'remote_url = "{self.health_url}"\n'
            'match_orgs = ["18f"]\n'
        )
        for origin in (
            "git@github.com:18F/site.git",
            "https://user@GitHub.com/18F/Site/",
            "ssh://git@github.com/18f/site.git",
        ):
            with self.subTest(origin=origin):
                stdout, _ = self.run_preflight(ORIGIN_URL=origin)
                self.assertEqual(stdout, "")

    @unittest.skipUnless(HAS_TOMLLIB, "needs tomllib")
    def test_a_check_that_raises_is_reported(self) -> None:
        self.stub_all_passing()
        self.witan_config.write_text("remote_url = [\n")
        stdout, _ = self.run_preflight()
        self.assertTrue(
            stdout.startswith("witan: the check raised TOMLDecodeError: "), stdout
        )
        self.assertEqual(len(stdout.splitlines()), 1)

    @unittest.skipUnless(HAS_TOMLLIB, "needs tomllib")
    def test_witan_reads_remotes_through_git(self) -> None:
        self.stub_all_passing()
        self.witan_config.write_text(
            f'remote_url = "{self.dead_url}"\n'
            "[targets.work]\n"
            f'remote_url = "{self.health_url}"\n'
            'match_orgs = ["mitodl"]\n'
        )
        # origin is an insteadOf alias that only `git remote get-url` expands.
        stdout, _ = self.run_preflight(ORIGIN_URL="git@github.com:mitodl/agent-kit.git")
        self.assertEqual(stdout, "")
        # Without origin, the first remote in git's sorted order (alpha), not
        # the first in the config file (zeta).
        stdout, _ = self.run_preflight(NO_ORIGIN="1")
        self.assertEqual(stdout, "")

    @unittest.skipUnless(HAS_TOMLLIB, "needs tomllib")
    def test_witan_target_env_selects_the_target(self) -> None:
        self.stub_all_passing()
        self.witan_config.write_text(
            "[targets.work]\n"
            f'remote_url = "{self.health_url}"\n'
            'match_orgs = ["mitodl"]\n'
            "[targets.other]\n"
            f'remote_url = "{self.dead_url}"\n'
        )
        stdout, _ = self.run_preflight(WITAN_TARGET="other")
        self.assertTrue(stdout.startswith("witan: "), stdout)
        stdout, _ = self.run_preflight(WITAN_TARGET="missing")
        self.assertEqual(stdout, "")

    def test_docker_is_checked_for_a_dockerfile_under_docker_dir(self) -> None:
        self.stub_all_passing()
        (self.repo / "Dockerfile").unlink()
        (self.repo / "docker").mkdir()
        (self.repo / "docker" / "app.Dockerfile").write_text("FROM scratch\n")
        self.stub("docker", "exit 1\n")
        stdout, _ = self.run_preflight()
        self.assertTrue(stdout.startswith("docker: "), stdout)

    def test_gh_checks_only_the_active_account(self) -> None:
        self.stub_all_passing()
        self.stub("gh", '[ "$*" = "auth status --active" ]\n')
        stdout, _ = self.run_preflight()
        self.assertEqual(stdout, "")

    @unittest.skipUnless(HAS_TOMLLIB, "needs tomllib")
    def test_witan_match_paths_wins_over_org(self) -> None:
        self.stub_all_passing()
        self.witan_config.write_text(
            "[targets.work]\n"
            f'remote_url = "{self.health_url}"\n'
            'match_orgs = ["mitodl"]\n'
            "[targets.here]\n"
            f'remote_url = "{self.dead_url}"\n'
            f'match_paths = ["{self.repo}"]\n'
        )
        stdout, _ = self.run_preflight()
        self.assertTrue(stdout.startswith("witan: "), stdout)

    def test_a_hung_check_times_out_and_its_processes_are_killed(self) -> None:
        self.stub_all_passing()
        pidfile = self.root / "sleeper.pid"
        # The background sleep is a grandchild holding the stdout pipe open;
        # killing only the stub would leave the script blocked on it.
        self.stub("gh", f"/bin/sleep 30 &\necho $! > {pidfile}\nwait\n")
        stdout, elapsed = self.run_preflight()
        self.assertTrue(
            stdout.startswith("gh: `gh auth status` did not answer within 3s"), stdout
        )
        self.assertLess(elapsed, WALL_LIMIT_SECONDS)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_total_run_is_bounded_when_checks_chain_past_the_budget(self) -> None:
        self.stub_all_passing()
        # Each kubectl call fits its own 3s timeout, but two in a row do not
        # fit the 5s run budget.
        self.stub("kubectl", "/bin/sleep 2.9\n" + KUBECTL_OK)
        stdout, elapsed = self.run_preflight()
        self.assertEqual(
            stdout,
            "kubectl: check did not finish within 5s and was stopped. "
            f"Run: python3 {SCRIPT}\n",
        )
        self.assertLess(elapsed, WALL_LIMIT_SECONDS)


if __name__ == "__main__":
    unittest.main()
