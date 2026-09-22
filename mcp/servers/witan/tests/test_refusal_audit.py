"""witan's own refusals declare the log-safety their messages actually have.

The equivalent table in witan-core's ``test_observability_middleware.py`` cannot
assert these: that suite runs isolated with only witan-core installed, so its
parametrised cases for ``witan.*`` classes SKIP. A skipped guard is not a guard,
which is the whole failure mode this contract exists to prevent, so the two
server-side entries are asserted here where they are importable.

See ``witan_core.refusal.Refusal.log_safe_message``. If one of these messages
changes, re-read its raise sites and update both the class and this table.
"""

import pytest

from witan.scan.enforce import WriteBlocked
from witan.server import MissingReference

EXPECTED_LOG_SAFE = {
    # A tool name, a node type and the slug the caller asked for.
    MissingReference: True,
    # A field name, a detector id and `Finding.preview` -- masked by the
    # detectors, which build every preview with `masked_preview`.
    WriteBlocked: True,
}


@pytest.mark.parametrize(("cls", "expected"), list(EXPECTED_LOG_SAFE.items()))
def test_refusal_opt_in_matches_the_audit(cls, expected):
    assert cls.log_safe_message is expected


def test_every_witan_refusal_is_in_the_table():
    """A new refusal here has to be read before it can opt in."""
    from witan_core.refusal import Refusal

    def walk(base):
        for sub in base.__subclasses__():
            yield sub
            yield from walk(sub)

    ours = {
        sub
        for sub in walk(Refusal)
        if sub.__module__.startswith("witan.")
        and sub.__dict__.get("log_safe_message") is True
    }
    assert ours <= set(EXPECTED_LOG_SAFE), (
        f"opted in without an audit entry: "
        f"{sorted(c.__name__ for c in ours - set(EXPECTED_LOG_SAFE))}"
    )
