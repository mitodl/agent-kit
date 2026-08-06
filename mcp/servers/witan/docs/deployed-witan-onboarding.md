# Pointing your CLI and agent at the deployed witan

How to stop using your local `~/.local/share/witan/graph.omni` and start using
the shared, deployed service — so your agent sessions read and write the same
graph as everyone else's.

This is one half of the cutover. The other half is
[migrating the history you already have](migration-runbook.md#local--shared-the-cutover);
they should be sequenced together, because a store you keep writing to after
its export was taken is a store whose tail nobody will merge.

## What you are switching to

| | Local (default) | Deployed |
|---|---|---|
| Store | `~/.local/share/witan/graph.omni` on your disk | one shared graph in the cluster |
| Reached via | the `omnigraph` binary, directly | an MCP call to `witan[.<env>].ol.mit.edu` |
| Identity | your `author` string, honour-system | a Keycloak JWT → `act-<sub>` → your own omnigraph token |
| Authorization | none | Cedar policy bundles, per actor |
| Who else sees it | nobody | the team |

The switch is opt-in and per-config: with `remote_url` unset the CLI runs
exactly as it does today. See
[ADR-0005](adr/0005-secure-cli-path-into-deployed-witan.md) for the design.

## Prerequisites

- `witan` on `PATH` (`uv tool install witan-council`), version new enough to
  have `witan login` — check with `witan login --help`.
- A Keycloak account in the `ol-platform-engineering` realm, enabled. The
  hourly `witan-token-sync` job mints an actor entry for every enabled realm
  user, so if you can log in to other OL services you almost certainly already
  have one.
- A browser you can reach from wherever you run the CLI. The device-code flow
  is designed for this to work over SSH — you approve on any device.

## 1. Configure the target

Add a `[targets.*]` block to `~/.config/witan/config.toml`. A named target is
better than exporting env vars: it scopes the deployment to the repos it
actually covers, and it routes **both** `witan` and `witan-code` at once (they
share one endpoint and one token cache — there is no separate
`WITAN_CODE_REMOTE_URL`).

```toml
[targets.ol]
remote_url  = "https://witan.ol.mit.edu/mcp"
oidc_issuer = "https://sso.ol.mit.edu/realms/ol-platform-engineering"
oidc_audience = "witan"
match_orgs  = ["mitodl"]
```

Swap the hostnames for `witan.ci.ol.mit.edu` / `sso-ci.ol.mit.edu` to try it
against CI first, which is the recommended way in. `oidc_client_id` defaults to
`witan-cli`, the public client registered for the device grant; you only set it
if that changes.

`match_orgs` is what makes this safe to leave in place: outside a `mitodl`
checkout the target doesn't match, and the CLI keeps using your local store.
`match_paths` (checkout prefixes), `match_repos`, and `match_hosts` are the
other selectors — see the `load()` docstring in `witan/config.py` for the
precedence order. To force a target regardless, `WITAN_TARGET=ol witan …`.

## 2. Log in

```bash
witan login
```

This runs the OIDC device authorization grant: it prints a URL and a user code,
you approve in a browser, and the resulting token is cached at
`~/.config/witan/tokens.json` (mode `0600`), keyed by `(issuer, client_id)` so
several deployments don't clobber each other. It refreshes automatically; you
should not need to run this again until the refresh token expires.

```bash
witan whoami
```

Confirm the endpoint, your username, and — the part worth actually reading —
your `actor`. It is `act-<keycloak-sub-uuid>`, **not** `act-<username>`. That
uuid is what appears in the Cedar policy logs and in the `actor-tokens` map, so
it is the string to quote when asking why a write was refused.

`witan logout` clears the cached token.

## 3. Verify reads, then a write

```bash
witan tasks --all-repos | head       # a read through the deployment
witan memory "cedar" --all-repos     # BM25 search, server-side
```

If these return the team's data rather than yours alone, the read path works.
Then check a write actually lands — this is the step that exercises the whole
ADR-0004 chain (JWT → actor → that actor's own omnigraph bearer token → Cedar):

```
memory_store(kind="lesson", title="onboarding probe", content="delete me")
```

…from an agent session, then `witan memory show <slug>` to confirm, and
`memory_delete` to clean up. A write that returns a Cedar denial rather than a
slug means your actor has a token but no policy grant — quote the `act-…` from
`witan whoami` when reporting it.

## 4. Point your agent at it

The MCP server your agent launches reads the same `config.toml`, so once step 1
is in place, `witan setup --agent claude` (or `pi`/`copilot`/`opencode`/`kilo`)
is all that is needed — no separate MCP-level configuration. Re-run `witan
setup` after upgrading.

Verify from inside a session: the context hook's output should show tasks and
projects that other people created.

## What happens when the deployment is unreachable

**It hard-fails. There is no fallback to your local store, by design.**

A configured-but-unreachable remote surfaces as a connection error from the
command you ran; the CLI does not quietly serve you a different graph. This is
deliberate — a silent fallback would split the corpus in two, writing some
sessions' work to the shared graph and some to a local one with no signal that
it happened, and the two would then have to be reconciled by a merge that
nobody knew to run.

So an outage means witan commands fail while it lasts, and your agent's context
hook comes back empty rather than stale. If you need to keep working offline,
that is a config change you make deliberately: comment out `remote_url` (or
`WITAN_TARGET=` a local target) and know that anything you write then lives in
a separate store, to be merged later with
[`witan migrate merge`](migration-runbook.md).

Related: `witan login` failing with an expired refresh token looks similar but
is not an outage — re-run `witan login`.

## Troubleshooting

- **"Remote mode is not configured."** No `remote_url` resolved: your target
  didn't match this repo. Check with `witan whoami` from inside the checkout,
  or force it with `WITAN_TARGET=ol`.
- **A remote URL is configured but no OIDC issuer.** The CLI refuses to fall
  through to the unauthenticated in-process path — set `oidc_issuer` on the
  same target, or unset `remote_url`.
- **401 / token rejected.** The deployment validates the `aud` claim. If your
  realm's audience mapper is not stamping `aud: witan`, set `oidc_audience` to
  match the deployment's `WITAN_OIDC_AUDIENCE`.
- **Your writes are refused but reads work.** Cedar. Human actors get `change`
  on the memory graph and on their own code-graph branch views, but *not* on a
  code graph's protected `main` — that one is CI's, and the refusal is
  deliberate.
- **`witan migrate …` is refused.** Correct: schema and store maintenance have
  no per-user identity to scope, so they run in-cluster as `svc-witan-admin`
  (ADR-0005 path b). See the
  [migration runbook](migration-runbook.md#local--shared-the-cutover).
- **witan-code went remote and you didn't want it to.** Expected — one endpoint
  serves both tool surfaces, so the four `remote_*`/`oidc_*` keys route both
  CLIs. Indexing stays local either way (it needs your checkout); only reads
  move. ADR-0005's 2026-07-31 amendment explains the coupling.

## References

- [ADR-0005](adr/0005-secure-cli-path-into-deployed-witan.md) — the CLI's
  remote MCP-client mode (path a) and the in-cluster admin path (path b).
- [ADR-0004](adr/0004-keycloak-jwt-per-user-actor-mapping.md) — JWT → actor →
  token mapping, i.e. what `witan whoami`'s `actor` line is showing you.
- [ADR-0007](adr/0007-local-to-shared-store-migration-transport.md) /
  [migration runbook](migration-runbook.md) — the data half of the cutover.
- ol-infrastructure `docs/adr/0009-…` — the deployment this connects to.
