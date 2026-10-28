# omnigraph 0.14 upgrade: storage format 16 cutover (spec)

Status: draft (spec phase). D3 is open and reserved for tmacey.
Project: `wp-omnigraph-0-14-upgrade-and-feature-adoption-for--bdf583`
Repos: agent-kit, ol-infrastructure
Precedent: [omnigraph-0-13-upgrade-spec.md](omnigraph-0-13-upgrade-spec.md),
[omnigraph-0-11-upgrade-spec.md](omnigraph-0-11-upgrade-spec.md)

Claims were checked on 2026-10-28 in three ways, and each is labelled with the
one it got:

- "Verified (binary)": run against the released v0.14.0 linux-x86_64 tarball
  (sha256 `02bde3e3a2c6c8cf50dc9739a8b1041df87cfeaa6ab59f620e1d8e32acffd4d9`,
  matching the published `.sha256`). Only `omnigraph version` and
  `omnigraph version --json` were run.
- "Verified (source)": read in ModernRelay/omnigraph at the v0.14.0 tag
  (`60bfd19`) unless another ref is named. The 0.13 to 0.14 upgrade guide,
  `docs/user/operations/upgrade-0.14.md`, landed after the tag (#1144), so it
  is cited at main `dc4a79d`.
- witan claims cite this repo at `07898579`.

Nothing was run against S3, a served cluster, or the deployed tiers.

Release notes: <https://github.com/ModernRelay/omnigraph/releases/tag/v0.14.0>.

## Goal

Move witan-core, witan-council, witan-code, the omnigraph-server image and the
ol-infrastructure omnigraph stack from omnigraph 0.13.0 (storage format 14) to
0.14.x (format 16). Then adopt the 0.14 features that remove witan-side
workarounds.

Unlike 0.11 and 0.13, this is not a rebuild. 0.14 serves format 14 graphs in
place. The work is in addressing and credentials. Three 0.14 changes stop the
deployed tier as we run it today:

1. The CLI accepts only absolute local `file://` direct targets, so
   `optimize`, `cleanup`, `cluster force-unlock` and the entrypoint's
   `cluster status/plan/apply` can't reach the S3 cluster root (D3).
2. A server target must be HTTPS unless it's an anonymous loopback server.
   The data tier is plain `http://omnigraph-server.<ns>` with bearer auth, so
   every CLI call witan makes against it is refused (D4).
3. The CLI no longer reads `OMNIGRAPH_BEARER_TOKEN`, which is how witan sends
   a different actor's token on each subprocess (D5).

## D1. Target 0.14.0 (decided)

Verified (binary): `omnigraph version` prints

```text
omnigraph 0.14.0
internal-schema 16 (serves v14 to v16)
http-contract 0.14
commit 60bfd19db780fdff3a5178f959c1204143420114
```

and `omnigraph version --json` reports `"http_contract": "0.14"`,
`"internal_schema": {"writes": 16, "serves": [14, 16]}`, `"channel": "stable"`
and `"protocols": {"target": 2, "credential": 2, "access": 2}`.

There is no 0.14.x patch release. Main is 19 commits past the tag (GitHub
compare `v0.14.0...main`) and already carries `version = "0.14.1"`
(`crates/omnigraph-cli/Cargo.toml:3` at `dc4a79d`). None of those commits
touches target addressing: `scope.rs` and `operator_schema/validation.rs` are
identical to the tag (D3). If a 0.14.1 ships before the
pin bump, re-read its notes and take it, because nothing in this spec depends
on 0.14.0 specifically.

## D2. Storage format 16 and the one-way stamp (decided)

Verified (source): `INTERNAL_MANIFEST_SCHEMA_VERSION = 16` and
`MIN_SUPPORTED_INTERNAL_SCHEMA_VERSION = 14`
(`crates/omnigraph-catalog/src/migrations.rs:155,163`). Normal open serves 14
to 16, and a branch's first publish under 0.14 stamps it 16
(`migrations.rs:157-162`). One release-note entry says format 15
(`v0.14.0.md:227`), but a later one says the release retains "graph storage
format 16" (`v0.14.0.md:846`). The tag writes 16, and the binary agrees.

That gives us:

- No rebuild and no storage-migration Job run. `omnigraph upgrade` is only
  for standalone stores that need an explicit stamp, and witan's local stores
  get stamped by their first write anyway.
- A one-way stamp per branch. Once a 0.14 write lands, 0.13 binaries refuse
  that branch.
- A one-way lock record on S3 even without a write. A 0.14 server's S3 lock is
  a version-3 leased record, and on clean shutdown it is rewritten as a
  released version-3 record rather than deleted. Older binaries refuse it
  (`v0.14.0.md:755` and `:891-895`). So starting a 0.14 server once, with no
  writes, is enough to stop a 0.13 server from booting on that root.
- Upstream's rule is to upgrade the CLI, server and SDK together and not mix
  versions against one root (`upgrade-0.14.md:11-12`, main).

ol-infrastructure's format guards assume every format change is a rebuild,
and that doesn't fit an in-place upgrade. `validate_image_internal_schema`
(`src/ol_infrastructure/applications/omnigraph/storage.py:262-276`) fails
preview when the image's `edu.mit.ol.omnigraph.internal-schema` label differs
from `omnigraph:internal_schema_version`, and tells the operator to arm a
migration. `validate_internal_schema_version` (`storage.py:161-170`) requires
that number to match the `fmt<N>` in `omnigraph:storage_prefix`. All three
stacks pin `fmt14` and `14` (`Pulumi.CI.yaml:66,74`, `Pulumi.QA.yaml:51,54`,
`Pulumi.Production.yaml:54,57`). Moving the label to 16 fails preview
everywhere, and keeping it at 14 makes the label lie. This needs a decision in
the cutover task (be7663). I propose that the image also carry the range it
serves (e.g. a second label with `14-16`, read from `version --json`), and
that the guard accept an image whose range contains the root's format. Then
`internal_schema_version` records what the root holds (16 after cutover)
while the prefix stays `fmt14`, because renaming the prefix would mean
copying the root. `validate_internal_schema_version` then has to stop
requiring the prefix name and the version to match.

So within one environment, omnigraph-server, witan, witan-council, the
maintenance Job and the CI indexer move together. Environments move one at a
time (Sequencing). Rollback after a 0.14 server has run is a restore of the
whole-root backup, `__cluster/` included, never an image rollback alone.

## D3. Maintenance and boot converge on the S3 root (open, reserved for tmacey)

Verified (source): an inline selection containing `://` that isn't `file://`
is refused with "inline access supports local storage only; configure a server
or managed target" (`crates/omnigraph-cli/src/scope.rs:127-132`), and an
operator-config direct target must be "an absolute local file:// URI without
authority" (`crates/omnigraph-cli/src/operator_schema/validation.rs:365-380`).
The upgrade guide says that "direct-only maintenance does not gain a remote
equivalent" (`upgrade-0.14.md:131-133`, main). The server has no maintenance
route: the 0.14 `openapi.json` has `/cluster/plan`, `/cluster/deployments` and
`/cluster/deployments/{deployment_id}`, and nothing for optimize, cleanup or
unlock. `docs/user/operations/maintenance.md:11,19-21` says the same.

That breaks four callers:

- `docker/omnigraph_maintenance.py:376` runs `optimize`/`cleanup --cluster
  <root> --graph <g>` against S3, then `cluster force-unlock`.
- `docker/omnigraph_server_entrypoint.py:624-670` runs `cluster
  status/plan/apply/force-unlock --config <dir>`, where `cluster.yaml` names
  the S3 root. 0.14 makes `--config` a source directory only, with the
  destination chosen by `--target` (`upgrade-0.14.md:151-158`, main).
- `docker/omnigraph_plan_gate.py` runs the same `cluster plan`.
- Recovery. An outstanding deployment on an S3 root can't be reconciled with
  released tools (ModernRelay/omnigraph#1163). That is a gap in the 0.14 tier
  whatever we pick for maintenance.
- witan's own direct `s3://` stores. A personal target can name an S3 graph
  with `s3_profile`/`s3_region` (`mcp/servers/witan/README.md:108-136`;
  `store_subprocess_env`, `witan_core/omnigraph.py:1343-1345`). The pooled
  transport skips `s3://`, so every call goes to the CLI, and stock 0.14
  refuses the inline `s3://` target. These stores stop working under every
  option below unless the patched CLI also reaches witan's installer.

ol-infrastructure's storage-migration Job
(`src/ol_infrastructure/applications/omnigraph/scripts/migrate_storage_format.py:968`)
is not needed for this upgrade (D2). The next format migration will hit the
same wall.

Upstream state on 2026-10-28:

- #1163 and #1082 are open with no linked PR. On #1163, ragnorc recovered an
  S3 cluster with a CLI built from `7711e4a` plus one change in `scope.rs`: an
  inline `s3://` selection binds `StorageProvider::S3` (region and endpoint
  from `AWS_REGION`/`AWS_ENDPOINT_URL`) instead of `StorageProvider::Local`
  (`scope.rs:137`). `cluster status`, `cluster apply --deployment-id
  --writers-stopped` and `cluster force-unlock` worked on S3 through that
  build. Optimize and cleanup weren't run on it.
- `scope.rs` has not changed since #1118 (2026-10-22, before the tag). So no
  edge build fixes this. The newest is `edge-20261027-790174c`.
- Served maintenance is in design. RFC #1186 ("Served optimization") is a
  draft that covers optimize of `main` only and leaves cleanup, repair and
  history deletion out of scope. Its implementation, #1177, is a draft that
  still implements an earlier protocol. #1175 (Cedar `maintain`/`reclaim`
  actions, which installed policies would have to grant) is a draft.
- The 0.14 lease takeover helps only the server. A restarted server takes
  over its predecessor's S3 lock after a lease and a half of heartbeat
  silence (default lease 30s, `crates/omnigraph-cluster/src/lease.rs:65`, so
  45s; `OMNIGRAPH_CLUSTER_LEASE_SECONDS` changes it), but "CLI writers keep the exact-ID
  unlock" (`v0.14.0.md:748-755`). A CLI optimize still leaves a lock that
  someone has to force-unlock.
- An older binary is not a way out. 0.13 refuses the version-3 lock record
  and every branch stamped 16 (D2).

Options, with what each costs:

- (a) Wait for upstream. Costs no engineering, but there's no date, and served
  cleanup isn't proposed anywhere. The upgrade waits, and so does every QA and
  Production cleanup task.
- (b) Pin an immutable `edge-<date>-<commit>` build that carries a fix. Cheap
  once one exists, since edge assets are pinnable with the same digest flow.
  None exists today. It would also put an unreleased tree on the server and
  the clients, not only on the maintenance path.
- (c) Serve first and use served `cluster plan`/`apply` over HTTP. This covers
  boot converge and the plan gate only. It needs an HTTPS endpoint in front of
  omnigraph-server, which doesn't terminate TLS itself
  (`docs/user/deployment.md:212`), so that means a sidecar or ingress hop plus
  `tls.ca: {mode: append, ...}` for an internal CA. It also needs a token for
  the apply actor. An empty new root still needs a direct apply. It does
  nothing for optimize, cleanup or force-unlock.
- (d) Build a patched binary: v0.14.0 plus the `scope.rs` change. The patch is
  small, and upstream has exercised the engine path for status, apply and
  force-unlock on S3. It costs a Rust build stage in place of the
  digest-pinned tarball, a patch to rebase on each release, and a path
  upstream explicitly does not qualify. Optimize and cleanup on S3 through it
  are unverified.
- (e) A patched binary for maintenance only. This is (d) scoped down: the
  patched `omnigraph` CLI ships only in the omnigraph-server image, for the
  entrypoint, the plan gate and the maintenance Job. `omnigraph-server` and
  the witan images stay on the stock release. The patched CLI only runs with
  the server stopped or before it serves, which is the exclusion D3/D5 of the
  0.13 spec already enforce.

Recommendation: (e). Build the CLI from the v0.14.0 tag plus the `scope.rs`
change. Before the pin bump, qualify it in CI against a copy of a real S3 root
for status, plan, apply, force-unlock, optimize and cleanup. Separately,
comment on #1163 and #1082 as an affected AWS S3 deployment and offer the
change upstream as a maintenance-and-recovery-only S3 direct target (the
"narrower interim" in #1082). Move to (b) when an edge or patch release
carries it, and to served maintenance when #1186/#1177 ship. (c) is worth
doing once the data tier has TLS, because it removes the boot-time CLI, but
it doesn't unblock maintenance and shouldn't gate the upgrade.

(e) leaves witan's direct `s3://` stores unreachable, because the witan
images and `omnigraph_install.py` stay stock. Either witan refuses an `s3://`
store under 0.14 with a message pointing at a local store or a served one,
or the patched CLI also becomes what `omnigraph_install.py` installs, which
is (d). I lean toward refusing, since I know of no shared deployment that
uses a direct S3 witan store. Who uses personal S3 targets today is
unverified, and this is part of the reserved decision.

The full comparison is on tk-decide-how-optimize-cleanup-force-unlock-and-boo-58bab2.

Under (e), `tk-rework-the-omnigraph-server-entrypoint-maintenan-4311a3` keeps
the 0.13 shape:

- `--cluster <root> --graph <g>` becomes `--target <s3 root> --graph <g>`.
- `cluster ... --config <dir>` becomes `cluster ... --config <dir> --target
  <s3 root>`.
- The Lease and pod check stay, because CLI locks are still exact-ID.
- The force-unlock of a previous server's `serve` lock (`release_previous_lock`,
  entrypoint `:744`) should become rare. A clean shutdown now releases writer
  admission (`v0.14.0.md:891`), and a crashed server's lock is taken over by
  its successor. Whether the entrypoint can stop reading a `serve` lock at all
  depends on `cluster status` reporting a released version-3 record as
  unlocked. That is unverified.
- 0.14 adds a new server exit. An S3 owner none of whose heartbeats is
  answered for two thirds of a lease "stops every storage write and exits 2"
  (`v0.14.0.md:753-754`). The entrypoint `exec`s the server, so the exit
  reaches Kubernetes directly, and a restart then waits out the lease and
  takes the lock over. Check that the Deployment's restart policy and the
  startup probe budget allow for that wait (at least 45s with the default
  lease).

## D4. witan stops using the CLI against a deployed server (decided)

Verified (source): a server target's endpoint must be HTTPS. HTTP is allowed
only when the host is loopback and the target's auth is `anonymous`
(`operator_schema/validation.rs:341-356`, with the anonymous flag passed from
`:89-91`). A token-authed `http://omnigraph-server.<ns>.svc.cluster.local`
target is refused when the config loads, and so is a token-authed
`http://localhost`.

The task asked whether the data tier gets TLS or witan stops using the CLI
against it. witan stops using the CLI against it:

- TLS is a new hop. The server doesn't terminate TLS (D3 (c)), so it would
  need a sidecar or ingress, an internal CA distributed to every witan pod,
  and a CA path in the generated operator config. All of that would exist
  only so the subprocess path keeps working.
- The HTTP API already covers every remote verb witan sends over the CLI, and
  0.13 serves all of them. At v0.13.0, `openapi.json` has `POST
  /graphs/{id}/load/ndjson` (query params `branch`, `from`, `mode`), `POST
  /graphs/{id}/export` (body `branch`, `type_names`), `GET
  /graphs/{id}/commits?branch=`, and `branch` on the `query` and `mutate`
  bodies. So these moves can land before the pin bump.
- The pooled transport already handles reads and writes
  (`witan_core/omnigraph_http.py`) and is the default for remote stores
  (`witan_core/omnigraph.py:2339-2385`).

The remote CLI calls that have to move:

- `OmnigraphClient.load_batch` (`witan_core/omnigraph.py:2663`, CLI at
  `:2693`) and `export_to` (`:2610`). This is
  tk-move-witan-s-remote-load-batch-and-export-to-off-d82169, in progress.
  Export is `POST`, not `GET` as that task's description says. 0.14 `load
  --json` drops `uri` (`upgrade-0.14.md:180`, main), and actor fields
  everywhere become `actor_id` (`:179`). witan reads neither.
- witan-code's branched clients. `_http_transport` returns `None` for any
  client whose `_extra_args` adds flags (`witan_core/omnigraph.py:2381-2384`),
  and witan-code adds `--branch` (and `--from main` on load) for its per-user
  views (`witan_code/graph.py:468-482`). `branch_last_write` runs `commit list
  --branch` over the CLI (`witan_code/graph.py:436`). The transport guard was
  written because omnigraph 0.8.1 documented no request-side branch selector.
  0.13 and 0.14 both have one, so the guard should become "send `branch` (and
  `from`) in the request" rather than "fall back to the CLI". This isn't in
  either in-flight task and needs its own task, or a scope extension of d82169.

After both, the `WITAN_OMNIGRAPH_HTTP=0` escape hatch
(`HTTP_TRANSPORT_ENV_VAR`, `witan_core/omnigraph.py:449`) can no longer reach
an `http://` remote under 0.14. It should refuse with a message naming D4,
not fail inside the CLI with a config error. The CLI path stays the path for
local stores, and for an `https://` remote if anyone runs one (D5).

## D5. Per-actor tokens through a generated operator config (decided)

Verified (source): 0.14 removes `--store` and `--server`, and resource
profiles stop being a separate selector: `--profile NAME` becomes `--target
NAME` once the binding is recreated as a target (`upgrade-0.14.md:116-133`,
main). A global `--profile` survives, but only for control-plane profiles
(`crates/omnigraph-cli/src/cli.rs:37`). The CLI
reads no implicit token variable (`upgrade-0.14.md:111-114`, main;
`v0.14.0.md:36`). A token comes only from a target's `auth`, and
`TokenSource::Env { env }` accepts any name matching `[A-Za-z_][A-Za-z0-9_]*`
(`operator_schema/validation.rs:185-195`). `--operator-config FILE` selects
one operator file "without merging" (`crates/omnigraph-cli/src/cli.rs:39-40`).

witan sets `OMNIGRAPH_BEARER_TOKEN` on each subprocess
(`store_subprocess_env`, `witan_core/omnigraph.py:1319-1345`;
`BEARER_TOKEN_ENV_VAR` at `:439`), so each call carries its own actor's token
(ADR-0004). The 0.14 shape that keeps that property, for
tk-replace-store-server-profile-and-omnigraph-beare-60fdc8:

```yaml
version: 1
targets:
  witan:
    kind: server
    endpoint: https://<host>/
    default_graph: <graph id>
    auth: {provider: token, source: {env: OMNIGRAPH_BEARER_TOKEN}}
```

witan writes this once per remote store, from the store URI, into a private
file, and passes `--operator-config <file> --target witan` on each call.
`OMNIGRAPH_BEARER_TOKEN` stays the per-subprocess variable, so per-call
isolation is unchanged and no shared credentials file exists. A config with
no secret in it can be shared between calls. Only the environment differs.

Under D4 this config only serves `https://` remotes. None of our deployed
tiers is one, so the generated-config path is for completeness and local
HTTPS testing. Tests should cover it, but nothing deployed depends on it.

Local stores become `--target /abs/path` (an inline local path needs no
config), or a `kind: direct` target with an absolute `file://` URI. Call sites
that build `--store`/`--server` flags today:

- `store_cli_args` (`witan_core/omnigraph.py:1237-1255`);
- witan's storage-migration helpers, which run the new binary with `--store`
  (`witan/server.py:1804` `snapshot`, `:2147-2156` `load`). The old binary's
  `export --store` at `:2138` stays as is, because that binary is still 0.13;
- tests and fixtures.

The `--profile` at `witan_core/omnigraph.py:1273` is the AWS CLI's
(`aws configure export-credentials --profile`), not omnigraph's, so 0.14
doesn't touch it. The 60fdc8 task description lists it, and it can come off.

Docs and guidance to update:

- `mcp/servers/witan/config/operator-config.example.yaml` (still the RFC-008
  `profiles:` shape, which 0.14 rejects);
- `witan/cli/migrate.py:99,122` (tells users to export `OMNIGRAPH_BEARER_TOKEN`
  for the CLI);
- `witan setup`;
- the witan README and the guides that use `--store`, `--server` or
  `OMNIGRAPH_BEARER_TOKEN`: `docs/guides/migration-runbook.md`,
  `store-quarantine-runbook.md` and `branch-indexing.md`. Most of
  `docs/guides/` is mirrored from the packages, so edit the source file and
  run `just docs-gen`.

witan's pooled transport also falls back to an ambient `OMNIGRAPH_BEARER_TOKEN`
(`_resolve_token`, `witan_core/omnigraph.py:2402`). That fallback is witan's
own HTTP behaviour and keeps working. Its docstring calls it "the CLI's own
documented last resort", which stops being true. Reword it so it says witan
owns the contract.

## D6. HTTP contract 0.14 and dispositions (decided)

Verified (source): `HTTP_API_CONTRACT = "0.14"` and `IF_ACTOR_HEADER =
"omnigraph-if-actor"` (`crates/omnigraph-api-types/src/lib.rs:36,43`). A
server refuses a request without exactly one matching header
(`require_contract`, `crates/omnigraph-server/src/http_contract.rs:88-94`).

witan sends and checks `Omnigraph-Http-Api: 0.13`
(`witan_core/omnigraph_http.py:177,184,437-447,605,679`).
ol-infrastructure's council probe does the same
(`src/ol_infrastructure/applications/omnigraph/scripts/check_council_health.py:133-134`).
A 0.13 client and a 0.14 server refuse each other in both directions, so
there is no mixed window (D2).

Verified (source): every server error body carries `code` and `disposition:
{effects, action}`. `effects` is `none`, `partial` or `unknown`. `action` is
`fix`, `refresh`, `retry`, `recover` or `reconcile`
(`crates/omnigraph-api-types/src/lib.rs:2742-2780`). A write the server can't
settle answers `500 outcome_unknown` with action `reconcile` (`:2305,2420`).
Every 503 that asks for a retry carries `Retry-After` (`v0.14.0.md:71-85`).
The error-code renames in that entry (`precondition_failed` for a failed Blob
`If-Match`, `payload_too_large` and friends in place of `bad_request`) don't
reach witan's classifiers, which key on status and on the markers listed in
D7.

CLI exits for data commands (`upgrade-0.14.md:184-188`, main; `v0.14.0.md:24`):

| Exit | Meaning |
| --- | --- |
| 1 | no effects |
| 2 | partial or unknown effects (was also "usage error") |
| 4 | failed condition |
| 64 | argument-parse refusal |
| 75 | busy, and also a server refusal with `effects: none`, `action: retry` |

`_classify_cli_error` (`witan_core/omnigraph.py:765`) knows only
`_EXIT_RETRY = 75` (`:418`). The pin bump
(tk-bump-the-omnigraph-pin-to-0-14-contract-0-14-sto-e7e5c7) has to read 2 as
`WriteIndeterminate` and 64 as a witan bug (never retried). Until then a 2
falls through to the text markers.

## D7. Features to adopt after the bump (decided, one task each)

- Dispositions in place of prose markers
  (tk-classify-omnigraph-failures-from-the-0-14-dispos-2ae3ca). Wording-based
  classification today:
  - `classify_status`'s message markers (`witan_core/omnigraph_http.py:301-345`);
  - the CLI marker tuples (`witan_core/omnigraph.py:122-212` and `:384-418`):
    `_RETRYABLE`, `_NEEDS_REPAIR`, `_PRECONDITION_FAILED`, `_RECOVERY_REQUIRED`,
    `_FULL_TEXT_REBUILD_REQUIRED`, `_ADMISSION_CAP_MARKERS`,
    `_UNAVAILABLE_MARKERS`, `_CLI_CONTRACT_VIOLATION` and
    `_GRAPH_NOT_YET_SERVING`;
  - group commit's replay-safety decision, `_DEFINITE_REJECTION_STATUSES` and
    `_DATAFUSION_ERROR_PREFIX` (`:465-466`, used at `:2596-2598`). These exist
    because a 0.13 400 didn't say on the wire whether it ran. Replay becomes
    `disposition.effects == "none"`.

  Keep the status fallbacks for non-JSON relays.
- `Omnigraph-If-Actor` on per-actor requests
  (tk-send-omnigraph-if-actor-on-per-actor-requests-so-f4fc67). A token for
  the wrong actor is then refused with `412 actor_mismatch` before graph
  lookup or effects (`omnigraph-api-types/src/lib.rs:37-43`). 412 is already
  terminal in `classify_status` (`omnigraph_http.py:314-320`). Give
  `actor_mismatch` its own kind so it isn't read as a failed `--if-commit`.
- `omnigraph version --json`
  (tk-read-the-binary-s-storage-format-from-omnigraph--ccb7ba) in place of
  `_INTERNAL_SCHEMA_LINE` (`witan_core/omnigraph_install.py:598`) and
  `bin/check_omnigraph_format.py`. Also assert `http_contract` equals
  `HTTP_API_CONTRACT`, so a pin bump that misses one of them fails a test
  instead of a deploy.
- Search fixes (tk-re-test-task-and-memory-search-against-0-14-s-an-80b1fc).
  Full-text functions apply the analyzer as soon as rows are written
  (`v0.14.0.md:1111`), and `bm25()` no longer depends on the query's filters
  for rows written after the index was built (`v0.14.0.md:987`). The task
  tracks these as upstream #909 and #839.
  Both bear on `task_search`, `memory_search` and `workflow_project_search`
  (`mcp/servers/witan/queries/read.gq`). Whether the fix-task-search-bm25 work
  is still needed is that task's result, not this spec's.
- Lock lease and clean restart (D3, last part). A crashed server's S3 lock is
  taken over after 45s, a clean shutdown releases admission, and a definite
  or uncertain storage failure on a served write no longer stops the server
  (`v0.14.0.md:891`, `:950`). The entrypoint takeover code that this makes
  redundant comes out in 4311a3.

## D8. Checked and not applicable (decided)

- T27, full-text calls need `@index`. Every property witan's queries pass to
  `search`/`bm25` is declared `@index`: `Memory.title`/`content`,
  `WorkflowProject.title`/`description` and `Task.title`/`description`
  (`mcp/servers/witan/schema/schema.pg:19-20,154-155,263-264`),
  `qualified_name` (`witan_code/schema/code-schema.pg:32`) and `key_norm`
  (`witan_code/schema/bridge-schema.pg:26,58`).
- `@range` and `@embed`: no witan or witan-code schema declares either.
- Cross-type hop bounds (`T5`): no query in `mcp/servers/witan/queries` or
  `witan_code/queries` uses a `{m,n}` hop bound.
- Branches. witan-council uses no omnigraph branches. witan-code does (D4),
  but 0.14's branch changes don't affect it: unattached branches and
  unpublished commits are opt-in, and the only behaviour change for existing
  callers is that names with surrounding whitespace or control characters are
  refused instead of trimmed (`upgrade-0.14.md:209-210`, main). That witan-code
  never generates such a name is unverified, though view names come from
  actor and git-branch slugs.
- Removed JSON fields. witan doesn't read `load --json`'s `uri`, the old
  `actor` field, or `command_outcome` (grep of `witan_core`, `witan` and
  `witan_code`).

## D9. Data-tier operations carried in from 0.13 and 0.11 (not upgrade work)

These belong to the project but aren't blocked on, or fixed by, 0.14. Every
cleanup item assumes some way to run cleanup against S3, which under 0.14 is
D3.

- Cleanup cost:
  - Production sizing (tk-size-production-s-nightly-omnigraph-cleanup-from-d111ce);
  - QA's optimize-only window (tk-make-qa-s-nightly-omnigraph-maintenance-window-o-b32827);
  - bounding cleanup planning while `__manifest` versions are never deleted
    (tk-decide-how-to-bound-omnigraph-cleanup-planning-w-d9fa8f; upstream #644, #1095);
  - alerting on `_versions` and manifest growth (tk-alert-on-omnigraph-versions-and-manifest-growth--7db12c).
- Optimize OOMs at the 150 MB DataFusion pool
  (tk-omnigraph-optimize-fails-with-datafusion-not-eno-cb866a,
  tk-backlog-cleanup-job-s-optimize-ooms-at-its-150-m-7f8d15). Nothing in 0.14
  addresses it. `query_memory_bytes` governs reads and `load csv`, not
  optimize. tk-re-check-the-optimize-ooms-under-0-14-314326 re-measures after
  cutover.
- witan-ci-indexer: the intermittent QA failures
  (tk-qa-s-scheduled-witan-ci-indexer-runs-fail-interm-41b348) and the missing
  `ttlSecondsAfterFinished` (tk-witan-ci-indexer-cronjob-has-no-job-ttl-so-its-l-80d688
  and tk-give-witan-ci-indexer-s-jobs-a-ttlsecondsafterfi-59aaef, probably
  duplicates).
- Batching hot write paths, since one commit per write caps a graph at about
  0.9 writes/s (tk-batch-the-hot-witan-write-paths-one-graph-commit-bc5b81).
- From 0.11: confirm the reaper's branch delete and cleanup's fork reclaim
  fire (tk-after-the-fmt9-stores-age-past-retention-confirm-025cb9). Its
  cleanup half waits on D3.

## Sequencing

1. Before the bump, safe on 0.13, in any order:
   - D4's HTTP moves (d82169, and the witan-code branched transport);
   - D5's config generation and flag rewrite behind a version check;
   - comments on #1163 and #1082.
2. D3 decision (tmacey). Under (e), build and qualify the patched CLI against
   a copy of a real S3 root in CI, then land 4311a3 on that path.
3. The pin bump (e7e5c7), the last code step. It moves together:
   - `_OMNIGRAPH_VERSION`, `_OMNIGRAPH_RELEASE_TAG` and the digests
     (`witan_core/omnigraph_install.py:56,88`, assets from `:121`);
   - the Dockerfile pins (`just check-omnigraph-pins`);
   - `_OMNIGRAPH_INTERNAL_SCHEMA` 14 to 16 (`omnigraph_install.py:119`) and
     the `edu.mit.ol.omnigraph.internal-schema` labels;
   - `HTTP_API_CONTRACT` (`omnigraph_http.py:184`);
   - the 0.13-worded CLI markers and exit codes (D6);
   - the witan-core floors.

   It merges only in the cutover window, because merging ships the images.
   Merging is a reserved decision.
4. Cutover per environment, CI, then QA, then Production
   (tk-cut-over-ci-qa-then-production-to-omnigraph-0-14-be7663). Production is
   a reserved decision.
   1. Settle pending deployments and uncertain writes with the 0.13 CLI, which
      can still address S3 directly (`upgrade-0.14.md:8`, main).
   2. Suspend the maintenance window CronJob, witan-ci-indexer and the view
      reaper, and scale the writers to zero.
   3. Take a whole-root S3 backup, `__cluster/` included, and verify it
      (object count and bytes against the source prefix).
   4. Land the format-guard change (D2) in the stack. Roll the
      omnigraph-server image, then the witan and witan-council images. Bump the council probe's contract header in the same
      ol-infrastructure change.
   5. Re-run representative reads, a controlled write per graph, and the
      search cases from 80b1fc.
   6. Run one maintenance window (D3 path), including a TERM mid-run.
   7. Confirm with the deploy-verification skill: `omnigraph version --json`
      in the running pods, the branch stamp reading 16 after the first write,
      and a passing council probe.
5. Local stores: a 0.14 witan stamps a local store 16 on its first write. A
   developer who goes back to a 0.13 witan needs a copy taken before that
   write.

Rollback: before the first 0.14 server start, roll the images back. After it,
restore the step-3 backup, including `__cluster/` (D2), and roll the images
back. Writes made under 0.14 are lost.

## Open questions

- D3, reserved for tmacey.
- Whether `cluster status` reports a released version-3 S3 lock record as
  unlocked. That decides how much of the entrypoint's `serve`-lock handling
  can go (D3, last part). Unverified.
- Whether optimize and cleanup run cleanly on S3 through a `scope.rs`-patched
  CLI. Upstream showed only status, apply and force-unlock. Unverified.
- The build time and image size of a CLI-only cargo build in
  `docker/omnigraph-server.Dockerfile`. Unmeasured.
- The shape of ol-infrastructure's format guard for an in-place upgrade (D2).
  Proposed, not decided.
- Whether anyone relies on a direct `s3://` witan store (D3). Unverified.
