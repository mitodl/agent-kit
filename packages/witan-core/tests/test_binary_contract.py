"""What the pinned omnigraph binary puts on the wire, asserted directly.

WHY THIS FILE EXISTS. omnigraph 0.9.0 changed how ``export`` renders a
``DateTime`` — naive ISO-8601 string to integer epoch milliseconds — and said
nothing about it in the release notes. What CI reported was::

    TypeError: fromisoformat: argument must be str

raised inside ``_reconcile_nodes``, four layers below the thing that actually
moved, and only raised at all because ``store_merge`` happened to be covered by
a test. Every other parsing site in witan was equally exposed and equally
silent.

So this suite asserts the SHAPE of what the binary returns, one test per
surface witan parses. An undocumented upstream change then arrives as a named
failing assertion that says which surface moved and how — a finding rather than
a puzzle.

★ KEYED ON THE ON-DISK FORMAT VERSION, NOT THE RELEASE. witan supports more
than one representation at once, deliberately: ``_parse_ts`` reads both the
0.8.x string and the 0.9.x integer, because ``witan migrate merge`` accepts an
export taken on another machine and old export files outlive the stores that
produced them. A suite that pinned one release's answer would therefore be
wrong about the other. ``_CONTRACTS`` maps ``internal-schema`` (which
``omnigraph version`` reports) to what that format does, so the suite passes on
every version we have verified — and a version MISSING from the table is a
deliberate, loud failure. That failure is the point: it fires on the bump PR,
before anyone has to debug a TypeError.

WHAT BELONGS HERE: only shapes witan actually depends on. This is not a test
of omnigraph; upstream has its own. Every assertion below should be traceable
to code in ``witan_core``, ``witan``, or ``witan_code`` that would break if it
changed. A contract nobody parses is noise that will one day fail and be
deleted unread rather than investigated.

WHAT DOES NOT BELONG HERE: values that legitimately vary. Timestamps, paths,
row ordering, commit ids. Assert the type and the key, not the reading.

The fixture is deliberately tiny — a handful of rows exercises every shape
below, and a slow contract suite is one people stop running. It uses its own
minimal schema rather than witan's, because these are the binary's contracts,
not witan's schema's, and reaching into a sibling package for a fixture would
couple them for no gain.
"""

import json
import os
import shutil
import subprocess

import pytest

_BINARY = shutil.which("omnigraph")

# A CONTRACT SUITE THAT SILENTLY SKIPS IS WORSE THAN NO CONTRACT SUITE: it
# reads as coverage while asserting nothing, and would keep reading that way
# for as long as it took someone to notice. Skipping is right for a contributor
# who has no binary installed; it is never right in CI, where the binary is an
# install step that must have already succeeded. `WITAN_REQUIRE_OMNIGRAPH=1`
# (set in .github/workflows/witan-core-tests.yml) turns the skip into a hard
# failure, so deleting the install step breaks this suite loudly instead of
# quietly retiring it.
_REQUIRED = os.environ.get("WITAN_REQUIRE_OMNIGRAPH", "") not in ("", "0", "false")

pytestmark = pytest.mark.skipif(
    _BINARY is None and not _REQUIRED,
    reason="omnigraph binary not on PATH (set WITAN_REQUIRE_OMNIGRAPH=1 to require it)",
)

# ── The contract table ────────────────────────────────────────────────────
#
# Keyed by `internal-schema`, the on-disk format version `omnigraph version`
# reports. Verified by hand against each binary; see the per-key notes.
#
# TO ADD A VERSION: run the suite against the new binary, read the failures,
# and record what it actually does — do not copy the previous row and hope.
# The 0.9.0 timestamp change looked exactly like "nothing to see here" until
# it was measured.
_CONTRACTS: dict[int, dict] = {
    # omnigraph 0.8.x
    4: {
        # `export` renders DateTime as a naive ISO-8601 string, offset
        # stripped: "2026-01-01T00:00:00".
        "export_datetime": "iso-string",
        # No per-table row cap on a keyed load.
        "keyed_row_cap": None,
    },
    # omnigraph 0.9.x
    6: {
        # `export` renders DateTime as integer epoch MILLISECONDS:
        # 1767225600000. Not microseconds — `commit list` uses those, and the
        # two surfaces genuinely disagree.
        "export_datetime": "epoch-millis",
        # A keyed write (mutate, or load --mode merge) stages at most this
        # many rows per table. Bounds `witan_core.chunking.LOAD_MAX_ROWS`.
        # `--mode overwrite` is exempt.
        "keyed_row_cap": 8192,
    },
}

# One node type carrying every field shape witan reads back (a @key String, an
# indexed String, two DateTimes, an optional left null), plus one edge type.
_SCHEMA = """
node Doc {
    slug: String @key
    title: String @index
    note: String? @index
    created_at: DateTime @index
    updated_at: DateTime
}

edge Cites: Doc -> Doc
"""

_QUERY = """
query all_docs() {
    match {
        $d: Doc
    }
    return {
        $d.slug, $d.title, $d.note, $d.created_at, $d.updated_at
    }
}
"""

# Fixed instants with distinct millisecond components, so a test can tell
# milliseconds from microseconds from seconds by inspection rather than by
# trusting the constant next to it.
_CREATED = "2026-01-01T00:00:00Z"
_UPDATED = "2026-08-10T12:30:45.123Z"
_CREATED_EPOCH_MS = 1767225600000
_UPDATED_EPOCH_MS = 1786365045123

# Comfortably over every cap in _CONTRACTS, so one fixture exercises both the
# "refused" and the "no cap" branch.
_OVER_CAP_ROWS = 9_000


def _run(*args: str, expect_ok: bool = True) -> subprocess.CompletedProcess:
    if _BINARY is None:
        pytest.fail(
            "WITAN_REQUIRE_OMNIGRAPH is set but no `omnigraph` binary is on "
            "PATH. In CI that means the install step did not run — these "
            "contracts assert what the REAL binary returns and cannot be "
            "checked without it."
        )
    result = subprocess.run(
        ["omnigraph", *args],
        capture_output=True,
        text=True,
        timeout=180,
        # Never raises: half the assertions below are about how the binary
        # REFUSES something, and `expect_ok=False` is how they ask for it.
        check=False,
    )
    if expect_ok and result.returncode != 0:
        raise AssertionError(
            f"omnigraph {' '.join(args)} failed ({result.returncode}):\n{result.stderr}"
        )
    return result


def _doc(slug: str, note: str | None = None) -> dict:
    return {
        "type": "Doc",
        "data": {
            "slug": slug,
            "title": "t",
            "note": note,
            "created_at": _CREATED,
            "updated_at": _UPDATED,
        },
    }


@pytest.fixture(scope="module")
def reader_schema() -> int:
    """The on-disk format version this binary reads, from `omnigraph version`."""
    out = _run("version").stdout
    line = next(
        (ln for ln in out.splitlines() if ln.strip().startswith("internal-schema")),
        None,
    )
    if line is None:
        pytest.fail(
            f"`omnigraph version` no longer reports internal-schema:\n{out}\n"
            "This suite and the format-break detector both depend on that line."
        )
    return int(line.split()[-1])


@pytest.fixture(scope="module")
def contract(reader_schema: int) -> dict:
    """What this format version is known to do — or a loud failure if unknown."""
    if reader_schema not in _CONTRACTS:
        pytest.fail(
            f"omnigraph internal-schema {reader_schema} is not in _CONTRACTS.\n"
            "An on-disk format version landed that nobody verified these wire "
            "contracts against. Run this suite's surfaces by hand against the "
            "new binary, record what it ACTUALLY does, and add a row — the "
            "0.9.0 DateTime change was invisible until it was measured.\n"
            f"Known versions: {sorted(_CONTRACTS)}"
        )
    return _CONTRACTS[reader_schema]


@pytest.fixture(scope="module")
def store(tmp_path_factory) -> str:
    """A tiny graph: two Doc rows and one Cites edge between them."""
    tmp = tmp_path_factory.mktemp("contract")
    schema = tmp / "contract.pg"
    schema.write_text(_SCHEMA)
    path = str(tmp / "contract.omni")
    _run("init", "--schema", str(schema), path)

    data = tmp / "seed.jsonl"
    data.write_text(
        "\n".join(
            json.dumps(r)
            for r in (
                _doc("doc-a"),
                _doc("doc-b", note="annotated"),
                {"edge": "Cites", "from": "doc-a", "to": "doc-b"},
            )
        )
        + "\n"
    )
    _run("load", "--store", path, "--data", str(data), "--mode", "merge")
    return path


@pytest.fixture(scope="module")
def query_file(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("contract-q") / "contract.gq"
    path.write_text(_QUERY)
    return str(path)


@pytest.fixture(scope="module")
def bulk_data(tmp_path_factory) -> str:
    """More rows of one type than any known cap allows."""
    path = tmp_path_factory.mktemp("contract-bulk") / "bulk.jsonl"
    path.write_text(
        "\n".join(json.dumps(_doc(f"bulk-{i:06d}")) for i in range(_OVER_CAP_ROWS))
        + "\n"
    )
    return str(path)


@pytest.fixture
def scratch_store(tmp_path) -> str:
    """A throwaway empty graph, one per test.

    The bulk-load tests below MUTATE what they load into — `--mode overwrite`
    truncates a table outright, which orphans the `store` fixture's Cites edge
    and fails every later test with `src 'doc-a' not found in Doc`. Function
    scope, so they cannot reach each other either.
    """
    schema = tmp_path / "contract.pg"
    schema.write_text(_SCHEMA)
    path = str(tmp_path / "scratch.omni")
    _run("init", "--schema", str(schema), path)
    return path


def _export_records(store: str) -> list[dict]:
    out = _run("export", "--store", store).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _export_node(store: str, slug: str) -> dict:
    """One exported NODE by slug.

    Filtering on "type" first is not incidental: an edge record carries a
    `data` key too, so reaching straight for `record["data"]["slug"]` across a
    whole export raises KeyError on the first edge.
    """
    nodes = [r for r in _export_records(store) if "type" in r]
    return next(n for n in nodes if n["data"]["slug"] == slug)


def test_the_format_version_is_one_we_have_verified(contract):
    """Guard for the whole suite. Fails first, and with instructions, when an
    unrecognised on-disk format appears — rather than letting the individual
    shape tests fail one by one with narrower messages."""
    assert contract


# ── export ────────────────────────────────────────────────────────────────
#
# Parsed by `witan.server.store_merge` (`_classify_rows`, `_reconcile_nodes`)
# and by `witan migrate merge` reading a .jsonl export from another machine.


def test_export_node_records_keep_the_type_data_shape(store):
    """`_classify_rows` splits nodes from edges on the presence of "type"."""
    nodes = [r for r in _export_records(store) if "type" in r]

    assert len(nodes) == 2, f"expected 2 Doc rows, got {len(nodes)}"
    for node in nodes:
        assert node["type"] == "Doc"
        assert isinstance(node["data"], dict)
        assert "slug" in node["data"]


def test_export_edge_records_carry_edge_from_to_and_no_type(store):
    """An edge is `{"edge", "from", "to"}` and carries NO "type" key — that
    asymmetry is the only discriminator `_classify_rows` has."""
    edges = [r for r in _export_records(store) if "type" not in r]

    assert len(edges) == 1, f"expected 1 Cites edge, got {len(edges)}"
    edge = edges[0]
    assert edge["edge"] == "Cites"
    assert edge["from"] == "doc-a"
    assert edge["to"] == "doc-b"
    assert "type" not in edge


def test_export_datetime_matches_this_formats_representation(store, contract):
    """THE 0.9.0 REGRESSION, pinned per format version.

    `witan.server._parse_ts` reads both representations. This asserts the
    binary is still producing the one its format is recorded as producing —
    so a third representation, or a version silently switching, is caught
    here instead of inside a merge.
    """
    doc = _export_node(store, "doc-a")
    created = doc["data"]["created_at"]
    updated = doc["data"]["updated_at"]
    expected = contract["export_datetime"]

    if expected == "epoch-millis":
        assert isinstance(created, int), (
            f"export created_at is {type(created).__name__} ({created!r}), "
            "expected int epoch milliseconds for this format version"
        )
        assert created == _CREATED_EPOCH_MS
        assert updated == _UPDATED_EPOCH_MS
    elif expected == "iso-string":
        assert isinstance(created, str), (
            f"export created_at is {type(created).__name__} ({created!r}), "
            "expected an ISO-8601 string for this format version"
        )
        assert created.startswith("2026-01-01T00:00:00")
        assert updated.startswith("2026-08-10T12:30:45")
    else:  # pragma: no cover - guards a typo'd _CONTRACTS entry
        pytest.fail(f"unknown export_datetime contract {expected!r}")


def test_export_epoch_datetimes_are_milliseconds_not_microseconds(store, contract):
    """Pinned separately from the type, because the SCALE is the trap:
    `commit list` reports microseconds for its own timestamps (see
    test_commit_list_timestamps_are_microseconds). Misreading one as the other
    raises nothing — it dates every row to 1970 and inverts merge decisions."""
    if contract["export_datetime"] != "epoch-millis":
        pytest.skip("this format renders export DateTimes as strings")

    doc = _export_node(store, "doc-a")
    seconds = doc["data"]["created_at"] / 1_000

    assert 1_760_000_000 < seconds < 1_800_000_000, (
        f"created_at={doc['data']['created_at']} does not read as epoch "
        "MILLISECONDS for a 2026 date; the scale changed"
    )


def test_export_keeps_an_unset_optional_as_an_explicit_null(store):
    """`store_merge` round-trips whole exported rows back through `load`, so a
    key silently vanishing would drop data rather than merely read as None."""
    doc = _export_node(store, "doc-a")

    assert "note" in doc["data"], "unset optional dropped its key entirely"
    assert doc["data"]["note"] is None


def test_export_is_jsonl_one_record_per_line(store):
    """`witan_core.chunking` and `witan.remote.proxy._read_export` both read an
    export line-by-line, and `OmnigraphClient.export` streams it to a file
    precisely so it is never materialised whole."""
    lines = [ln for ln in _run("export", "--store", store).stdout.splitlines() if ln]

    assert len(lines) == 3
    for line in lines:
        assert isinstance(json.loads(line), dict)


# ── query ─────────────────────────────────────────────────────────────────
#
# Parsed by `OmnigraphClient.read` — every memory/task/workflow read in witan.
# Checked alongside export deliberately: these two surfaces render DateTime
# DIFFERENTLY as of 0.9.0, and that asymmetry is load-bearing.


def _query_rows(store: str, query_file: str) -> list[dict]:
    out = _run(
        "query", "--store", store, "--query", query_file, "all_docs", "--format", "json"
    ).stdout
    parsed = json.loads(out)
    assert isinstance(parsed, dict), f"expected an envelope object, got {type(parsed)}"
    return parsed["rows"]


def test_query_wraps_rows_in_an_envelope(store, query_file):
    """`read` unwraps `{"rows": [...]}` and falls back to a bare list."""
    rows = _query_rows(store, query_file)

    assert isinstance(rows, list)
    assert len(rows) == 2


def test_query_row_keys_carry_the_binding_prefix(store, query_file):
    """`read` strips the alias with `k.split(".", 1)[-1]`, so "d.slug" becomes
    "slug". If the keys changed SHAPE — nested, or a different separator —
    every field lookup in witan silently returns nothing."""
    row = _query_rows(store, query_file)[0]

    assert "d.slug" in row, f"expected alias-prefixed keys, got {sorted(row)}"
    stripped = {k.split(".", 1)[-1]: v for k, v in row.items()}
    assert stripped["slug"] in {"doc-a", "doc-b"}


def test_query_renders_datetimes_as_iso_strings_on_every_format(store, query_file):
    """The query path did NOT change in 0.9.0 while export did. That asymmetry
    is why only `store_merge` broke and the dozen other `fromisoformat` call
    sites did not. Asserted unconditionally, across all format versions,
    because a release that unified the two would affect all of them at once."""
    row = _query_rows(store, query_file)[0]
    created = row["d.created_at"]

    assert isinstance(created, str), (
        f"query created_at is {type(created).__name__} ({created!r}), expected "
        "an ISO-8601 string — the query path has adopted export's epoch "
        "representation and every fromisoformat call site in witan is affected"
    )
    assert created.startswith("2026-01-01T00:00:00")


# ── commit list ───────────────────────────────────────────────────────────
#
# Parsed by `witan_code.graph.branch_last_write`, which drives the view reaper.


def _commits(store: str, branch: str = "main") -> list[dict]:
    out = _run("commit", "list", "--store", store, "--branch", branch, "--json").stdout
    parsed = json.loads(out)
    assert isinstance(parsed, dict), f"expected an envelope object, got {type(parsed)}"
    return parsed["commits"]


def test_commit_list_wraps_commits_in_an_envelope(store):
    """`branch_last_write` reads `{"commits": [...]}` and RAISES on an
    unexpected shape rather than degrading, because a silent None would turn
    the reaper into a no-op that reports success while branches accumulate."""
    commits = _commits(store)

    assert isinstance(commits, list) and commits
    assert all("manifest_branch" in c for c in commits)
    assert all("created_at" in c for c in commits)


def test_a_branchs_own_commits_are_tagged_with_its_name(scratch_store):
    """The exact contract `witan_code.graph.branch_last_write` filters on:
    `row["manifest_branch"] == <branch>` selects the writes that belong to a
    view rather than the ones it inherited from main.

    Tested on a NAMED branch, not on main, because `manifest_branch` is null
    for main-line commits — which is correct and is why the reaper documents
    "None means the branch has no commits of its own". A test asserting main
    were tagged would be asserting the opposite of the real behaviour.
    """
    _run("branch", "create", "view-x", "--store", scratch_store)

    before = _commits(scratch_store, "view-x")
    assert not [c for c in before if c["manifest_branch"] == "view-x"], (
        "a freshly created branch already has commits of its own; the reaper's "
        "'no commits of its own' signal no longer means what it reads as"
    )

    data = f"{scratch_store}.branch.jsonl"
    with open(data, "w") as fh:
        fh.write(json.dumps(_doc("on-the-branch")) + "\n")
    _run(
        "load",
        "--store",
        scratch_store,
        "--data",
        data,
        "--mode",
        "merge",
        "--branch",
        "view-x",
    )

    tagged = [c for c in _commits(scratch_store, "view-x") if c["manifest_branch"]]
    assert tagged, "a write to view-x produced no commit tagged with that branch"
    assert all(c["manifest_branch"] == "view-x" for c in tagged)


def test_commit_list_timestamps_are_microseconds(store):
    """The other half of the scale trap. `branch_last_write` divides
    `created_at` by 1_000_000 — a DIFFERENT divisor from export's, on purpose.
    If these two surfaces ever converge, both call sites need revisiting."""
    stamps = [c["created_at"] for c in _commits(store) if "created_at" in c]

    assert stamps, "no commit carried a created_at"
    for stamp in stamps:
        assert isinstance(stamp, (int, float))
        seconds = stamp / 1_000_000
        assert 1_700_000_000 < seconds < 2_000_000_000, (
            f"commit created_at={stamp} does not read as epoch MICROseconds; "
            "witan_code.graph.branch_last_write divides by 1_000_000"
        )


# ── branch list ───────────────────────────────────────────────────────────


def test_branch_list_returns_named_branch_rows(store):
    """`witan_code.graph.list_branches` accepts either `{"branches": [...]}` or
    a bare list, and each row as a dict with "name" or a bare string."""
    parsed = json.loads(_run("branch", "list", "--store", store, "--json").stdout)

    rows = parsed.get("branches", parsed) if isinstance(parsed, dict) else parsed
    assert isinstance(rows, list)
    names = [r.get("name") if isinstance(r, dict) else r for r in rows]
    assert "main" in names


# ── version / snapshot ────────────────────────────────────────────────────
#
# The storage-format version, from both sides. These two lines are what makes
# a format break detectable mechanically instead of by reading release notes.


def test_snapshot_reports_the_stores_internal_schema(store):
    """The store's own format version. Paired with the reader's, it says
    exactly which deployed graphs need rebuilding — without opening each one
    and catching the failure."""
    out = _run("snapshot", "--store", store).stdout

    line = next(
        (
            ln
            for ln in out.splitlines()
            if ln.strip().startswith("internal_schema_version")
        ),
        None,
    )
    assert line is not None, (
        f"`omnigraph snapshot` no longer reports internal_schema_version:\n{out}\n"
        "The deployed migration depends on this to decide what to rebuild."
    )
    assert int(line.split(":")[-1]) > 0


def test_a_freshly_written_store_matches_the_reader(store, reader_schema):
    """A store this binary just created must be readable by it. Trivially true
    today, and the canary for a build whose writer and reader disagree."""
    written = int(
        next(
            ln
            for ln in _run("snapshot", "--store", store).stdout.splitlines()
            if "internal_schema_version" in ln
        ).split(":")[-1]
    )

    assert written == reader_schema


# ── limits and the error text witan pattern-matches ───────────────────────
#
# Not cosmetic. `_classify_cli_error` routes retry/repair/abort on substring
# matches, and a wording change silently reclassifies a failure — the worst
# kind of break, because nothing raises.


def test_keyed_load_respects_this_formats_row_cap(scratch_store, bulk_data, contract):
    """Bounds `witan_core.chunking.LOAD_MAX_ROWS`. On a format with no cap this
    asserts the load is accepted, so a cap appearing where there was none is
    caught just as loudly as one changing."""
    cap = contract["keyed_row_cap"]
    result = _run(
        "load",
        "--store",
        scratch_store,
        "--data",
        bulk_data,
        "--mode",
        "merge",
        expect_ok=False,
    )

    if cap is None:
        assert result.returncode == 0, (
            f"{_OVER_CAP_ROWS} rows in one keyed load were REFUSED on a format "
            "recorded as having no per-table row cap. A cap has appeared — set "
            "keyed_row_cap in _CONTRACTS and check chunking.LOAD_MAX_ROWS:\n"
            + result.stderr
        )
        return

    assert _OVER_CAP_ROWS > cap, "fixture must exceed the cap to test it"
    assert result.returncode != 0, (
        f"{_OVER_CAP_ROWS} rows in one keyed load were accepted, but this "
        f"format is recorded as capping keyed writes at {cap} rows per table. "
        "The cap has been raised or removed; revisit chunking.LOAD_MAX_ROWS."
    )
    stderr = result.stderr.lower()
    assert "resource limit exceeded" in stderr
    assert "keyed rows" in stderr
    assert "doc" in stderr, "the refusal no longer names the table it applies to"
    assert str(cap) in result.stderr, (
        f"the refusal no longer states the {cap}-row limit, so chunking cannot "
        f"be tuned from it:\n{result.stderr}"
    )


def test_overwrite_is_never_subject_to_the_row_cap(scratch_store, bulk_data):
    """Why `witan_code.graph.load` must never chunk overwrite: it TRUNCATES the
    table rather than upserting, so splitting it would make each batch erase
    the one before. That is only safe because overwrite is exempt from the cap
    — asserted on every format version, since the exemption is the load-bearing
    part."""
    result = _run(
        "load",
        "--store",
        scratch_store,
        "--data",
        bulk_data,
        "--mode",
        "overwrite",
        "--yes",
        expect_ok=False,
    )

    assert result.returncode == 0, (
        f"`--mode overwrite` was refused for {_OVER_CAP_ROWS} rows. It is no "
        "longer exempt from the keyed-row cap, and witan_code.graph.load's "
        "never-chunk-overwrite rule needs revisiting:\n" + result.stderr
    )
