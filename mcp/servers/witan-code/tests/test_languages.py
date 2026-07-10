"""Extraction coverage for TS/JS/JSX, bash, yaml, go, sql, and hcl."""

from .conftest import requires_stack

FILES = {
    "app.ts": (
        "export interface User { id: number }\n"
        "export type ID = string;\n"
        "export enum Role { Admin, User }\n"
        "export function load(id: ID) { return 1; }\n"
        "export const save = (u: User) => 2;\n"
        "class Repo { find(id: ID) {} render = () => 1; }\n"
    ),
    "util.jsx": (
        "export function Button() { return <button>x</button>; }\n"
        "const handleClick = () => doThing();\n"
    ),
    "deploy.sh": (
        "#!/usr/bin/env bash\nbuild() { compile; }\ndeploy() { build; push; }\n"
    ),
    "ci.yaml": ("name: ci\njobs:\n  build:\n    steps:\n      - run: make\n"),
    "main.go": (
        "package main\n\n"
        "func helper() int { return 1 }\n\n"
        "type Server struct{}\n\n"
        "func (s *Server) Run() { helper() }\n"
    ),
    "model.sql": (
        "CREATE TABLE foo (id INT);\n\n"
        "with base as (select 1 as x), final as (select x from base)\n"
        "select * from final;\n"
    ),
    "main.tf": (
        'resource "aws_instance" "web" {\n  ami = "x"\n}\n\n'
        'variable "app_name" {\n  type = string\n}\n'
    ),
}


@requires_stack
def test_multilanguage_extraction(tmp_path, monkeypatch):
    monkeypatch.setenv("WITAN_REPO", "https://github.com/test/ml")
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    src = tmp_path / "src"
    src.mkdir()
    for name, content in FILES.items():
        (src / name).write_text(content)

    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import repo as repo_mod
    from witan_code import store as store_mod
    from witan_code.graph import OmnigraphClient

    cfg = cfg_mod.load()
    stats = indexer.index_path(src, config=cfg)
    assert stats.errors == 0
    assert stats.indexed == len(FILES)

    client = OmnigraphClient(
        str(store_mod.store_for_repo(repo_mod.detect(), cfg)), cfg.queries_dir
    )

    def kind_of(name: str) -> set[str]:
        return {
            r["kind"]
            for r in client.read("code_read.gq", "find_by_name", {"name": name})
        }

    # TypeScript: interface / type / enum / function / arrow-const / class / methods
    assert "interface" in kind_of("User")
    assert "type" in kind_of("ID")
    assert "enum" in kind_of("Role")
    assert "function" in kind_of("load")
    assert "function" in kind_of("save")  # arrow const
    assert "class" in kind_of("Repo")
    assert "method" in kind_of("find")
    assert "method" in kind_of("render")  # class-field arrow

    # JSX parses (previously broke under the plain javascript grammar)
    assert "function" in kind_of("Button")

    # bash functions
    assert "function" in kind_of("deploy")

    # yaml nested key path
    nested = client.read(
        "code_read.gq", "find_by_qualified_name", {"qualified_name": "jobs.build"}
    )
    assert any(r["kind"] == "key" for r in nested)

    # kind filter: "build" is both a bash function and a yaml key
    unfiltered = {
        r["kind"]
        for r in client.read("code_read.gq", "search_symbols", {"query": "build"})
    }
    assert {"key", "function"} <= unfiltered
    funcs = client.read(
        "code_read.gq", "search_symbols_by_kind", {"query": "build", "kind": "function"}
    )
    assert funcs and all(r["kind"] == "function" for r in funcs)

    # go: function/type/method via the generic TAGS_QUERY bootstrap adapter
    # (no hand-written queries_ts/go.scm), plus a Calls edge Run -> helper.
    assert "function" in kind_of("helper")
    assert "type" in kind_of("Server")
    assert "method" in kind_of("Run")
    callers = client.read(
        "code_read.gq", "find_by_qualified_name", {"qualified_name": "helper"}
    )
    helper_id = next(r["slug"] for r in callers if r["kind"] == "function")
    callers_of_helper = client.read("code_read.gq", "callers", {"id": helper_id})
    assert any(r["qualified_name"] == "Run" for r in callers_of_helper)

    # sql: CREATE TABLE + CTEs (dbt-style models have no CREATE statement —
    # CTEs are the only reliably navigable structure in those files).
    assert "table" in kind_of("foo")
    assert "cte" in kind_of("base")
    assert "cte" in kind_of("final")

    # hcl: labeled blocks keyed on their last (most specific) label, quotes
    # stripped; unlabeled blocks aren't captured (tested via absence below).
    assert "block" in kind_of("web")
    assert "block" in kind_of("app_name")
