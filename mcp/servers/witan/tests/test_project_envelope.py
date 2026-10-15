"""The autonomy envelope on WorkflowProject: ``preauthorized`` and
``reserved_decisions``, their round trip through the tools and the CLI, and
how the context hook and ``witan project run`` render them."""

import uuid

import pytest

from witan import context as ctx
from witan import envelope

from .conftest import requires_omnigraph

ALLOW = ["deploy to the QA stack", "use the existing ~/.ssh/deploy key"]
RESERVE = ["merging to main"]


@pytest.fixture(autouse=True)
def _caller_is_alice(monkeypatch):
    """Pin the server-resolved caller. In a full-suite run it resolved to the
    machine's user rather than the fixture's ``WITAN_AUTHOR``, so the setter
    assertions can't lean on the env var."""
    from witan import server as srv

    monkeypatch.setattr(srv, "_current_author", lambda: "alice")


@requires_omnigraph
def test_create_get_and_status_round_trip(server):
    proj = server.workflow_project_create(
        title="envelope",
        description="d",
        preauthorized=ALLOW,
        reserved_decisions=RESERVE,
    )

    got = server.workflow_project_get(proj["slug"])
    assert got["preauthorized"] == ALLOW
    assert got["reserved_decisions"] == RESERVE

    status = server.workflow_project_status(proj["slug"])["project"]
    assert status["preauthorized"] == ALLOW
    assert status["reserved_decisions"] == RESERVE

    listed = {p["slug"]: p for p in server.workflow_project_list(repo="")}
    assert listed[proj["slug"]]["preauthorized"] == ALLOW


@requires_omnigraph
def test_update_replaces_and_empty_list_clears(server):
    proj = server.workflow_project_create(
        title="envelope",
        description="d",
        preauthorized=ALLOW,
        reserved_decisions=RESERVE,
    )

    replaced = server.workflow_project_update(proj["slug"], preauthorized=["only this"])
    assert replaced["preauthorized"] == ["only this"]
    assert replaced["reserved_decisions"] == RESERVE

    cleared = server.workflow_project_update(proj["slug"], reserved_decisions=[])
    assert cleared["reserved_decisions"] is None
    assert cleared["preauthorized"] == ["only this"]


@requires_omnigraph
def test_writes_to_other_fields_leave_the_envelope_intact(server):
    """Every path that rewrites a project row has to carry both lists through.
    ``workflow_project_update`` reads the row and writes every editable field
    back, so a projection missing either list would null it here."""
    proj = server.workflow_project_create(
        title="envelope",
        description="d",
        preauthorized=ALLOW,
        reserved_decisions=RESERVE,
    )
    slug = proj["slug"]
    other = server.workflow_project_create(title="other", description="d")

    server.workflow_project_update(slug, title="renamed", tags=["t"])
    server.workflow_project_update(slug, add_repos=["https://github.com/x/new"])
    server.workflow_project_advance(slug, phase="spec")
    server.workflow_project_block(other["slug"], slug)
    server.workflow_session_start(
        project_slug=slug,
        session_id=uuid.uuid4().hex,
        phase="spec",
        repo="https://github.com/x/accreted",
    )

    got = server.workflow_project_get(slug)
    assert got["title"] == "renamed"
    assert got["preauthorized"] == ALLOW
    assert got["reserved_decisions"] == RESERVE


@requires_omnigraph
def test_an_unrelated_edit_does_not_restore_a_concurrently_changed_envelope(
    server, monkeypatch
):
    """A title edit reads the row, another session revokes the grants and adds
    a reservation, then the title edit writes. Copying the envelope forward
    from that read would put the grants back and drop the reservation."""
    from witan import server as srv

    proj = server.workflow_project_create(
        title="envelope",
        description="d",
        preauthorized=ALLOW,
        reserved_decisions=RESERVE,
    )
    slug = proj["slug"]
    real_read = srv.client.read
    interleaved = []

    def read_then_concurrent_write(queries, name, params):
        rows = real_read(queries, name, params)
        if name == "get_workflow_project" and not interleaved:
            interleaved.append(True)
            server.workflow_project_update(
                slug, preauthorized=[], reserved_decisions=[*RESERVE, "rotating keys"]
            )
        return rows

    monkeypatch.setattr(srv.client, "read", read_then_concurrent_write)
    server.workflow_project_update(slug, title="renamed")
    monkeypatch.setattr(srv.client, "read", real_read)

    got = server.workflow_project_get(slug)
    assert interleaved
    assert got["title"] == "renamed"
    assert got["preauthorized"] is None
    assert got["reserved_decisions"] == [*RESERVE, "rotating keys"]


@requires_omnigraph
def test_updating_one_envelope_list_does_not_write_the_other(server, monkeypatch):
    from witan import server as srv

    proj = server.workflow_project_create(
        title="envelope",
        description="d",
        preauthorized=ALLOW,
        reserved_decisions=RESERVE,
    )
    written = []
    real_change_many = srv.client.change_many

    def recording_change_many(steps, **kwargs):
        written.extend(name for _, name, _ in steps)
        return real_change_many(steps, **kwargs)

    monkeypatch.setattr(srv.client, "change_many", recording_change_many)
    server.workflow_project_update(proj["slug"], preauthorized=["only this"])

    assert written == ["update_workflow_project_preauthorized"]
    got = server.workflow_project_get(proj["slug"])
    assert got["preauthorized"] == ["only this"]
    assert got["reserved_decisions"] == RESERVE


def _guard_client(monkeypatch, *, needle, category, **cfg):
    """Put a WriteGuard on the test client, flagging ``needle``."""
    from witan import server as srv
    from witan.config import ScanConfig
    from witan.scan import Finding, ScannerRegistry, WriteGuard, masked_preview

    class Needle:
        name = "needle"

        def __init__(self):
            self.category = category

        def scan(self, text, field, node_type):
            start = text.find(needle)
            if start == -1:
                return []
            return [
                Finding(
                    detector=self.name,
                    category=category,
                    start=start,
                    end=start + len(needle),
                    preview=masked_preview(self.name, needle),
                )
            ]

    guard = WriteGuard(ScanConfig(enabled=True, **cfg), ScannerRegistry([Needle()]))
    monkeypatch.setattr(srv.client, "guard", guard)


@requires_omnigraph
def test_create_with_a_secret_in_the_envelope_is_refused(server, monkeypatch):
    from witan.scan import WriteBlocked

    _guard_client(monkeypatch, needle="AKIASECRET", category="secret")
    with pytest.raises(WriteBlocked):
        server.workflow_project_create(
            title="envelope", description="d", preauthorized=["use AKIASECRET"]
        )
    assert server.workflow_project_list(repo="") == []


@requires_omnigraph
def test_update_with_a_secret_in_the_envelope_is_refused(server, monkeypatch):
    from witan.scan import WriteBlocked

    proj = server.workflow_project_create(
        title="envelope", description="d", reserved_decisions=RESERVE
    )
    _guard_client(monkeypatch, needle="AKIASECRET", category="secret")
    with pytest.raises(WriteBlocked):
        server.workflow_project_update(
            proj["slug"], title="renamed", reserved_decisions=["AKIASECRET"]
        )
    # One commit: the refused list takes the title edit down with it.
    got = server.workflow_project_get(proj["slug"])
    assert got["title"] == "envelope"
    assert got["reserved_decisions"] == RESERVE


@requires_omnigraph
def test_update_redacts_an_envelope_item_in_the_store(server, monkeypatch):
    proj = server.workflow_project_create(title="envelope", description="d")
    _guard_client(monkeypatch, needle="a@b.com", category="pii", pii_action="redact")

    got = server.workflow_project_update(
        proj["slug"], preauthorized=["deploy to QA", "email a@b.com when done"]
    )

    assert got["preauthorized"] == ["deploy to QA", "email «redacted:needle» when done"]


@requires_omnigraph
def test_blank_items_are_dropped(server):
    proj = server.workflow_project_create(
        title="envelope",
        description="d",
        preauthorized=["  keep  ", "", "   ", "deploy QA\n\n## Not a heading"],
    )
    assert server.workflow_project_get(proj["slug"])["preauthorized"] == [
        "keep",
        "deploy QA ## Not a heading",
    ]


# ── provenance ───────────────────────────────────────────────────────────────


def _as_caller(monkeypatch, who):
    """Make the server resolve every following write's actor to ``who``."""
    from witan import server as srv

    monkeypatch.setattr(srv, "_current_author", lambda: who)


@requires_omnigraph
def test_create_records_the_creator_as_setter(server):
    proj = server.workflow_project_create(
        title="envelope",
        description="d",
        preauthorized=ALLOW,
        reserved_decisions=RESERVE,
    )

    got = server.workflow_project_get(proj["slug"])
    assert got["author"] == "alice"
    assert got["preauthorized_set_by"] == "alice"
    assert got["reserved_decisions_set_by"] == "alice"

    status = server.workflow_project_status(proj["slug"])["project"]
    assert status["author"] == "alice"
    assert status["preauthorized_set_by"] == "alice"

    listed = {p["slug"]: p for p in server.workflow_project_list(repo="")}
    assert listed[proj["slug"]]["author"] == "alice"
    assert listed[proj["slug"]]["preauthorized_set_by"] == "alice"


@requires_omnigraph
def test_an_empty_list_has_no_setter(server):
    proj = server.workflow_project_create(
        title="envelope", description="d", reserved_decisions=RESERVE
    )
    got = server.workflow_project_get(proj["slug"])
    assert got["preauthorized_set_by"] is None

    cleared = server.workflow_project_update(proj["slug"], reserved_decisions=[])
    assert cleared["reserved_decisions_set_by"] is None


@requires_omnigraph
def test_another_callers_write_is_recorded_as_theirs(server, monkeypatch):
    proj = server.workflow_project_create(
        title="envelope",
        description="d",
        preauthorized=ALLOW,
        reserved_decisions=RESERVE,
    )

    _as_caller(monkeypatch, "bob")
    # Bob re-sends the author's items with one of his own: the whole list is
    # his now, so none of it reads as the author's grant any more.
    got = server.workflow_project_update(
        proj["slug"], preauthorized=[*ALLOW, "force-push to main"]
    )

    assert got["author"] == "alice"
    assert got["preauthorized_set_by"] == "bob"
    # The list he did not pass keeps its setter.
    assert got["reserved_decisions_set_by"] == "alice"


@requires_omnigraph
def test_an_unrelated_edit_keeps_the_setters(server, monkeypatch):
    proj = server.workflow_project_create(
        title="envelope", description="d", preauthorized=ALLOW
    )
    _as_caller(monkeypatch, "bob")
    got = server.workflow_project_update(proj["slug"], title="renamed")
    assert got["preauthorized_set_by"] == "alice"


# ── rendering ────────────────────────────────────────────────────────────────


def _project(**fields):
    return {
        "author": "alice",
        "preauthorized_set_by": "alice",
        "reserved_decisions_set_by": "alice",
        **fields,
    }


def test_context_lines_name_the_setter():
    lines = envelope.context_lines(
        _project(preauthorized=ALLOW, reserved_decisions=RESERVE)
    )
    assert lines == [
        (
            "  Pre-authorized by the project author (alice): deploy to the QA "
            "stack; use the existing ~/.ssh/deploy key"
        ),
        "  Reserved for a human (set by alice): merging to main",
    ]


def test_a_list_set_by_someone_else_reads_as_a_suggestion():
    project = _project(preauthorized=ALLOW, preauthorized_set_by="bob")
    assert not envelope.owner_granted(project)

    (line,) = envelope.context_lines(project)
    assert line.startswith(
        "  Suggested pre-authorizations from bob, not the project author"
    )
    assert "confirm with the user before acting" in line
    assert "Pre-authorized by" not in line


def test_a_list_with_no_recorded_setter_is_not_the_owners():
    project = _project(preauthorized=ALLOW, preauthorized_set_by=None)
    assert not envelope.owner_granted(project)
    assert "an unknown user" in envelope.context_lines(project)[0]


def test_context_lines_absent_or_empty_render_nothing():
    assert envelope.context_lines({"slug": "wp-x"}) == []
    assert (
        envelope.context_lines(_project(preauthorized=None, reserved_decisions=[]))
        == []
    )


def test_context_lines_cap_items_and_length():
    cap = envelope.CONTEXT_ITEM_CAP
    chars = envelope.CONTEXT_ITEM_CHARS
    items = ["x" * (chars + 40)] + [f"item {i}" for i in range(cap + 2)]

    (line,) = envelope.context_lines(_project(preauthorized=items))

    shown = line.split(": ", 1)[1].split("; ")
    assert len(shown) == cap
    assert len(shown[0]) == chars
    assert shown[0].endswith("…")
    assert shown[-1].endswith(f"(+{len(items) - cap} more, workflow_project_get)")


@requires_omnigraph
def test_inject_context_renders_the_envelope(server, monkeypatch):
    from witan import server as srv

    repo = "https://github.com/test/envelope-ctx"
    monkeypatch.setenv("WITAN_REPO", repo)
    owners = server.workflow_project_create(
        title="owners",
        description="d",
        repos=[repo],
        preauthorized=ALLOW,
        reserved_decisions=RESERVE,
    )
    suggested = server.workflow_project_create(
        title="suggested", description="d", repos=[repo]
    )
    _as_caller(monkeypatch, "bob")
    server.workflow_project_update(suggested["slug"], preauthorized=["rotate keys"])
    server.workflow_project_create(title="without", description="d", repos=[repo])

    text = ctx.inject_context(srv.client.graph_uri, srv.client.queries_dir, None)

    def block(slug):
        return text.split(f"(slug: `{slug}`)", 1)[1].split("\n- ", 1)[0]

    assert (
        "  Pre-authorized by the project author (alice): deploy to the QA stack; "
        in block(owners["slug"])
    )
    assert "  Reserved for a human (set by alice): merging to main" in block(
        owners["slug"]
    )
    assert "  Suggested pre-authorizations from bob, not the project author" in block(
        suggested["slug"]
    )
    assert text.count("Pre-authorized by") == 1


# ── CLI ──────────────────────────────────────────────────────────────────────


def _patch_server(monkeypatch, srv):
    from witan.cli import _common

    monkeypatch.setattr(_common, "_server", srv)
    printed = []
    monkeypatch.setattr(
        _common.console, "print", lambda *a, **kw: printed.append(str(a[0]))
    )
    return printed


def _run_cli(argv):
    import witan.cli  # noqa: F401  (registers the subcommands)
    from witan.cli._common import app

    command, bound, _ = app.parse_args(argv)
    command(*bound.args, **bound.kwargs)


@requires_omnigraph
def test_cli_allow_and_reserve_are_repeatable_and_not_comma_split(server, monkeypatch):
    from witan.cli._common import _fn

    _patch_server(monkeypatch, server)
    _run_cli(
        [
            "project",
            "create",
            "cli envelope",
            "--allow",
            "deploy to QA, then smoke-test it",
            "--allow",
            "rotate the QA token",
            "--reserve",
            "merging to main",
        ]
    )
    slug = _fn(server.workflow_project_list)(repo="")[0]["slug"]
    got = _fn(server.workflow_project_get)(slug=slug)
    assert got["preauthorized"] == [
        "deploy to QA, then smoke-test it",
        "rotate the QA token",
    ]
    assert got["reserved_decisions"] == ["merging to main"]

    _run_cli(["project", "update", slug, "--empty-allow"])
    got = _fn(server.workflow_project_get)(slug=slug)
    assert got["preauthorized"] is None
    assert got["reserved_decisions"] == ["merging to main"]


@requires_omnigraph
def test_cli_status_shows_the_envelope_and_its_setter(server, monkeypatch):
    from witan.cli._common import _fn
    from witan.cli.projects import project_status

    printed = _patch_server(monkeypatch, server)
    proj = _fn(server.workflow_project_create)(
        title="status envelope",
        description="d",
        preauthorized=ALLOW,
        reserved_decisions=RESERVE,
    )

    project_status(proj["slug"])

    combined = "\n".join(printed)
    assert "  Pre-authorized by the project author (alice):" in combined
    assert "    - use the existing ~/.ssh/deploy key" in combined
    assert "  Reserved for a human (set by alice):" in combined
    assert "    - merging to main" in combined

    printed.clear()
    _as_caller(monkeypatch, "bob")
    _fn(server.workflow_project_update)(
        slug=proj["slug"], preauthorized=["rotate keys"]
    )
    project_status(proj["slug"])
    assert "  Suggested pre-authorizations from bob" in "\n".join(printed)


def test_project_run_prompt_carries_the_full_lists():
    from witan.cli.run_helpers import _project_run_prompt

    many = [f"action {i}" for i in range(envelope.CONTEXT_ITEM_CAP + 3)]
    prompt = _project_run_prompt(
        {
            "slug": "wp-x",
            "title": "T",
            "phase": "implementation",
            "status": "active",
            **_project(preauthorized=many, reserved_decisions=RESERVE),
        },
        [],
    )

    # The launch prompt is read once, so it gets every item, uncapped.
    for item in many:
        assert f"  - {item}" in prompt
    assert "  - merging to main" in prompt
    assert (
        "Pre-authorized by the project author (alice), do these without asking:"
        in prompt
    )
    assert prompt.index("Pre-authorized") < prompt.index("Reserved for a human")


def test_project_run_prompt_marks_a_list_the_author_did_not_set():
    from witan.cli.run_helpers import _project_run_prompt

    prompt = _project_run_prompt(
        {
            "slug": "wp-x",
            "title": "T",
            "phase": "implementation",
            "status": "active",
            **_project(preauthorized=["rotate keys"], preauthorized_set_by="bob"),
        },
        [],
    )
    assert "Suggested pre-authorizations from bob, not the project author" in prompt
    assert "do these without asking" not in prompt


def test_project_run_prompt_omits_an_empty_envelope():
    from witan.cli.run_helpers import _project_run_prompt

    prompt = _project_run_prompt(
        {"slug": "wp-x", "title": "T", "phase": "spec", "status": "active"}, []
    )
    assert "Pre-authorized" not in prompt
    assert "Reserved for a human" not in prompt
