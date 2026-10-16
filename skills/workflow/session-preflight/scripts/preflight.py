#!/usr/bin/env python3
"""Report expired credentials, unreachable services and a stale checkout.

Runs as a session-start hook and on demand. Silent when every check passes;
otherwise one line per failure naming the command the user should run. It
only reports: it never repairs anything, never prompts, never reads stdin and
never runs ``op`` (a noninteractive ``op`` failure says nothing about the
user's 1Password session).

Stdlib-only and Python 3.9-compatible, so plain ``python3`` runs it with no
uv resolve on every session start. Every check runs concurrently with its own
timeout, and the whole run is cut off at ``TOTAL_BUDGET_SECONDS``: a check
still running then is reported as unfinished and its process group killed.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CHECK_TIMEOUT_SECONDS = 3.0
HTTP_TIMEOUT_SECONDS = 2.0
TOTAL_BUDGET_SECONDS = 5.0
DRIFT_THRESHOLD_COMMITS = 50
CONTAINER_GLOBS = (
    "Dockerfile*",
    "*.Dockerfile",
    "docker/*Dockerfile*",
    "compose.y*ml",
    "docker-compose*.y*ml",
    "Tiltfile",
)
NONINTERACTIVE_ENV = {"GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"}
WITAN_CONFIG_PATH = Path("~/.config/witan/config.toml")
# witan_core.repo_key._CASE_INSENSITIVE_PATH_HOSTS
CASE_INSENSITIVE_PATH_HOSTS = frozenset({"github.com", "gitlab.com"})

_live: set[subprocess.Popen] = set()
_live_lock = threading.Lock()
_stopping = threading.Event()


@dataclass(frozen=True)
class Outcome:
    returncode: int
    stdout: str
    stderr: str

    def detail(self) -> str:
        for line in (self.stderr + "\n" + self.stdout).splitlines():
            if line.strip():
                return line.strip()
        return f"exit {self.returncode}"


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run(args: list[str], cwd: Path | None = None) -> Outcome | None:
    """Run ``args`` in its own process group; ``None`` when it timed out.

    A timeout kills the whole group, so a child the tool spawned (a kubectl
    exec credential plugin, a docker context helper) cannot outlive it. Once
    ``main`` has swept the live processes nothing new starts, so a chained
    check's next step can't escape the sweep.
    """
    with _live_lock:
        if _stopping.is_set():
            return None
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **NONINTERACTIVE_ENV},
            start_new_session=True,
        )
        _live.add(proc)
    try:
        stdout, stderr = proc.communicate(timeout=CHECK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill(proc)
        return None
    finally:
        with _live_lock:
            _live.discard(proc)
    return Outcome(proc.returncode, stdout, stderr)


def _repo_root() -> Path | None:
    if not shutil.which("git"):
        return None
    out = _run(["git", "rev-parse", "--show-toplevel"])
    if out is None or out.returncode != 0:
        return None
    return Path(out.stdout.strip())


def check_gh() -> str | None:
    if not shutil.which("gh"):
        return None
    # --active: without it, one stale secondary account or host fails the
    # whole command even when the account in use works.
    out = _run(["gh", "auth", "status", "--active"])
    if out is None:
        return f"gh: `gh auth status` did not answer within {CHECK_TIMEOUT_SECONDS:g}s. Run: gh auth status"
    if out.returncode == 0:
        return None
    return f"gh: the active account is not logged in or its token is invalid ({out.detail()}). Run: gh auth login"


def check_vault() -> str | None:
    addr = os.environ.get("VAULT_ADDR")
    if not shutil.which("vault") or not addr:
        return None
    out = _run(["vault", "token", "lookup", "-format=json"])
    if out is None:
        return f"vault: {addr} did not answer within {CHECK_TIMEOUT_SECONDS:g}s (VPN?). Run: vault token lookup"
    if out.returncode == 0:
        return None
    return f"vault: no valid token for {addr} ({out.detail()}). Run: vault login -method=oidc"


def check_kubectl() -> str | None:
    if not shutil.which("kubectl"):
        return None
    ctx = _run(["kubectl", "config", "current-context"])
    if ctx is None or ctx.returncode != 0 or not ctx.stdout.strip():
        return None
    context = ctx.stdout.strip()
    out = _run(["kubectl", "auth", "can-i", "get", "pods", "--request-timeout=2s"])
    if out is None:
        return f"kubectl: context {context} did not answer within {CHECK_TIMEOUT_SECONDS:g}s. Run: kubectl auth can-i get pods"
    # "no" is an authenticated answer: the credentials work, the permission
    # is just not granted in this namespace.
    if out.returncode == 0 or out.stdout.strip() == "no":
        return None
    return (
        f"kubectl: context {context} cannot authenticate ({out.detail()}). "
        f"Run: refresh the credentials behind {context}, then kubectl auth can-i get pods"
    )


def check_docker(repo_root: Path | None) -> str | None:
    if repo_root is None or not shutil.which("docker"):
        return None
    if not any(next(repo_root.glob(pattern), None) for pattern in CONTAINER_GLOBS):
        return None
    out = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if out is None:
        return f"docker: `docker info` did not answer within {CHECK_TIMEOUT_SECONDS:g}s. Run: docker info"
    if out.returncode == 0:
        return None
    return "docker: the daemon is not reachable. Run: start Docker (Docker Desktop, or sudo systemctl start docker), then docker info"


def check_drift(repo_root: Path | None) -> str | None:
    if repo_root is None:
        return None
    base = _run(["git", "rev-parse", "--abbrev-ref", "origin/HEAD"], cwd=repo_root)
    if base is None or base.returncode != 0:
        return None
    default = base.stdout.strip()
    counts = _run(
        ["git", "rev-list", "--left-right", "--count", f"HEAD...{default}"],
        cwd=repo_root,
    )
    if counts is None or counts.returncode != 0:
        return None
    behind = int(counts.stdout.split()[1])
    if behind <= DRIFT_THRESHOLD_COMMITS:
        return None
    return (
        f"git: {repo_root} is {behind} commits behind {default} as of the last fetch; "
        f"confirm this is the worktree you meant. Run: git fetch && git rebase {default}"
    )


def _remote_url(repo_root: Path) -> str | None:
    """``origin``'s URL, else the first remote's (``witan.repo.git_remote_url``)."""
    out = _run(["git", "config", "--get-regexp", r"^remote\..*\.url$"], cwd=repo_root)
    if out is None or out.returncode != 0:
        return None
    urls = dict(line.split(" ", 1) for line in out.stdout.splitlines() if " " in line)
    return urls.get("remote.origin.url") or next(iter(urls.values()), None)


def _repo_key(repo_root: Path | None) -> str | None:
    """``host/path`` for the checkout: witan's ``repo.detect`` and
    ``repo_key.normalise``, without the ``https://`` that ``match_target``
    strips anyway."""
    env_repo = os.environ.get("WITAN_REPO")
    if env_repo is not None:
        raw = env_repo or None
    else:
        raw = _remote_url(repo_root) if repo_root is not None else None
    if not raw:
        return None
    url = re.sub(r"\.git$", "", raw.strip().rstrip("/"))
    m = re.match(r"(?:ssh://)?[^@]+@([^:/]+)[:/](.+)", url) or re.match(
        r"https?://(?:[^@/]+@)?([^/]+)/(.+)", url
    )
    if not m:
        return url
    host, path = m.group(1).lower(), m.group(2)
    if host in CASE_INSENSITIVE_PATH_HOSTS:
        path = path.lower()
    return f"{host}/{path}"


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _witan_remote_url(repo_root: Path | None) -> str | None:
    """The deployed witan this checkout talks to, or ``None`` when it uses a
    local store. Mirrors witan's ``load_remote_config``: ``WITAN_REMOTE_URL``,
    then the selected target's ``remote_url``, then the global one. The target
    is the one ``WITAN_TARGET`` names, else the first matched by
    ``match_paths`` > ``match_repos`` > ``match_hosts`` > ``match_orgs``
    (``witan_core.target_config.match_target``). witan fails on an unknown
    ``WITAN_TARGET``; here that skips the check."""
    if url := os.environ.get("WITAN_REMOTE_URL"):
        return url
    try:
        import tomllib
    except ModuleNotFoundError:
        return None
    path = Path(
        os.environ.get("WITAN_CONFIG", "").strip() or WITAN_CONFIG_PATH
    ).expanduser()
    try:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        return None
    targets = cfg.get("targets", {})
    if name := os.environ.get("WITAN_TARGET"):
        return targets.get(name, {}).get("remote_url") or (
            cfg.get("remote_url") if name in targets else None
        )
    selected = None
    local = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()).resolve()
    for target in targets.values():
        for pattern in _as_list(target.get("match_paths")):
            candidate = Path(pattern).expanduser().resolve()
            if local == candidate or candidate in local.parents:
                selected = target
                break
        if selected:
            break
    key = None if selected else _repo_key(repo_root)
    if key:
        host, _, rest = key.partition("/")
        org = rest.split("/")[0]
        tiers: list[Callable[[dict], bool]] = [
            lambda t: any(
                key == re.sub(r"^https?://", "", p).rstrip("/")
                or key.endswith("/" + re.sub(r"^https?://", "", p).rstrip("/"))
                for p in _as_list(t.get("match_repos"))
            ),
            lambda t: host in _as_list(t.get("match_hosts")),
            lambda t: bool(org) and org in _as_list(t.get("match_orgs")),
        ]
        for matches in tiers:
            selected = next((t for t in targets.values() if matches(t)), None)
            if selected:
                break
    return (selected or {}).get("remote_url") or cfg.get("remote_url")


def check_witan(repo_root: Path | None) -> str | None:
    remote = _witan_remote_url(repo_root)
    if not remote:
        return None
    parts = urllib.parse.urlsplit(remote)
    health = f"{parts.scheme}://{parts.netloc}/health"
    try:
        with urllib.request.urlopen(health, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        reason = f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        reason = str(getattr(exc, "reason", exc))
    else:
        return None
    return f"witan: {health} is unreachable ({reason}); witan MCP tools will fail until it answers (VPN?). Run: curl -sS {health}"


def main() -> int:
    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
    repo_root = _repo_root()
    checks: dict[str, Callable[[], str | None]] = {
        "gh": check_gh,
        "vault": check_vault,
        "kubectl": check_kubectl,
        "docker": lambda: check_docker(repo_root),
        "git": lambda: check_drift(repo_root),
        "witan": lambda: check_witan(repo_root),
    }
    results: dict[str, str | None] = {}
    threads = {
        name: threading.Thread(
            target=lambda n=name, c=check: results.__setitem__(n, c()), daemon=True
        )
        for name, check in checks.items()
    }
    for thread in threads.values():
        thread.start()
    for thread in threads.values():
        thread.join(max(0.0, deadline - time.monotonic()))

    with _live_lock:
        _stopping.set()
        for proc in _live:
            _kill(proc)

    for name, thread in threads.items():
        if thread.is_alive():
            print(
                f"{name}: check did not finish within {TOTAL_BUDGET_SECONDS:g}s and was stopped. Run: python3 {__file__}"
            )
        elif results.get(name):
            print(results[name])
    sys.stdout.flush()
    # Daemon threads may still be blocked on a killed child's pipe; exit
    # without joining them.
    os._exit(0)


if __name__ == "__main__":
    main()
