"""Richer symbol attributes: signatures (params/return), docstrings, decorators."""

from .conftest import requires_stack

PY = '''import functools


@app.route("/x")
def handler(
    req,
    timeout: int = 5,
) -> Response:
    """Handle it."""
    return ok()
'''

TS = """/** Adds two. */
export function add(a: number, b: number): number {
  return a + b
}

class W {
  /** Render. */
  @Input()
  render(): void {}
}
"""

R = "https://github.com/test/enrich"


def _fn(tool):
    return getattr(tool, "fn", tool)


@requires_stack
def test_signature_docstring_decorators(tmp_path, monkeypatch):
    monkeypatch.setenv("WITAN_CODE_DIR", str(tmp_path / "code"))
    from witan_code import config as cfg_mod
    from witan_code import indexer
    from witan_code import server as srv

    monkeypatch.setattr(srv, "cfg", cfg_mod.load())
    srv._clients.clear()

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text(PY)
    (src / "b.ts").write_text(TS)
    indexer.index_path(src, repo_override=R, config=srv.cfg)

    # Python: multi-line signature with param + return types, docstring, decorator.
    h = _fn(srv.code_find_definition)("handler", repo=R)[0]
    assert "timeout: int = 5" in h["signature"]
    assert "-> Response" in h["signature"]
    assert h["docstring"] == "Handle it."
    assert h["decorators"] == ['@app.route("/x")']

    # TS function: typed signature + JSDoc (no decorator).
    add = _fn(srv.code_find_definition)("add", repo=R)[0]
    assert "a: number" in add["signature"] and add["signature"].endswith(": number")
    assert add["docstring"] == "Adds two."
    assert not add["decorators"]

    # TS method: JSDoc resolved past the decorator; decorator captured.
    r = _fn(srv.code_find_definition)("render", repo=R)[0]
    assert r["docstring"] == "Render."
    assert r["decorators"] == ["@Input()"]
