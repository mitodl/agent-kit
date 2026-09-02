<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit mcp/servers/witan/docs/deployed-witan-onboarding.md instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`mcp/servers/witan/docs/deployed-witan-onboarding.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/docs/deployed-witan-onboarding.md).

# Pointing your CLI and agent at the deployed witan

How to stop using your local `~/.local/share/witan/graph.omni` and start using
the shared, deployed service — so your agent sessions read and write the same
graph as everyone else's.

This is one half of the cutover. The other half is
[migrating the history you already have](migration-runbook.md#local-shared-the-cutover);
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
[ADR-0005](../explanation/decisions/0005-secure-cli-path-into-deployed-witan.md) for the design.

## Prerequisites

- `witan` on `PATH` (`uv tool install witan-council`), version new enough to
  have `witan target` — check with `witan target --help`. That is the newest
  of the commands below (witan-council 0.11.0), so a CLI that passes this
  check has all of them; checking `witan login` instead would let an older
  CLI through, to fail at step 1 with an unknown command.
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

**This puts your code graphs on the cluster too.** `--remote-url` implies
`code_transport = "mcp"`, which is what makes a branch you index visible to
anyone else — that is the whole point of indexing per writer (ADR-0005/0006).
Scanning still happens locally, because it needs your checkout; `code_transport`
decides only where the resulting index is *written*. You cannot write a code
graph's `main`, which belongs to the CI indexer; what you get is your own branch
view.

Pass `--code-transport direct --code-server <url>` instead for a writer that
already shares the cluster network (the CI indexer, maintenance jobs). `direct`
is the global default because those in-cluster writers are the ones who need it;
from a laptop it fails as an unreachable host, which is why a `--remote-url`
target overrides it.

`witan target list` shows what is configured and marks with `*` the target in
effect for the current checkout; `witan target remove ol` deletes the block
again. To change one setting on a target you already have, use
`witan target set ol --<key> <value>`, which touches only the keys you name —
not `add --force`, which rebuilds the block from its flags and so drops
everything you did not re-type. See [Already registered?](#already-registered).

<details>
<summary>What that writes, if you would rather edit the TOML by hand</summary>

```toml
[targets.ol]
remote_url = "https://witan.ci.ol.mit.edu/mcp"
oidc_issuer = "https://sso-ci.ol.mit.edu/realms/ol-platform-engineering"
oidc_audience = "witan"
code_transport = "mcp"
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
itself, so it is only ever reached explicitly. Name it with `--target ol` on
any command, or export `WITAN_TARGET=ol` for a whole shell — `--target` wins
over the env var, which wins over `match_*` auto-detection.

`--target` is an app-level option, so it works on every command and in either
position (`witan --target ol tasks` and `witan tasks --target ol` are the
same). `witan migrate merge` additionally takes `--from <name>`/`--to <name>`,
which name the two ENDS of a merge rather than the one target a command runs
against; its destination-by-URI flag is `--target-uri`, deliberately not
`--target`, because it takes a store address rather than a configured name.

Selector precedence is by **specificity, not file order**: every target's
`match_paths` is checked before any `match_repos`, then `match_hosts`, then
`match_orgs` (`witan_core.target_config.match_target`). A `match_paths` target
at the bottom of the file therefore beats a `match_orgs` target at the top;
position only breaks ties *within* one tier.

### Already registered?

`target add` only grew `--code-transport` on 2026-09-01. Blocks written before
that have no `code_transport` key, so they fall through to the global `direct`
default and every branch you have indexed since is sitting on your own machine —
working, findable by you, invisible to everyone else. Nothing reports this as a
failure, which is exactly why it is worth checking.

Print your own target's block and look for `code_transport` in it — a bare
`grep` over the file will happily match some *other* target's key and tell you
you are fine when you are not:

```bash
awk '/^\[targets\.ol\]/{f=1;print;next} /^\[/{f=0} f' ~/.config/witan/config.toml
```

No `code_transport` line in that block? Add it:

```bash
witan target set ol --code-transport mcp
```

That is the whole procedure. `set` changes the keys you name and nothing else,
rewriting each where it sits, so the rest of the block — including any comment
in it — comes through untouched. `--dry-run` shows the amended block first.

**Do not use `add --force` for this.** `add` builds the block from the flags it
is given, so replacing a block that way deletes every key it has no flag for.
`token`, `model`, `code_dir`, `code_token`, `index_role` and `actor` are all
readable from a target block and none of them are `add` parameters, so a
`--force` re-register drops all six — plus any flag you did not re-type.

Your existing login survives: the token cache is keyed on `(oidc_issuer,
oidc_client_id)` (`DeviceAuth._cache_key`), and this changes neither.

Then confirm the graphs you want are actually there:

```bash
witan code doctor
```

Cluster code graphs are **declared by provisioning**, not created by the client
— `managed_repos` in ol-infrastructure
`src/ol_infrastructure/applications/omnigraph/Pulumi.<env>.yaml`, read by
`applications/omnigraph/data_tier.py`. `doctor` lists the ones your target can
reach, by repo. As of 2026-09-01 production serves 14 — `agent-kit`,
`learn-ai`, `lehrer`, `mit-learn`, `mitxonline`, `mitxpro`, `ocw-hugo-themes`,
`ocw-studio`, `odl-video-service`, `ol-concourse`, `ol-data-platform`,
`ol-django`, `ol-infrastructure`, `open-edx-plugins` — plus the shared
`code-bridge`.

If a repo you work in is not on that list, indexing it fails with
`ClusterGraphMissing` rather than quietly writing somewhere else. Adding one
needs a `pulumi up` plus an `omnigraph cluster apply` and a server restart, so
raise it before you switch rather than after.

Re-index whatever you want shared afterwards: the local index is not migrated,
and `witan code index` writes wherever the config now points.

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

…from an agent session, then `witan memory --kind <kind>` to confirm, and
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

A configured-but-unreachable remote fails the command you ran, naming the
endpoint and saying so out loud:

```
The deployed service at https://witan.qa.ol.mit.edu/mcp could not be reached:
Client failed to connect: All connection attempts failed. witan does not fall
back to your local store — falling back silently would split your memory across
two graphs with no signal that it happened, leaving a merge nobody knew to run.
Check the endpoint is reachable and that your session is still valid (`witan
whoami`, then `witan login`), or unset `remote_url` on target [qa] to work
against your local store on purpose.
```

The CLI does not quietly serve you a different graph. This is deliberate — a
silent fallback would split the corpus in two, writing some sessions' work to
the shared graph and some to a local one with no signal that it happened, and
the two would then have to be reconciled by a merge that nobody knew to run.

`witan-code` prints the same shape for its own reads, with its own reason: an
answer with no hits from a stale or absent local index is indistinguishable
from a true "nothing calls this".

So an outage means witan commands fail while it lasts, and your agent's context
hook comes back empty rather than stale. If you need to keep working offline,
that is a config change you make deliberately: comment out `remote_url` (or
`WITAN_TARGET=` a local target) and know that anything you write then lives in
a separate store, to be merged later with
[`witan migrate merge`](migration-runbook.md).

Related: `witan login` failing with an expired refresh token looks similar but
is not an outage — re-run `witan login`.

## What happens when the deployment is busy

Not the same thing, and the difference matters: the service is up, and it is
writes — not reads — that are scarce. The shared graph serialises them at
roughly one every 3-4 seconds, so a burst of concurrent writers queues.

You may see either of two answers, and they say different things:

```
omnigraph mutate was refused before it was sent: 4 writes are already in flight
against https://.../council and no slot freed within 10s. … NOTHING WAS
WRITTEN — retry once the burst clears.
```

That one is clean. It happened before anything left the client, so the graph is
untouched and retrying is unambiguous.

```
The deployed service at https://… answered HTTP 502 for `memory_store`: the
request reached it and was cut off before a reply came back. `memory_store`
writes, so ITS OUTCOME IS INDETERMINATE — the write may or may not have been
applied … Re-read before retrying; retrying blind writes it twice if it did land.
```

That one is not. The call was cut at the deployment's 30-second deadline with
the write already in flight, and nothing in the reply says whether it committed
— measured live, most such writes had committed and some had not. **Re-read
before you retry.** `witan migrate merge` is the exception: it reconciles
newest-record-wins, so re-running it is safe and its message says so.

Reads are unaffected by all of this and stay fast under the same load.

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
  silently overwrite. `--force` replaces the block in place, keeping its
  position — which matters for ties, since within one selector tier the first
  matching target wins. (Across tiers, specificity decides; see step 1.) Or
  pick another name.
- **"could not be reached" but the endpoint is definitely up.** The same
  message covers a token the *server* rejects, because both fail while the
  connection is being opened and the client cannot tell them apart from
  outside. Check `witan whoami` first — an expired session, or a missing `aud`
  claim (below), reads identically to an outage.
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
  [migration runbook](migration-runbook.md#local-shared-the-cutover).
- **`witan migrate merge --target-uri …` is refused.** Against a deployment the
  target is the deployment's own graph, resolved server-side. Name the
  deployment with `--to <name>` instead, or unset `remote_url` to merge
  between stores you address yourself.
- **witan-code went remote and you didn't want it to.** Expected for *reads* —
  one endpoint serves both tool surfaces, so the four `remote_*`/`oidc_*` keys
  route both CLIs. ADR-0005's 2026-07-31 amendment explains the coupling.
- **Your code graphs are still local.** Check `code_transport` on the block.
  Scanning always happens locally (it needs your checkout), but *where the
  index is written* is `code_transport`'s call: `mcp` writes to the cluster,
  `direct` addresses `--code-server`, and the global default is `direct`, which
  from outside the cluster means a directory on this machine. `witan` warns on
  startup when your memory graph is deployed and your code graphs are not.
  Targets registered before this was wired have no `code_transport` key — see
  [Already registered?](#already-registered) to add it.

## References

- [ADR-0005](../explanation/decisions/0005-secure-cli-path-into-deployed-witan.md) — the CLI's
  remote MCP-client mode (path a) and the in-cluster admin path (path b).
- [ADR-0004](../explanation/decisions/0004-keycloak-jwt-per-user-actor-mapping.md) — JWT → actor →
  token mapping, i.e. what `witan whoami`'s `actor` line is showing you.
- [ADR-0007](../explanation/decisions/0007-local-to-shared-store-migration-transport.md) /
  [migration runbook](migration-runbook.md) — the data half of the cutover.
- ol-infrastructure `docs/adr/0009-…` — the deployment this connects to.
