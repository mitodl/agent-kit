"""An id for the client process calling witan, for joining its log lines.

A deployed witan cannot tell which agent session a call came from unless the
agent passes ``session_id``, and on ``memory_get`` and ``recall`` they almost
never do: in the 2026-11-12 to 2026-11-23 production window, 430 of 435 read
lines had a null session, and the other 5 were the measuring session's own.
Nearly all ``memory_get`` calls come through witan-core's remote proxy, which
negotiates MCP 2026-07-28, where there is no transport session to fall back on
either (each request is its own connection).

The proxy runs one process per agent session (``witan serve`` under an agent),
so an id fixed for the process stands in for the session. It travels as a
request header, not a tool argument, so it never reaches a tool's schema or a
task claim's holder. A launcher sets :data:`CLIENT_INSTANCE_ENV` in the
environment of the agent it starts, so the agent's own proxy sends the same id
the launcher logged its surfacing under.

Only ever logged. A client sets the header to whatever it likes, so this
attributes calls to a client process, never to a person.
"""

from __future__ import annotations

import os
import uuid

__all__ = [
    "CLIENT_INSTANCE_ENV",
    "CLIENT_INSTANCE_HEADER",
    "client_instance",
    "new_client_instance",
]

CLIENT_INSTANCE_ENV = "WITAN_CLIENT_INSTANCE"
CLIENT_INSTANCE_HEADER = "x-witan-client-instance"


def new_client_instance() -> str:
    """A fresh client instance id."""
    return uuid.uuid4().hex


_PROCESS_INSTANCE = new_client_instance()


def client_instance() -> str:
    """This process's client instance id.

    :data:`CLIENT_INSTANCE_ENV` when a launcher set one, else an id generated
    once per process.
    """
    return os.environ.get(CLIENT_INSTANCE_ENV) or _PROCESS_INSTANCE
