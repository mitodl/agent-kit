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

## 1. Register the target

`witan target add` writes the config for you. Start against CI, which is the
recommended way in:

```bash
witan target add ol \
    --remote-url https://witan.ci.ol.mit.edu/mcp \
    --oidc-issuer https://sso-ci.ol.mit.edu/realms/ol-platform-engineering \
    --oidc-audience witan \
    --match-orgs mitodl
```

Drop the `ci`/`-ci` from the two hostnames for production. `oidc_client_id`
defaults to `witan-cli`, the public client registered for the device grant; you
only pass `--oidc-client-id` if that changes.

**The issuer is checked before anything is written.** `target add` fetches the
issuer's `.well-known/openid-configuration` and confirms the document advertises
the issuer you gave it, so a typo is an error about the issuer, right here —
rather than a confusing auth failure at step 2, which is where it used to
surface. Pass `--no-verify` if you are registering offline and want to skip the
check.

A named target beats exporting env vars: it scopes the deployment to the repos
it actually covers, survives the shell, and routes **both** `witan` and
`witan-code` at once (they share one endpoint and one token cache — there is no
separate `WITAN_CODE_REMOTE_URL`).

`witan target list` shows what is configured and marks with `*` the target in
effect for the current checkout; `witan target remove ol` deletes the block
again. Re-running `target add` with an existing name refuses rather than
overwriting — pass `--force` to replace it in place.

<details>
<summary>What that writes, if you would rather edit the TOML by hand</summary>

```toml
[targets.ol]
remote_url = "https://witan.ci.ol.mit.edu/mcp"
oidc_issuer = "https://sso-ci.ol.mit.edu/realms/ol-platform-engineering"
oidc_audience = "witan"
match_orgs = ["mitodl"]
```

…and for production, the same block with `witan.ol.mit.edu` /
`sso.ol.mit.edu`. Hand-editing `~/.config/witan/config.toml` still works
exactly as before; the command is a convenience, not a new format.
</details>

`match_orgs` is what makes this safe to leave in place: outside a `mitodl`
checkout the target doesn't match, and the CLI keeps using your local store.
`match_paths` (checkout prefixes), `match_repos`, and `match_hosts` are the
other selectors — see the `load()` docstring in `witan/config.py` for the
precedence order. To force a target regardless, `WITAN_TARGET=ol witan …`.

Note the corollary: a target with **no** `match_*` selectors never selects
itself, so it is only ever reached explicitly — pass `--target ol` to the
commands below, or export `WITAN_TARGET=ol`.

## 2. Log in

```bash
witan login --target ol
```

This runs the OIDC device authorization grant: it prints a URL and a user code,
you approve in a browser, and the resulting token is cached at
`~/.config/witan/tokens.json` (mode `0600`), keyed by `(issuer, client_id)` so
several deployments don't clobber each other. It refreshes automatically; you
should not need to run this again until the refresh token expires.

`--target` is accepted by `login`, `logout`, and `whoami`. Inside a `mitodl`
checkout the `match_orgs` above already selects the target, so you can leave it
off — but it is always correct, and it is *required* for a target with no
`match_*` selectors. (`witan target add --login` runs this step for you
immediately after registering.)

```bash
witan whoami --target ol
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
  didn't match this repo. Check with `witan target list` — if no row is marked
  `*`, nothing matches here. Pass `--target ol`, or force it with
  `WITAN_TARGET=ol`.
- **"Could not verify OIDC issuer …" from `target add`.** The issuer URL is
  wrong, or unreachable from where you ran it. Nothing was written, so just fix
  it and re-run. Note the check also fails if the discovery document advertises
  a *different* issuer than the one you passed — that mismatch is refused
  deliberately (RFC 8414 §3.3), not worked around.
- **A remote URL is configured but no OIDC issuer.** The CLI refuses to fall
  through to the unauthenticated in-process path — set `oidc_issuer` on the
  same target, or unset `remote_url`. `target add` rejects this combination up
  front, so this only comes from a hand-edited config.
- **`target add` says the target already exists.** Deliberate: it will not
  silently overwrite. `--force` replaces the block in place (keeping its
  position, which matters — the *first* matching target wins), or pick another
  name.
- **401 / token rejected.** The deployment validates the `aud` claim. If your
  realm's audience mapper is not stamping `aud: witan`, set `oidc_audience` to
  match the deployment's `WITAN_OIDC_AUDIENCE`.
- **Your writes are refused but reads work.** Cedar. Human actors get `change`
  on the memory graph and on their own code-graph branch views, but *not* on a
  code graph's protected `main` — that one is CI's, and the refusal is
  deliberate.
- **`witan migrate schema`/`topics`/`repo-keys` are refused.** Correct: those
  have no per-user identity to scope, so they run in-cluster as
  `svc-witan-admin` (ADR-0005 path b). **`witan migrate merge` is the
  exception** — it has a per-actor form and is how you bring your own history
  across. See the
  [migration runbook](migration-runbook.md#local--shared-the-cutover).
- **`witan migrate merge --target …` is refused.** Against a deployment the
  target is the deployment's own graph, resolved server-side. Unset
  `remote_url` to merge between stores you address yourself.
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
