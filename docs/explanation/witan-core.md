# witan-core

`witan-core` is the shared floor under both MCP servers. You never install it
deliberately — `witan-council` and `witan-code` each depend on it — but it is
worth understanding, because the things that live in it are precisely the things
that *must not differ* between the two servers.

## Why it exists

The two servers were built copy-paste-and-diverge, and for a while carried an
explicit convention: **deliberately duplicated — no cross-package import.**

That held until the shared surface grew. Fixes then had to be applied twice, and
silently drifted when they weren't — including code that is *contractually*
required to stay identical:

- The **repo-key canonicaliser**, which produces the join key linking the memory
  graph to the code graph. Two implementations that disagree about whether
  `git@github.com:mitodl/agent-kit.git` and
  `https://github.com/mitodl/agent-kit` are the same repository do not produce a
  broken build — they produce a graph where half your memories are invisible
  from the other layer.
- The **pinned omnigraph binary version**, which was kept in lockstep across two
  files by a fragile Renovate custom manager. A store written by one version and
  read by another is a strict-format error, not a warning.

`witan-core` reverses the duplication convention for exactly this class of code:
things where "the two copies drifted" is a correctness bug rather than an
inconsistency.

## The invariant

```
witan-core          ← imports neither server
   ↑        ↑
witan-council → witan-code     (`witan` mounts `witan code`; never the reverse)
```

`witan_core` imports **neither** `witan` nor `witan_code`. It is a leaf below
both, which is what preserves the one-directional mount DAG: `witan-council`
optionally mounts `witan-code`, and `witan-code` never imports `witan-council`.

A shared package that imported either server would turn that DAG into a cycle
and make the optional mount impossible.

## Stdlib-only by default

The base package has **no dependencies at all**. Everything heavier sits behind
an extra, so neither server carries weight it does not use:

| Extra | Brings | For |
| --- | --- | --- |
| *(base)* | — | `_detach`, `repo_key`, `timeutil`, `omnigraph`, `maintenance`, `config_file`, `target_config` |
| `cli` | `cyclopts`, `rich`, `agent-config-kit` | CLI scaffolding and the installer's styled output |
| `mcp` | `fastmcp` | The `confirm`/`text` elicitation primitives |
| `remote` | `httpx2`, `fastmcp` | OIDC device auth, token cache, and the MCP-client proxy |
| `observability` | `structlog`, OpenTelemetry | Structured logs and traces |
| `sentry` | `sentry-sdk` | Error reporting, hooked onto the logging chain above |

`observability` is not in the base package because a local stdio session exports
nothing and an OTLP exporter is a lot of weight for a `pip install`.

`sentry` is **additive to `observability`, not an alternative to it**. It hooks
the stdlib logging chain `observability` already sets up rather than introducing
a pipeline of its own, and `telemetry.py` imports
`witan_core.observability.logging`, which imports `structlog` at module scope —
so installing `sentry` alone gets you an ImportError, not a lighter build. Both
servers request both extras. The split exists so a deployment can run structured
logs and traces *without* shipping errors to Sentry, not the other way round.

## What's in it

The pieces most worth knowing about:

**`repo_key`** — `normalise` and `find_git_config`. The cross-layer repo-key
canonicaliser, carrying a golden contract test. This is the highest-stakes
module in the package: its output is the identity of a repository everywhere in
witan.

**`omnigraph.OmnigraphClient`** — the subprocess wrapper around the pinned
`omnigraph` binary, holding the write lock, the retry/repair logic, and the
admission-cap backoff. Each server subclasses it: `witan-council` adds
`apply_schema`, `witan-code` adds branch operations and bulk `load`.

**`target_config`** — the `[targets.<name>]` matching logic, with priority
`match_paths` > `match_repos` > `match_hosts` > `match_orgs`. Each server keeps
its own typed target model, because they carry different override fields
(`server`/`graph`/`token` for witan-council, `code_dir` for witan-code), and
calls into this shared matcher, which is structurally typed over just the four
`match_*` lists.

**`config_file.load_toml`** — why both CLIs read one file. `witan` and
`witan code` load the same `~/.config/witan/config.toml`, so a single
`[targets.<name>]` block configures both at once.

**`remote/`** — the OIDC device-authorization grant, the shared token cache, and
the MCP-client proxy. This is the client stack behind `witan login`, and the
reason logging in once covers both CLIs.

**`observability/`** — structlog configuration plus OpenTelemetry, with the OTel
halves imported defensively so an install that wants structured logs without an
exporter still works.

Also present: `_detach.popen_detached` (cross-platform detached subprocess
spawning, used by the throttled background `optimize`), `omnigraph_install` (the
single source of the pinned binary version), `maintenance` (the
stamp/interval/due mechanics for that throttle), `elicit`, `caching`,
`chunking`, `identity`, and `timeutil.now_iso`.

CLI scaffolding is **shared**, not local: `witan_core.cli` provides `make_app`,
`resolve_author`, and `report_install`, and both servers import them. What stays
local to each server is its own commands and setup behaviour — the surface that
is genuinely different between a coordination graph and a code index.

## The version-floor trap

This is the one thing that will bite you when contributing.

The uv workspace resolves `witan-core` **by path**, so a server always imports
whatever is in your checkout. An external install resolves it **by version
range** from PyPI. Those two disagree the moment you add a `witan_core` symbol
and use it in a server without raising that server's floor:

```toml
# mcp/servers/witan/pyproject.toml
"witan-core[cli,remote,observability,sentry]>=0.25,<1",   # ← this floor
```

Everything passes locally and in CI. Then `pip install witan-council` resolves a
`witan-core` that does not export the symbol, and the server cannot even import.

**If you add a `witan_core` symbol in the same change as its caller, raise that
caller's floor in that same change.** Nothing automated catches this — the
workspace's path resolution is precisely what hides it. The comments on those
pins record how often it has already happened: five times in `witan-council`,
three in `witan-code`.

The floors track what a package actually *imports*, not what was released
alongside it. `witan-code`'s floor was deliberately **not** raised for
witan-core's trace-context work, because nothing in `witan-code` imports
`trace_context_middleware` — raising it would have bought no behaviour and
misstated the minimum.
