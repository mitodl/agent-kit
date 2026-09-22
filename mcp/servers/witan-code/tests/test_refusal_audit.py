"""witan-code's own refusals declare the log-safety their messages actually have.

Counterpart to ``mcp/servers/witan/tests/test_refusal_audit.py``; see that
module's docstring for why the table in witan-core's suite cannot cover these.

See ``witan_core.refusal.Refusal.log_safe_message``.
"""

import pytest

from witan_code.ingest import IngestRefused
from witan_code.store import ClusterGraphMissing

EXPECTED_LOG_SAFE = {
    # A repo, a graph id and a provisioning hint. The message whose absence
    # made Production's 27% `code_store_views` failure rate undiagnosable.
    ClusterGraphMissing: True,
    # WITHHELD. `_step_field` raises it as `f"...got {value!r}."` over the
    # caller's own step, and two more sites raise `IngestRefused(str(exc))`
    # over an arbitrary upstream exception.
    IngestRefused: False,
}


@pytest.mark.parametrize(("cls", "expected"), list(EXPECTED_LOG_SAFE.items()))
def test_refusal_opt_in_matches_the_audit(cls, expected):
    assert cls.log_safe_message is expected


def test_every_witan_code_refusal_is_in_the_table():
    """A new refusal here has to be read before it can opt in."""
    from witan_core.refusal import Refusal

    def walk(base):
        for sub in base.__subclasses__():
            yield sub
            yield from walk(sub)

    ours = {
        sub
        for sub in walk(Refusal)
        if sub.__module__.startswith("witan_code.")
        and sub.__dict__.get("log_safe_message") is True
    }
    assert ours <= set(EXPECTED_LOG_SAFE), (
        f"opted in without an audit entry: "
        f"{sorted(c.__name__ for c in ours - set(EXPECTED_LOG_SAFE))}"
    )


def test_no_refusal_here_logs_structured_attributes():
    """None of ours declares `log_safe_attributes` yet, and one that does needs
    the same reading as an opt-in. witan-core's
    `EXPECTED_LOG_SAFE_ATTRIBUTES` cannot see this package, so assert it here.
    """
    from witan_core.refusal import Refusal

    def walk(base):
        for sub in base.__subclasses__():
            yield sub
            yield from walk(sub)

    declaring = sorted(
        sub.__name__
        for sub in walk(Refusal)
        if sub.__module__.startswith("witan_code.")
        and "log_safe_attributes" in sub.__dict__
    )
    assert not declaring, f"declares log_safe_attributes unaudited: {declaring}"
