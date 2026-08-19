"""The message shown when ``witan serve`` cannot reach its deployment.

Its own module because it has to be readable in a place nobody is looking. An
MCP server is started by an agent harness, not by a person at a prompt: stderr
scrolls past inside a client's log pane, and the visible symptom is only that
the witan tools are missing. So the sentence has to carry the whole diagnosis —
what was tried, why it stopped, and both ways forward — rather than assume
somebody will go and read the docs.
"""

from __future__ import annotations

from ..config import RemoteConfig

__all__ = ["remote_serving_needs_stdio", "remote_startup_failure"]


def remote_startup_failure(remote: RemoteConfig, exc: BaseException) -> str:
    """Explain a refusal to start, naming the endpoint and the way out.

    Deliberately does NOT classify ``exc`` into categories of its own. The
    proxy has already turned an unreachable host, an expired session and a
    rejected credential into sentences written for exactly this reader, so
    re-deriving a category here would either contradict them or paraphrase
    them worse. What is added is the part the proxy cannot know: that this is
    startup, so nothing is running, and that the fallback the caller might
    expect is refused on purpose.
    """
    source = remote.url_source or "the configured remote URL"
    return (
        f"witan serve: cannot reach the deployed witan at {remote.url} "
        f"(from {source}), so no tools are being served.\n"
        f"\n"
        f"  {exc}\n"
        f"\n"
        "witan does not fall back to your local store. Serving it here would "
        "split this agent's memory from the graph your `witan` commands read, "
        "with no signal that it happened — writes would accumulate on this "
        "machine and only surface as a merge nobody knew to run.\n"
        "\n"
        "Either restore the connection (`witan whoami`, then `witan login` if "
        "the session has expired), or work against the local store on purpose "
        "by selecting a target that declares one — e.g. WITAN_TARGET=work."
    )


def remote_serving_needs_stdio(remote: RemoteConfig, transport: str) -> str:
    """Explain why a deployed target may only be re-served over stdio.

    Names the credential rather than the rule. "stdio only" on its own reads
    like an arbitrary restriction somebody will work around with a reverse
    proxy; what actually matters is that this server authenticates every
    forwarded call with ONE cached token — the token of whoever started it —
    and has no inbound authentication to decide who is asking.
    """
    source = remote.url_source or "the configured remote URL"
    return (
        f"witan serve: refusing to serve {remote.url} over {transport!r}.\n"
        f"\n"
        "Re-serving a deployed witan is stdio-only. Every call this process "
        "forwards is authenticated with the OIDC token of the user who started "
        "it, and this process authenticates nobody on the way in — so on a "
        "socket it would let anyone who can reach the port act as that user, "
        "with none of the per-caller identity mapping the deployment itself "
        "does.\n"
        "\n"
        f"Either drop --transport (stdio is the default, and is what an agent "
        f"harness uses), or unset {source} to serve your local store over "
        "HTTP on purpose."
    )
