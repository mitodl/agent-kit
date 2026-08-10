"""The probe's failure record has to name the layer, not just quote the prose.

Every one of these cases is a shape the DEPLOYED stack actually produced on
2026-08-09 while the write ceiling was being traced. The reason they are worth
tests: `str(exc)` on each of them is the sentence "Server returned an error
response", which is true of an authentication failure, a timeout, a crash and a
gateway 502 alike -- and a whole session went into re-deriving from logs what
the exception was already carrying.
"""

from witan.scripts.concurrency_probe import _error_detail


class _MCPErrorish(Exception):
    """Stands in for mcp.shared.exceptions.MCPError, whose __str__ is `message`."""

    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.data = code, data

    def __str__(self) -> str:
        return self.args[0]


def test_code_is_read_off_the_link_that_carries_it_not_the_wrapper():
    # RemoteUnreachable is what the reader sees; the -32603 lives two links in.
    inner = _MCPErrorish(-32603, "Server returned an error response")
    outer = RuntimeError("The deployed service could not be reached: ...")
    outer.__cause__ = inner

    detail = _error_detail(outer)

    assert detail["error_code"] == -32603
    assert detail["error_chain"][0].startswith("RuntimeError:")
    assert "_MCPErrorish: Server returned an error response" in detail["error_chain"]


def test_the_fault_inside_an_exception_group_is_followed():
    # anyio re-raises transport faults through a group, so __cause__ alone
    # stops at the group and reports nothing about what actually failed.
    inner = _MCPErrorish(-32000, "upstream closed", data={"status": 502})
    group = ExceptionGroup("unhandled errors in a TaskGroup", [inner])

    detail = _error_detail(group)

    assert detail["error_code"] == -32000
    assert detail["error_data"] == "{'status': 502}"


def test_the_coded_error_is_found_when_it_is_not_the_first_group_member():
    # A task group that lost several workers reports them in completion order,
    # so the one carrying the JSON-RPC code is routinely not `exceptions[0]`.
    # Following only the first branch reported nothing and looked like a clean
    # "no detail available" — the failure mode this helper exists to prevent.
    group = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [OSError("connection reset"), _MCPErrorish(-32603, "internal error")],
    )

    detail = _error_detail(group)

    assert detail["error_code"] == -32603
    assert any("OSError" in link for link in detail["error_chain"])


def test_the_groups_own_cause_is_still_walked():
    # anyio raises the group *from* the fault that triggered it, so the code can
    # sit under the group's __cause__ rather than inside it. Stepping into the
    # members and stopping there skipped it.
    group = ExceptionGroup("unhandled errors in a TaskGroup", [OSError("reset")])
    group.__cause__ = _MCPErrorish(-32000, "upstream closed")

    assert _error_detail(group)["error_code"] == -32000


def test_an_http_status_is_recorded_when_the_transport_kept_the_response():
    class _Response:
        status_code = 502

    exc = RuntimeError("gateway said no")
    exc.response = _Response()

    assert _error_detail(exc)["http_status"] == 502


def test_a_plain_failure_adds_no_noise():
    # A lone exception has no chain worth printing; emitting a one-element one
    # would put a redundant `via` line under every single worker failure.
    assert _error_detail(ValueError("nope")) == {}


def test_a_self_referential_cause_terminates():
    exc = RuntimeError("looping")
    exc.__cause__ = exc

    assert _error_detail(exc) == {}
