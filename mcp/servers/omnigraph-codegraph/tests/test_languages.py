"""Extraction coverage for TS/JS/JSX, bash, and yaml."""

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
}


@requires_stack
def test_multilanguage_extraction(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGRAPH_CODEGRAPH_REPO", "https://github.com/test/ml")
    monkeypatch.setenv("OMNIGRAPH_CODEGRAPH_DIR", str(tmp_path / "code"))
    src = tmp_path / "src"
    src.mkdir()
    for name, content in FILES.items():
        (src / name).write_text(content)

    from omnigraph_codegraph import config as cfg_mod
    from omnigraph_codegraph import indexer
    from omnigraph_codegraph import repo as repo_mod
    from omnigraph_codegraph import store as store_mod
    from omnigraph_codegraph.graph import OmnigraphClient

    cfg = cfg_mod.load()
    stats = indexer.index_path(src, config=cfg)
    assert stats.errors == 0
    assert stats.indexed == 4

    client = OmnigraphClient(
        str(store_mod.store_for_repo(repo_mod.detect(), cfg)), cfg.queries_dir
    )

    def kind_of(name: str) -> set[str]:
        return {
            r["kind"] for r in client.read("read.gq", "find_by_name", {"name": name})
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
        "read.gq", "find_by_qualified_name", {"qualified_name": "jobs.build"}
    )
    assert any(r["kind"] == "key" for r in nested)

    # kind filter: "build" is both a bash function and a yaml key
    unfiltered = {
        r["kind"] for r in client.read("read.gq", "search_symbols", {"query": "build"})
    }
    assert {"key", "function"} <= unfiltered
    funcs = client.read(
        "read.gq", "search_symbols_by_kind", {"query": "build", "kind": "function"}
    )
    assert funcs and all(r["kind"] == "function" for r in funcs)
