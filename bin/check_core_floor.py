#!/usr/bin/env -S uv run --quiet --package witan-core --extra cli --with packaging python
"""Fail when a server's declared `witan-core` floor is lower than what it imports.

WHY. The uv workspace resolves `witan-core` BY PATH (root pyproject's
`[tool.uv.sources]`), so a new `witan_core` symbol is importable in-workspace,
in CI, and in every test run the moment it is written. Only an external
`pip install witan-council` (or `witan-code`) resolves the *published*
witan-core that lacks it — and then the server fails at IMPORT, not at use.

That gap has been crossed three times, always found by review or by the next
change, never by a check:

    #198  witan_core.chunking + load_batch + config_file.resolve_config_path
    #200  chunking.MCP_LOAD_MAX_BYTES  (caught in review ON that PR — it had
          reintroduced the exact defect #198 fixed two commits earlier)
    #201  remote.proxy.RemotePayloadTooLarge + payload_too_large, and
          chunking.describe_budget

All three had a green CI. The floors' pin comments now narrate the count, which
is a comment doing a test's job.

── HOW ──
Build the server's wheel, install it into a CLEAN environment — no workspace, no
path resolution — with witan-core pinned to EXACTLY the lowest version its floor
admits, then import every module in the package. That reproduces the failure
mode rather than modelling it: no symbol bookkeeping, no changelog parsing, and
a new import is covered the day it is written.

★ PINNING IS THE WHOLE TRICK, and getting it wrong makes this check vacuous. A
plain install of the wheel resolves the NEWEST witan-core the floor admits — so
`>=0.11` installs 0.29 and every import succeeds, proving nothing about the
floor. `--resolution lowest-direct` does not help either: witan-core is a
transitive requirement of the wheel, not a direct one, so it stays at the
newest. Only an explicit `==` on the floor version exercises the bound.

★ AND THE FLOOR MAY NAME AN UNPUBLISHED VERSION. That is the *correct* state
for the change that bumps it: a PR raising the floor to `>=0.30` is opened
before witan-core 0.30.0 exists on PyPI, and the three publish workflows fire in
parallel off the same merge commit. So when no published version satisfies the
floor, this falls back to witan-core's locally built wheel — provided the
workspace version is itself the floor. Both branches assert the same property;
they differ only in where the artifact comes from.

Run it as `just check-core-floor`, or one package with `--package witan-code`.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

import cyclopts
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

# The servers, and the top-level package each one publishes. Only these two
# declare a witan-core floor; the other workspace members do not depend on it.
SERVERS = {
    "witan-council": ("mcp/servers/witan", "witan"),
    "witan-code": ("mcp/servers/witan-code", "witan_code"),
}

CORE = "packages/witan-core"

# `witan-core[cli,remote,observability,sentry]>=0.29,<1` — extras kept, because
# an extra that does not exist at the floor version is its own kind of stale
# pin (witan-core gained `sentry` in 0.24; below that uv warns and installs no
# sentry-sdk at all).
CORE_REQUIREMENT = re.compile(
    r"^witan-core(?P<extras>\[[^\]]*\])?\s*(?P<specifier>[<>=!~,.\s0-9a-zA-Z*]+)$"
)

# Imports the package and every module under it, reporting ALL failures rather
# than dying on the first — one stale floor usually breaks several modules, and
# seeing them together is what says which witan_core symbol is missing.
#
# ★ NOT EVERY IMPORT FAILURE IS A FLOOR FAILURE, and conflating the two makes
# this check untrustworthy in the direction that gets checks deleted. Some
# modules legitimately raise at import in a bare environment: `witan.server`
# resolves the omnigraph binary at module scope and raises `RuntimeError:
# omnigraph binary not found` on a runner that has never run `witan setup`.
# That says nothing about the floor.
#
# The discriminator is the EXCEPTION TYPE, not the traceback. "witan_core is
# somewhere in the frames" is too broad and produced exactly that false
# positive: the RuntimeError above is *raised from* witan_core/omnigraph.py, so
# witan_core is all over its traceback. The failure mode a stale floor actually
# produces is a NAME THAT IS NOT THERE — ImportError (a missing module or a
# missing `from ... import X`) or AttributeError on a witan_core object. A
# RuntimeError from inside witan-core is that package working as designed
# against an environment it does not like.
#
# Deliberately narrow. A stale floor could in principle surface some other way
# (a signature change evaluated at module scope, say), and this would miss it —
# but a check that cries wolf is worth less than one with a known blind spot,
# and the three escapes it exists to catch were all plain ImportErrors.
#
# ★ THE NON-FLOOR FAILURES SPLIT IN TWO, and the split is the difference
# between a report and a line people learn to ignore. `witan.server` raising
# `omnigraph binary not found` is not a defect awaiting a fix; it is a design
# decision, taken deliberately and load-bearing for something else (see
# EXPECTED_IMPORT_FAILURES). Printing it under the same heading as a genuinely
# unexplained failure asks the reader to re-derive that on every run, and the
# second or third time they do, they stop reading the section. So a known case
# is named as known, with its reason, and only the rest is left open.
#
# The entries are matched on module + exception type + a substring of the
# message, all three. Matching on the module alone would let some future
# unrelated RuntimeError from `witan.server` inherit an explanation that does
# not apply to it, which is the failure mode an allowlist like this has.
EXPECTED_IMPORT_FAILURES = [
    (
        "witan.server",
        "RuntimeError",
        "omnigraph binary not found",
        "witan.server bootstraps the local store at module scope "
        "(`_ensure_graph`), which needs the omnigraph binary; `witan setup` "
        "installs it. Deliberate: the CLI's local-dispatch guard holds this "
        "import unevaluated precisely BECAUSE importing is what touches the "
        "store, and that ordering is the agent-kit#261 fix. See "
        "witan/server.py::_ensure_graph.",
    ),
]

IMPORT_ALL = """
import importlib, json, pkgutil, sys, textwrap, traceback
pkg = importlib.import_module(sys.argv[1])
expected_spec = json.loads(sys.argv[2])
names = [pkg.__name__] + [
    m.name for m in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + ".")
]
floor, expected, other = [], [], []
for name in names:
    try:
        importlib.import_module(name)
    except Exception as exc:
        trace = "".join(traceback.format_exception(exc))
        kind = type(exc).__name__
        line = f"{name}: {kind}: {exc}"
        missing_name = isinstance(exc, ImportError | AttributeError)
        if missing_name and "witan_core" in trace:
            floor.append(line)
            continue
        reason = next(
            (
                r
                for mod, exc_type, needle, r in expected_spec
                if mod == name and exc_type == kind and needle in str(exc)
            ),
            None,
        )
        (expected if reason else other).append(
            (line, reason) if reason else line
        )
ok = len(names) - len(floor) - len(expected) - len(other)
print(f"imported {ok}/{len(names)} modules")
for line in floor:
    print("FAIL " + line)
for line, reason in expected:
    print("EXPECTED (by design; does not fail this check) " + line)
    print(textwrap.fill(reason, 74, initial_indent="  > ", subsequent_indent="    "))
for line in other:
    print("UNRELATED (not a witan_core import; does not fail this check) " + line)
sys.exit(1 if floor else 0)
"""

app = cyclopts.App(
    name="check-core-floor",
    help="Assert each server imports cleanly against the witan-core floor it declares.",
)


class Floor(NamedTuple):
    """One server's declared dependency on witan-core."""

    server: str
    package: str
    extras: str
    specifier: SpecifierSet

    def pinned(self, version: Version) -> str:
        return f"witan-core{self.extras}=={version}"


def read_floor(root: Path, server: str) -> Floor:
    directory, package = SERVERS[server]
    data = tomllib.loads((root / directory / "pyproject.toml").read_text())
    for requirement in data["project"]["dependencies"]:
        match = CORE_REQUIREMENT.match(requirement.strip())
        if match:
            return Floor(
                server=server,
                package=package,
                extras=match.group("extras") or "",
                specifier=SpecifierSet(match.group("specifier")),
            )
    message = (
        f"{server} declares no witan-core dependency. Either the pin was "
        f"dropped (the servers cannot run without it) or its spelling changed "
        f"past CORE_REQUIREMENT — a check that silently matches nothing is "
        f"worse than no check."
    )
    raise SystemExit(message)


def published_versions(name: str) -> list[Version]:
    """Every version of ``name`` on PyPI, oldest first."""
    url = f"https://pypi.org/simple/{name}/"
    # PEP 691. The Simple API serves HTML unless this exact media type is asked
    # for, and an `application/json` Accept gets the HTML — so the wrong header
    # fails as a JSON parse error rather than as a content negotiation one.
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SystemExit(f"could not reach PyPI for {name}: {exc}") from exc

    versions = []
    for file in json.loads(body).get("files", []):
        # `witan_core-0.29.0-py3-none-any.whl` / `witan_core-0.29.0.tar.gz`
        stem = file["filename"].removesuffix(".tar.gz").removesuffix(".whl")
        parts = stem.split("-")
        if len(parts) < 2:
            continue
        try:
            versions.append(Version(parts[1]))
        except InvalidVersion:
            continue
    return sorted(set(versions))


def workspace_version(root: Path) -> Version:
    data = tomllib.loads((root / CORE / "pyproject.toml").read_text())
    return Version(data["project"]["version"])


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def check(root: Path, floor: Floor, workdir: Path) -> str | None:
    """Return a failure description, or None when the server imports cleanly."""
    lowest = [v for v in published_versions("witan-core") if v in floor.specifier]
    local = workspace_version(root)

    if lowest:
        target, source = min(lowest), "PyPI"
        core_wheel = None
    elif local in floor.specifier:
        # The same-change bump: the floor names the version this branch is about
        # to publish. Test against the wheel the workspace would produce, which
        # is what PyPI will hold once the publish workflow runs.
        target, source = local, "the workspace, not yet published"
        build = run(
            ["uv", "build", "--package", "witan-core", "--wheel", "-o", str(workdir)],
            cwd=root,
        )
        if build.returncode != 0:
            return f"could not build witan-core to test against:\n{build.stderr}"
        core_wheel = next(iter(sorted(workdir.glob("witan_core-*.whl"))), None)
        if core_wheel is None:
            return "uv build produced no witan_core wheel"
    else:
        return (
            f"declares witan-core{floor.specifier} and NOTHING satisfies it — "
            f"no published version does, and the workspace is at {local}. "
            f"The floor names a version that does not and will not exist."
        )

    dist = workdir / floor.server
    build = run(
        ["uv", "build", "--package", floor.server, "--wheel", "-o", str(dist)], cwd=root
    )
    if build.returncode != 0:
        return f"could not build {floor.server}:\n{build.stderr}"
    wheel = next(iter(sorted(dist.glob("*.whl"))), None)
    if wheel is None:
        return f"uv build produced no wheel for {floor.server}"

    venv = workdir / f"venv-{floor.server}"
    # Built OUTSIDE the repo, so nothing about the workspace can reach it: the
    # root `[tool.uv.sources]` is exactly what this check has to be blind to.
    created = run(["uv", "venv", str(venv), "-q"], cwd=workdir)
    if created.returncode != 0:
        return f"could not create a clean venv:\n{created.stderr}"

    pins = [floor.pinned(target) if core_wheel is None else str(core_wheel)]
    install = run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv),
            "--no-cache",
            *pins,
            str(wheel),
        ],
        cwd=workdir,
    )
    if install.returncode != 0:
        return (
            f"cannot even INSTALL against witan-core {target} (from {source}):\n"
            f"{install.stderr.strip()}"
        )

    imported = run(
        [
            str(venv / "bin" / "python"),
            "-c",
            IMPORT_ALL,
            floor.package,
            json.dumps(EXPECTED_IMPORT_FAILURES),
        ],
        cwd=workdir,
    )
    if imported.returncode != 0:
        return (
            f"declares witan-core{floor.specifier}, but does not IMPORT against "
            f"{target} (from {source}):\n\n"
            f"{imported.stdout.strip()}\n{imported.stderr.strip()}"
        )
    for line in imported.stdout.strip().splitlines():
        print(
            f"  {floor.server}: witan-core{floor.specifier} -> {target} "
            f"({source}) — {line}"
            if line.startswith("imported ")
            else f"    {line}"
        )
    return None


@app.default
def main(*, package: str | None = None, root: str = ".") -> None:
    """Check every server, or one.

    Parameters
    ----------
    package
        Check only this server (``witan-council`` or ``witan-code``).
    root
        Repo root. Defaults to the working directory.
    """
    base = Path(root).resolve()
    if package is not None and package not in SERVERS:
        raise SystemExit(f"unknown package {package!r} — one of: {', '.join(SERVERS)}")
    servers = [package] if package else list(SERVERS)

    problems: list[tuple[str, str]] = []
    workdir = Path(tempfile.mkdtemp(prefix="witan-floor-check-"))
    try:
        print("witan-core floor — clean-install import check")
        for server in servers:
            floor = read_floor(base, server)
            failure = check(base, floor, workdir)
            if failure is not None:
                problems.append((server, failure))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if not problems:
        print(f"\n{len(servers)} server(s) import cleanly at their declared floor")
        return

    # Flushed, or the progress lines land after the failure block in a CI log
    # that merges the two streams — which reads as if the check ran twice.
    sys.stdout.flush()
    print("\nWITAN-CORE FLOOR IS STALE\n", file=sys.stderr)
    for server, failure in problems:
        print(f"  {server} {failure}\n", file=sys.stderr)
    print(
        "A server that imports a witan_core symbol added in the same change "
        "must raise its floor in that same change: the workspace resolves "
        "witan-core by path, so nothing else can catch it. Raise the "
        "`witan-core[...]>=X` pin in the server's pyproject.toml to the "
        "version that introduces the symbol named above.",
        file=sys.stderr,
    )
    raise SystemExit(1)


if __name__ == "__main__":
    app()
