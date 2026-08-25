"""Every CLI dispatch path must configure observability.

Without this, a Sentry-worthy `log.error` at a call site reports nothing,
because no client was ever installed. That is half of why the production
cross-repo bridge failed silently for ~15 hours: the site logged at `warning`
AND the process it ran in had no Sentry client at all.

★ THE PATH THAT MATTERS IS THE UMBRELLA ONE. `witan code …` mounts
witan_code's cyclopts App but NOT its meta launcher, so `witan code index` —
which is literally what `docker/witan-ci-index.sh` runs — dispatches through
`witan.cli._launcher` and never touches `witan_code.cli._launcher`. Fixing
only the latter looked right and covered nothing that was actually failing.
"""

import pytest


def _stub_dispatch(monkeypatch, module):
    """Replace the module's `app` so the launcher dispatches nowhere.

    The launcher resolves `app` as a global at call time, so rebinding the
    name is enough — and it keeps the test off every real command's side
    effects while still exercising the launcher body.
    """
    dispatched: list[tuple] = []
    monkeypatch.setattr(module, "app", lambda tokens: dispatched.append(tokens))
    return dispatched


def _record_configure(monkeypatch):
    calls: list[dict] = []
    import witan_core.observability as obs

    monkeypatch.setattr(
        obs, "configure_observability", lambda **kw: calls.append(kw), raising=True
    )
    return calls


def test_the_umbrella_launcher_configures_observability(monkeypatch):
    """`witan …`, including the mounted `witan code index` the CI indexer runs."""
    from witan import cli

    calls = _record_configure(monkeypatch)
    dispatched = _stub_dispatch(monkeypatch, cli)

    cli._launcher("code", "index", ".")

    assert calls, (
        "the umbrella launcher did not configure observability — `witan code "
        "index` is the CI indexer's command, so nothing it logs would reach "
        "Sentry"
    )
    assert dispatched == [("code", "index", ".")]


def test_the_umbrella_launcher_skips_the_otel_instrumentors(monkeypatch):
    """A short-lived CLI should not pay auto-instrumentation startup cost."""
    from witan import cli

    calls = _record_configure(monkeypatch)
    _stub_dispatch(monkeypatch, cli)

    cli._launcher("whoami")

    assert calls[0].get("instrument") is False


# A plain guard, not `pytest.importorskip` in a decorator: that call is
# evaluated at COLLECTION and skips the whole module when it raises, which
# silently took the two umbrella tests above with it.
try:
    import witan_code  # noqa: F401

    _HAS_WITAN_CODE = True
except ImportError:  # pragma: no cover - witan-code is a normal dependency here
    _HAS_WITAN_CODE = False


@pytest.mark.skipif(not _HAS_WITAN_CODE, reason="witan-code not installed")
def test_the_witan_code_launcher_configures_observability_too(monkeypatch):
    """The standalone `witan-code …` binary is a real path as well."""
    from witan_code import cli as code_cli

    calls = _record_configure(monkeypatch)
    dispatched = _stub_dispatch(monkeypatch, code_cli)

    code_cli._launcher("index", ".")

    assert calls
    assert dispatched == [("index", ".")]
