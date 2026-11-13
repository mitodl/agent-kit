"""Map a git diff to the symbols it changes and what depends on them.

Backs ``witan code diff-impact``. ``code_impact`` and ``code_cross_repo_impact``
each take one symbol, so "what does this branch affect" otherwise means mapping
the diff to symbols by hand and fanning out per symbol, which agents skip.

The diff is mapped to symbols by parsing both sides of every changed file
locally with the indexer's own parser, not by reading line ranges out of the
index. The index can lag the base by any number of commits, and a line number
from a stale index names the wrong function. Parsing the exact blobs gives the
same ``repo#path::QualifiedName`` ids the indexer writes, so they route
straight into the id-routed tools.

What the answer cannot see is reported, never left to read as "no impact":
an unindexed repo, symbols the index does not have, callers that only exist on
this branch (id-routed tools read the default view), callers in other files
(the indexer resolves Calls edges within a file only, see
``indexer._file_records``), and consumers in repos nobody indexed.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import indexer

Status = Literal["removed", "modified", "added"]

_STATUS_ORDER: dict[str, int] = {"removed": 0, "modified": 1, "added": 2}

# Every per-symbol lookup is a round trip to the store or the deployment, about
# a second each against a deployment, so they run concurrently.
_LOOKUP_WORKERS = 8

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# The graph has no test-to-code edges, so a test file that calls or names a
# changed symbol is the closest available stand-in for "a test that runs it".
_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec)/"
    r"|(^|/)(test_[^/]*|conftest)\.py$"
    r"|_test\.(py|go)$"
    r"|\.(test|spec)\.[cm]?[jt]sx?$"
)

# How deep `code_impact` walks from each changed symbol. Two levels reaches a
# test that calls a helper that calls the change, without the long tail.
IMPACT_DEPTH = 2


@dataclass
class FileDiff:
    """One file's changed line numbers on each side of a ``-U0`` diff."""

    old_path: str | None
    new_path: str | None
    old_lines: set[int] = field(default_factory=set)
    new_lines: set[int] = field(default_factory=set)

    @property
    def renamed(self) -> bool:
        return None not in (self.old_path, self.new_path) and (
            self.old_path != self.new_path
        )


@dataclass(frozen=True)
class ChangedSymbol:
    """A symbol whose lines the diff touches."""

    symbol_id: str
    path: str
    qualified_name: str
    kind: str
    line: int
    status: Status


def is_test_path(path: str) -> bool:
    """Whether ``path`` looks like a test file in any language the indexer reads.

    :param path: Repo-relative path.
    :returns: ``True`` for test directories and test file naming conventions.
    :rtype: bool
    """
    return _TEST_PATH.search(path) is not None


def _diff_path(raw: str, prefix: str) -> str | None:
    raw = raw.rstrip("\t")
    if raw == "/dev/null":
        return None
    return raw.removeprefix(prefix)


def parse_diff(text: str) -> list[FileDiff]:
    """Parse ``git diff -U0`` output into per-file changed line sets.

    A hunk header ``@@ -a,b +c,d @@`` marks old lines ``a..a+b-1`` and new
    lines ``c..c+d-1``. A pure insertion (``b == 0``) touches no old line,
    and a pure deletion (``d == 0``) no new one, so each side carries only
    the lines that side really changed.

    Header lines are read only before a file's first hunk: inside a hunk, a
    removed SQL comment (``-- note``) is the line ``--- note``.

    :param text: Output of ``git diff -U0 --src-prefix=a/ --dst-prefix=b/``.
    :returns: One entry per file with at least one hunk, plus pure renames,
        which have none but still move every symbol id in the file.
    :rtype: list[FileDiff]
    """
    files: list[FileDiff] = []
    current: FileDiff | None = None
    in_hunks = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = FileDiff(old_path=None, new_path=None)
            files.append(current)
            in_hunks = False
        elif current is None:
            continue
        elif match := _HUNK.match(line):
            in_hunks = True
            old_start, old_count, new_start, new_count = match.groups()
            old_n = 1 if old_count is None else int(old_count)
            new_n = 1 if new_count is None else int(new_count)
            current.old_lines.update(range(int(old_start), int(old_start) + old_n))
            current.new_lines.update(range(int(new_start), int(new_start) + new_n))
        elif in_hunks:
            continue
        elif line.startswith("--- "):
            current.old_path = _diff_path(line[4:], "a/")
        elif line.startswith("+++ "):
            current.new_path = _diff_path(line[4:], "b/")
        elif line.startswith("rename from "):
            current.old_path = line.removeprefix("rename from ")
        elif line.startswith("rename to "):
            current.new_path = line.removeprefix("rename to ")
    return [f for f in files if f.old_lines or f.new_lines or f.renamed]


def _parse(repo: str, path: str, raw: bytes) -> indexer.ParsedFile | None:
    spec = indexer._EXT_TO_SPEC[Path(path).suffix]
    parsed = indexer._parse_file(
        raw, Path(path), spec, repo, f"{repo}#{path}", path, content_hash=""
    )
    if parsed is not None:
        # The indexer starts a definition at `def`/`class`, after its
        # decorators, so a decorator-only edit (a route, an auth check) would
        # land on the enclosing class or <module>. Widen each decorated
        # symbol's range to its first decorator. A local parse, never stored.
        starts = _decorator_starts(raw, spec)
        for sym in parsed.symbols:
            sym.start_line = starts.get(sym.start_line, sym.start_line)
    return parsed


def _decorator_starts(raw: bytes, spec: indexer.LanguageSpec) -> dict[int, int]:
    """``definition start line -> its first decorator's line``.

    Decorators are siblings that precede the definition they apply to: under
    Python's ``decorated_definition`` and before a TypeScript method alike.
    """
    from tree_sitter import Parser

    tree = Parser(indexer._ts_language(spec.grammar)).parse(raw)
    starts: dict[int, int] = {}
    stack = [indexer._root(tree)]
    while stack:
        node = stack.pop()
        children = indexer._children(node)
        stack.extend(children)
        first: int | None = None
        for child in children:
            if indexer._kind(child) == "decorator":
                first = indexer._start_line(child) if first is None else first
            elif first is not None:
                line = indexer._start_line(child)
                starts[line] = min(first, starts.get(line, first))
                first = None
    return starts


def _innermost(
    parsed: indexer.ParsedFile, lines: Iterable[int]
) -> list[indexer.ParsedSymbol]:
    by_id = {s.id: s for s in parsed.symbols}
    ids = {indexer._symbol_at_line(parsed, line) for line in lines}
    return [by_id[sid] for sid in sorted(ids - {None})]


def changed_symbols(
    files: list[FileDiff],
    *,
    repo: str,
    read_blob: Callable[[str, str], bytes],
) -> tuple[list[ChangedSymbol], list[str]]:
    """Map each changed line to the innermost symbol enclosing it.

    New-side lines are resolved against the head blob, old-side lines against
    the base blob. A symbol present on both sides (by qualified name) is
    ``modified``, only on the head side ``added``, only on the base side
    ``removed``. Lines outside every definition land on the file's
    ``<module>`` symbol, as the indexer attributes them.

    :param files: Output of :func:`parse_diff`.
    :param repo: Canonical repo URI that prefixes every symbol id.
    :param read_blob: ``(side, path) -> bytes`` where ``side`` is ``"base"``
        or ``"head"``.
    :returns: The changed symbols in report order, and the changed paths the
        indexer does not parse (docs, config formats it has no grammar for).
    :rtype: tuple[list[ChangedSymbol], list[str]]
    """
    found: dict[str, ChangedSymbol] = {}
    unparsed: list[str] = []
    for f in files:
        if not (f.old_lines or f.new_lines):  # a pure rename
            continue
        head = (
            _parse(repo, f.new_path, read_blob("head", f.new_path))
            if f.new_path and indexer.indexable(Path(f.new_path))
            else None
        )
        base = (
            _parse(repo, f.old_path, read_blob("base", f.old_path))
            if f.old_path and indexer.indexable(Path(f.old_path))
            else None
        )
        if head is None and base is None:
            unparsed.append(f.new_path or f.old_path or "")
            continue
        head_names = {s.qualified_name for s in head.symbols} if head else set()
        base_names = {s.qualified_name for s in base.symbols} if base else set()
        hits: list[tuple[indexer.ParsedSymbol, str, Status]] = []
        if head is not None:
            for sym in _innermost(head, f.new_lines):
                status: Status = (
                    "modified" if sym.qualified_name in base_names else "added"
                )
                hits.append((sym, f.new_path, status))
        if base is not None:
            head_by_name = {s.qualified_name: s for s in head.symbols} if head else {}
            for sym in _innermost(base, f.old_lines):
                if sym.qualified_name in head_names:
                    hits.append(
                        (head_by_name[sym.qualified_name], f.new_path, "modified")
                    )
                else:
                    hits.append((sym, f.old_path, "removed"))
        for sym, path, status in hits:
            found.setdefault(
                sym.id,
                ChangedSymbol(
                    symbol_id=sym.id,
                    path=path,
                    qualified_name=sym.qualified_name,
                    kind=sym.kind,
                    line=sym.start_line,
                    status=status,
                ),
            )
    # Removed symbols lead, so --limit cuts them last: a caller of one is a break.
    return sorted(
        found.values(), key=lambda s: (_STATUS_ORDER[s.status], s.path, s.line)
    ), unparsed


def git_diff_files(base: str, head: str, cwd: Path) -> tuple[str, list[FileDiff]]:
    """Diff ``head`` against its merge base with ``base``.

    :param base: The ref the branch will merge into (e.g. ``origin/main``).
    :param head: The ref under review (e.g. ``HEAD``).
    :param cwd: A directory inside the checkout.
    :returns: The merge-base commit and the parsed diff.
    :rtype: tuple[str, list[FileDiff]]
    """
    merge_base = _git(cwd, "merge-base", base, head).strip()
    diff = _git(
        cwd,
        "-c",
        "core.quotePath=false",
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "-U0",
        "-M",
        merge_base,
        head,
    )
    return merge_base, parse_diff(diff)


def git_blob_reader(
    cwd: Path, base_rev: str, head_rev: str
) -> Callable[[str, str], bytes]:
    """A ``read_blob`` for :func:`changed_symbols` backed by ``git show``."""
    revs = {"base": base_rev, "head": head_rev}

    def read(side: str, path: str) -> bytes:
        return subprocess.run(
            ["git", "show", f"{revs[side]}:{path}"],
            cwd=cwd,
            capture_output=True,
            check=True,
        ).stdout

    return read


def base_branch(base: str, cwd: Path) -> str | None:
    """The git branch ``base`` names, with any remote stripped.

    ``origin/main`` is ``main``, a local branch is itself, and a bare commit
    falls back to the repo's default branch: the store's main view is the
    closest indexed state to any commit on it.

    :param base: The ref the branch merges into.
    :param cwd: A directory inside the checkout.
    :returns: The branch name, or ``None`` when no default is recognizable.
    :rtype: str | None
    """
    from . import repo as repo_module

    full = _git(cwd, "rev-parse", "--symbolic-full-name", base).strip()
    if full.startswith("refs/heads/"):
        return full.removeprefix("refs/heads/")
    if full.startswith("refs/remotes/"):
        # refs/remotes/<remote>/<branch>; a branch may itself contain "/".
        return full.removeprefix("refs/remotes/").partition("/")[2]
    return repo_module._default_branch(cwd)


def branch_name(head: str, cwd: Path) -> str | None:
    """The local branch ``head`` names, or ``None`` for a commit or detached HEAD.

    :param head: A ref, e.g. ``HEAD`` or ``feature-x``.
    :param cwd: A directory inside the checkout.
    :returns: The short branch name.
    :rtype: str | None
    """
    full = _git(cwd, "rev-parse", "--symbolic-full-name", head).strip()
    return full.removeprefix("refs/heads/") if full.startswith("refs/heads/") else None


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


# A `.ts` hook is tested from `.test.tsx` as often as from `.test.ts`.
_JS_FAMILY = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"})


def _language_family(path: str) -> str:
    suffix = Path(path).suffix
    return "js" if suffix in _JS_FAMILY else suffix


def _short(symbol_id: str, repo: str) -> str:
    return symbol_id.removeprefix(f"{repo}#")


def _bounded(items: list, limit: int) -> dict[str, Any]:
    return {"total": len(items), "shown": items[:limit]}


def git_test_mentions(cwd: Path, head: str) -> Callable[[str], list[str]]:
    """``name -> test files at head that contain name as a word``, via git grep.

    The stand-in for the test-to-code edges the graph does not have: Calls
    edges stop at the file boundary, and tests live in other files.
    """

    def mentions(name: str) -> list[str]:
        result = subprocess.run(
            # `:/` and --full-name: the whole tree, as repo-relative paths,
            # whichever subdirectory the command runs from.
            ["git", "grep", "-l", "-w", "-F", "--full-name", "-e", name, head]
            + ["--", ":/"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        # 1 is git grep's "no match", not a failure.
        if result.returncode not in (0, 1):
            raise subprocess.CalledProcessError(
                result.returncode, result.args, result.stdout, result.stderr
            )
        paths = (line.split(":", 1)[1] for line in result.stdout.splitlines())
        return sorted(p for p in paths if is_test_path(p))

    return mentions


def build_report(
    symbols: list[ChangedSymbol],
    unparsed: list[str],
    *,
    renamed: list[str],
    repo: str,
    branch: str | None,
    code: Callable[[str], Callable[..., Any]],
    work: Callable[[str], Callable[..., Any]] | None,
    test_mentions: Callable[[str], list[str]],
    view: str | None,
    limit: int,
    max_items: int,
) -> dict[str, Any]:
    """Join the changed symbols to callers, consumers, tasks and memories.

    :param symbols: Output of :func:`changed_symbols`.
    :param unparsed: Changed paths the indexer does not parse.
    :param renamed: ``old -> new`` for each renamed file.
    :param repo: Canonical repo URI.
    :param branch: The head's git branch, for ``task_for_branch``; ``None``
        when detached.
    :param view: The base's git branch, passed as ``branch`` to every code
        lookup so they read the base's view (the store's main for the default
        branch), where a symbol the head removed still exists. ``None`` sends
        no ``branch``, for a deployment, which has no checkout to follow.
    :param code: Looks up a witan-code tool by name, already unwrapped.
    :param work: The same for witan (council) tools, or ``None`` when witan is
        not installed, in which case tasks and memories are skipped and the
        report says so.
    :param test_mentions: ``name -> test files naming it``, e.g.
        :func:`git_test_mentions`.
    :param limit: Most changed symbols to look up.
    :param max_items: Most rows to keep per list on each symbol.
    :returns: The report, every list carrying its total next to what is shown.
    :rtype: dict[str, Any]
    """
    notes: list[str] = []
    pin = {} if view is None else {"branch": view}
    view_label = (
        "the deployment's default view"
        if view is None
        else f"the base branch's view ({view})"
    )
    indexed_repos = {r["repo"] for r in code("code_indexed_repos")()}
    indexed = repo in indexed_repos
    # A test's own callers and consumers say nothing about the change, and
    # every lookup costs a round trip, so changed tests are listed by file.
    code_symbols = [s for s in symbols if not is_test_path(s.path)]
    changed_tests = sorted({s.path for s in symbols if is_test_path(s.path)})
    shown = code_symbols[:limit]
    if len(code_symbols) > limit:
        notes.append(
            f"Showing {limit} of {len(code_symbols)} changed symbols; pass --limit "
            "to see more."
        )
    if renamed:
        notes.append(
            "A renamed file moves every symbol id in it (repo#path::Name), and "
            "its importers in other files are not traced; grep for the old "
            "module path."
        )
    if not indexed:
        notes.append(
            f"{repo} has no code graph here, so callers and cross-repo consumers "
            "were not looked up. Index it with `witan code index`, or select the "
            "target that serves it."
        )
    else:
        notes.append(
            "Callers are same-file only (the indexer resolves calls by name within "
            f"a file) and come from {view_label}, so callers in other files and "
            "callers this branch adds are not listed. A symbol that view lacks "
            "is marked [not in index]."
        )
        notes.append(
            f"Cross-repo consumers cover the {len(indexed_repos)} repos indexed "
            "here; a consumer in an unindexed repo is not shown."
        )
    notes.append(
        "Tests to run (stand-in): test files that call the symbol or name it. "
        "The graph has no test-to-code edges, so a test that reaches the change "
        "without naming it is not listed."
    )

    # Every symbol, removed ones included, is checked against the same view its
    # callers are read from, so an empty answer from a view that lacks the
    # symbol (a stale index, or a local view of a branch that removed it) is
    # flagged rather than read as "nothing depends on this".
    checked_paths = sorted({s.path for s in shown})

    def file_ids(path: str) -> set[str]:
        rows = code("code_symbols_in_file")(path=path, repo=repo, **pin)
        return {r["symbol_id"] for r in rows}

    def lookup(sym: ChangedSymbol) -> dict[str, Any]:
        row: dict[str, Any] = {
            "symbol_id": sym.symbol_id,
            "path": sym.path,
            "qualified_name": sym.qualified_name,
            "kind": sym.kind,
            "status": sym.status,
        }
        is_module = sym.kind == "module"
        tests: set[str] = set()
        if not is_module:
            # Same language only: `run` in a YAML fixture is not a test of it.
            family = _language_family(sym.path)
            name = sym.qualified_name.rsplit(".", 1)[-1]
            tests.update(
                p
                for p in test_mentions(name)
                if p != sym.path and _language_family(p) == family
            )
        if indexed:
            row["in_index"] = sym.symbol_id in in_index[sym.path]
            # A module is never a Calls target, so it has no callers to walk.
            if not is_module:
                impact = code("code_impact")(
                    symbol_id=sym.symbol_id,
                    max_depth=IMPACT_DEPTH,
                    max_nodes=200,
                    **pin,
                )
                callers = []
                for c in impact["impacted"]:
                    caller_path = c["symbol_id"].split("#", 1)[1].split("::", 1)[0]
                    if is_test_path(caller_path):
                        tests.add(caller_path)
                    else:
                        callers.append(_short(c["symbol_id"], repo))
                row["callers"] = _bounded(callers, max_items)
                row["callers_truncated"] = impact["truncated"]
            cross = code("code_cross_repo_impact")(symbol_id=sym.symbol_id, **pin)
            # One row per binding, so a repo reading the key in two files would
            # otherwise be listed twice.
            contracts = sorted(
                {
                    (b["repo"], b["kind"], b["key"], b["role"])
                    for b in cross["cross_repo"]
                }
            )
            row["cross_repo"] = _bounded(
                [
                    {"repo": r, "kind": kind, "key": key, "role": role}
                    for r, kind, key, role in contracts
                ],
                max_items,
            )
        row["tests_to_run"] = _bounded(sorted(tests), max_items)
        if work is not None:
            ctx = work("symbol_context")(symbol_id=sym.symbol_id)
            row["tasks"] = _bounded(
                [
                    {"slug": t["slug"], "title": t["title"], "status": t["status"]}
                    for t in ctx["tasks"]
                ],
                max_items,
            )
            row["memories"] = _bounded(
                [{"slug": m["slug"], "title": m["title"]} for m in ctx["memories"]],
                max_items,
            )
        return row

    with ThreadPoolExecutor(max_workers=_LOOKUP_WORKERS) as pool:
        in_index = (
            dict(zip(checked_paths, pool.map(file_ids, checked_paths), strict=True))
            if indexed
            else {}
        )
        rows = list(pool.map(lookup, shown))

    # No None values anywhere below: TOML has no null (see `dump_payload`), so
    # an absent answer is an absent key.
    report: dict[str, Any] = {"repo": repo, "indexed": indexed}
    if branch:
        report["branch"] = branch
    if work is None:
        notes.append(
            "witan (council) is not installed, so linked tasks and memories were "
            "not looked up."
        )
    elif branch:
        report["branch_tasks"] = _bounded(
            [
                {"slug": t["slug"], "title": t["title"], "status": t["status"]}
                for t in work("task_for_branch")(branch=branch, repo=repo)
            ],
            max_items,
        )
    report["changed_symbols"] = {"total": len(code_symbols), "shown": rows}
    report["changed_tests"] = _bounded(changed_tests, max_items)
    report["renamed_files"] = _bounded(renamed, max_items)
    report["unparsed_files"] = _bounded(unparsed, max_items)
    report["notes"] = notes
    return report


def _count(bounded: dict[str, Any]) -> str:
    total, shown = bounded["total"], len(bounded["shown"])
    return f"{total}" if total == shown else f"{total}, showing {shown}"


def render_text(report: dict[str, Any], *, header: str) -> str:
    """Render a report as compact plain text for an agent or a terminal.

    :param report: Output of :func:`build_report`.
    :param header: First line, naming what was diffed.
    :returns: The text, newline-terminated.
    :rtype: str
    """
    out = [
        header,
        f"repo: {report['repo']}" + ("" if report["indexed"] else " (NOT INDEXED)"),
    ]
    changed = report["changed_symbols"]
    out.append(f"\nChanged symbols ({_count(changed)}):")
    if not changed["shown"]:
        out.append("  none in files the indexer parses")
    for row in changed["shown"]:
        path_name = f"{row['path']}::{row['qualified_name']}"
        # An unindexed symbol's empty caller list means "unknown", not "none".
        flag = " [not in index]" if row.get("in_index") is False else ""
        out.append(f"- {path_name} ({row['kind']}, {row['status']}){flag}")
        for key, label, fmt in (
            ("callers", "callers", str),
            ("tests_to_run", "tests to run (stand-in)", str),
            (
                "cross_repo",
                "cross-repo",
                lambda b: f"{b['repo']} ({b['kind']} {b['key']}, {b['role']})",
            ),
            ("tasks", "tasks", lambda t: f"{t['slug']} [{t['status']}]"),
            ("memories", "memories", lambda m: m["slug"]),
        ):
            if key in row and row[key]["total"]:
                names = ", ".join(fmt(item) for item in row[key]["shown"])
                out.append(f"    {label} ({_count(row[key])}): {names}")
        if row.get("callers_truncated"):
            out.append(f"    more callers past depth {IMPACT_DEPTH} (see code_impact)")
    if "branch_tasks" in report:
        tasks = report["branch_tasks"]
        listed = ", ".join(f"{t['slug']} [{t['status']}]" for t in tasks["shown"])
        out.append(
            f"\nTasks on branch {report['branch']} ({_count(tasks)})"
            + (f": {listed}" if listed else "")
        )
    for key, label in (
        ("renamed_files", "Renamed files"),
        ("changed_tests", "Changed test files"),
    ):
        if report[key]["total"]:
            out.append(
                f"\n{label} ({_count(report[key])}): " + ", ".join(report[key]["shown"])
            )
    unparsed = report["unparsed_files"]
    if unparsed["total"]:
        out.append(
            f"\nChanged files with no parsed symbols ({_count(unparsed)}): "
            + ", ".join(unparsed["shown"])
        )
    out.append("\nNotes:")
    out.extend(f"- {n}" for n in report["notes"])
    return "\n".join(out) + "\n"
