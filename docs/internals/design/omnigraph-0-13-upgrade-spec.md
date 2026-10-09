# omnigraph 0.13 upgrade: storage format 14 cutover — spec

Status: accepted (spec phase)
Project: `wp-omnigraph-0-12-upgrade-and-feature-adoption-for--497108`
Repos: agent-kit, ol-infrastructure
Precedent: [omnigraph-0-11-upgrade-spec.md](omnigraph-0-11-upgrade-spec.md)

Every claim marked "verified" was run on 2026-10-07 against the released
0.13.0 binary (`omnigraph 0.13.0`, `internal-schema 14`, linux-x86_64 tarball
sha256 `0123daa64f0f2db9860284200bd71c79a22a6ad2c6f5501b6fca24cac86a7a30`). The
runs used local `file://` scratch clusters built from this repo's policy
fixture (`mcp/servers/witan/policy/`), and the 0.11.0 binary where a
comparison matters. Nothing was run against S3 roots or the deployed clusters.
Facts carried over from the 0.12.0 discovery (2026-10-05/06) and not re-run are
marked "0.12 discovery".

## Goal

Move witan-core, witan-council, witan-code, the omnigraph-server image and the
ol-infrastructure omnigraph stack from omnigraph 0.11.0 (storage format 9) to
0.13.0 (format 14) in one rebuild. Then adopt the features that remove our
workarounds. This project originally targeted 0.12.0, and D1 explains the move
to 0.13.0.

As with 0.11, `omnigraph upgrade` refuses cluster-managed roots. Every deployed
graph is exported with 0.11 and loaded into a fresh format-14 root with 0.13,
and commit history and branch topology are not carried over. That costs us
little: council uses no omnigraph branches, and nothing in witan reads past
commits. Local standalone stores can use the offline in-place
`omnigraph upgrade` (v9 to v14 keeps branches and history). No mixed fleet:
CLI, server and clients move together, and rollback is the backed-up format-9
root with the 0.11 image.

## D1. Target 0.13.0, skip 0.12.0 (decided)

0.12.0 refuses to change an applied policy bundle once the ledger has
resources. Our entrypoint re-renders `witan-users` membership on every boot, so
under 0.12 a user added after a graph was created could never be granted
access to it (D2). Upstream fixed this in 0.13.0 (ModernRelay/omnigraph#878,
closing our #882), released 2026-10-07. 0.13 keeps 0.12's storage format (14),
lock model and topology. Relative to 0.12 it changes:

- `Omnigraph-Http-Api` must be exactly `0.13`. **Verified**: no header and
  `0.12` both get `400 {"code":"api_contract_mismatch"}`. Public `/healthz`
  and `/readyz` need no header (verified: `/readyz` without it is 200), so
  kubelet probes are unaffected. Only protected routes need it.
- HTTP `/read`, `/change`, `/ingest` and `/schema/apply` are removed, along
  with CLI `read`, `change`, `ingest`, `check`, `query lint`, `query check` and
  `export --jsonl`. agent-kit already uses `/graphs/<id>/query`,
  `/graphs/<id>/mutate`, `query`/`mutate`/`load`/`lint` and plain `export`
  (`witan_core/omnigraph.py`, `omnigraph_http.py`). The exception is
  `schema apply` against a remote store (D7).
- Served apply supports graph creation, physical graph deletion, and policy,
  provider and Blob-rule changes. Removing a graph declaration and applying
  now **deletes its storage and history**. `cluster.yaml` edits that drop a
  graph are destructive from 0.13 on.
- `cluster import` and `cluster refresh` remain gone (0.12 discovery).
  **Verified**: a fresh direct `cluster apply` on an empty root creates the
  ledger by itself.

Do not merge the Renovate PR for 0.12.0 (agent-kit #459). The pin bump to
0.13.0 is the last code step (Sequencing).

## D2. Policy-membership freeze: resolved upstream (decided)

**Verified on 0.13.0**: with a server running, an actor with a token but no
group (`act-carol`) is denied (`403 policy denied action 'read'`). We then
added the actor to `witan-users` in every bundle. `cluster plan --server` and
`cluster apply --server`, as an actor holding `config_manage` at cluster
scope, completed with `active: true, in_progress: false`, and the same server
pid then answered the actor's read with 200. A direct apply of a policy-only
change on an existing graph (server stopped, lock released) also completes.

Bearer tokens are still read once at boot. **Verified**: a token added to
`OMNIGRAPH_SERVER_BEARER_TOKENS_FILE` after start gets
`401 invalid bearer token` (upstream #434, open). So the hourly token-sync
still has to restart the server for a new user, and the restart path is where
membership is rendered (D4). `--oidc-identity-trust`, whose admissions refresh
from a local file without a restart, is the route to removing that restart. It
is evaluated after the cutover (D9).

## D3. Lock handoff on restart (decided)

0.12 and 0.13 persist an exclusive lock at `<root>/__cluster/lock.json`
(`lock_id`, `operation`, `created_at`, `pid`; no host or pod). **Verified on
0.13**: after a clean SIGTERM the server logs `v2 cluster admission retained
after shutdown` and the lock stays (`operation: serve`). The next start is
refused with `state_lock_held` until
`omnigraph --cluster <root> cluster force-unlock <id>`. Every direct write
leaves its own lock: `cluster apply`, including a no-op, leaves `deployment`,
and `optimize` and `cleanup` leave `graph_operation`. Read the lock with
`omnigraph cluster status --config <dir> --json` →
`state_observations.{locked, lock_id, lock_operation, lock_age_seconds}`.
Upstream says the lock "is not native-I/O fencing" (crate
`omnigraph-cluster`, `admission.rs`): it only coordinates processes that ask
for it.

Upstream's rule is to unlock only after the prior owner's graph and control
I/O is terminal. A stopped pid alone is not proof, but a process that has
exited has no I/O left. The question is whether the prior owner has exited.
Token-sync rotations, config-hash rollouts, liveness kills, evictions and
node drains all restart the single pod, so the handoff has to be automatic.

`strategy: Recreate` is not that evidence. It orders pods only during a
rollout. On a pod delete, an eviction, a node drain or a lost node, the
ReplicaSet creates the replacement immediately while the old pod is still
Terminating and may still be committing for up to its 30-second drain. A
`/readyz` request through the Service cannot see that pod either, because a
Terminating pod has already left the endpoints. The entrypoint therefore asks
the Kubernetes API:

1. List the pods that match the Deployment's selector, Terminating pods
   included, and count every other pod as live unless its phase is `Failed`
   or `Succeeded`. Those phases mean its containers have exited. An evicted
   pod stays behind in `Failed` until pod garbage collection, so waiting for
   deletion would never finish. `Unknown` (a lost node) counts as live, as it
   does in Kubernetes' own Recreate rollout. Unlock only when no other live
   pod exists. Otherwise wait, up to its termination grace plus a margin,
   then exit 1 so the kubelet retries. A live pod object goes away only after
   its containers have exited, or after a forced delete. The runbook forbids
   `kubectl delete pod --force` on this Deployment, which is the one way to
   break that.
2. A liveness kill restarts the container in place, after the kubelet has
   killed the old process, so the pod list shows only this pod.
   `terminationGracePeriodSeconds` (35) stays above the server's shutdown
   grace (30), so a clean drain finishes inside the pod's lifetime.

The pod check does not exclude the maintenance job (D5), which is not in the
Deployment's selector. Two unlockers are dangerous on their own, too:
upstream `force-unlock` reads the lock, compares the id, then deletes without
a condition, and upstream says operators "must exclude concurrent
force-unlock/release; the backend has no conditional-delete guarantee"
(`omnigraph-cluster` `admission.rs`, `store.rs`). So every process that
force-unlocks first takes one Kubernetes Lease (`coordination.k8s.io`,
`omnigraph-unlock`): the entrypoint, the maintenance job and the runbook. The
entrypoint takes it before the pod check, renews it through the apply, and
lets it expire shortly after `exec`, by which time the server holds its
`serve` lock. If another holder has it, the entrypoint exits 1. The
maintenance job holds and renews it for its whole window.

The Lease excludes other holders only while it is valid. During an API
partition it can expire while its holder is still writing, and a second
process can then take it. So every holder fails closed:

- Renew well inside `leaseDurationSeconds`. Treat ownership as lost as soon as
  no renewal has succeeded within a renew deadline shorter than the duration.
  Having taken the Lease once is not evidence of still holding it.
- Before each `force-unlock`, confirm that the Lease still names this holder
  (`holderIdentity`) and that the last successful renewal leaves more than the
  unlock's own runtime before expiry. If either check fails, do not unlock.
- When ownership is lost, stop before the Lease can expire: TERM the running
  child (apply, optimize, cleanup), wait for it to exit, then exit 1 without
  unlocking, restoring replicas or releasing anything. The duration minus the
  renew deadline must be longer than the slowest child shutdown, so the
  child's I/O is terminal before anyone else can take the Lease.

A lock left behind this way blocks the next unlocker in the normal way:
D3's boot exits 1 on `graph_operation` and on an outstanding `deployment`,
and the runbook clears it. That is the intended fail-closed state.

RBAC cannot restrict top-level `create` by `resourceNames`, so a `create`
grant would cover every Lease in the namespace. Pulumi therefore pre-creates
the `omnigraph-unlock` Lease with no holder and ignores later changes to its
`spec`, so an update does not reset a live holder. Each ServiceAccount that
unlocks gets only `get`/`update` on that named Lease. It takes the Lease with
an `update` carrying the `resourceVersion` it read. The server's
ServiceAccount also gets `list` on pods.

A `replicas: 1` StatefulSet would give the same ordering without the API call,
since it does not create a replacement until the old pod object is gone. It
was not chosen because it changes the resource kind, the Pulumi state and the
rollout path for the sake of one check.

Boot sequence (replaces import/refresh/apply):

```text
take the omnigraph-unlock Lease (else exit 1)
wait until no other live pod matches the selector (else exit 1)
status := cluster status --config $dir --json
if locked:
  serve           -> force-unlock <lock_id>
  deployment      -> no outstanding deployment reported -> force-unlock <lock_id>
                     outstanding deployment -> exit 1 (runbook: reconcile by exact id)
  graph_operation -> exit 1 (a maintenance run owns it; see D5 and runbook)
  any other       -> exit 1 (e.g. deployment_reconcile; runbook)
render group membership (render_groups.py, unchanged)
cluster plan --config $dir --json -> exit 1 if it removes a graph not in
                                     the allow-list (D4)
cluster apply --config $dir --as <admin actor> --json
status := cluster status --config $dir --json   # re-read: apply left a new lock
if locked and lock_operation == deployment: force-unlock <status.lock_id>
exec omnigraph-server "$@"
```

Every `force-unlock` in this sequence runs only after re-confirming the Lease,
under the fail-closed rules above. If the last one is skipped, the next boot
finds a `deployment` lock with no outstanding deployment and releases it.

Because the Lease and the pod check come first, the `deployment` branch can
only meet a lock whose owner has exited: an earlier boot of this Deployment that crashed
between apply and unlock. **Unverified**: which status field reports an
outstanding deployment. The entrypoint task pins it against the binary by
killing an apply mid-run.

This sequence is for 0.13 only. On 0.11, apply releases its lock on exit and
`force-unlock` fails on a missing lock, so the rewritten entrypoint lands in
the same change as the pin bump (Sequencing).

## D4. Deploy-time apply: entrypoint direct apply only (decided)

The pre-deploy `cluster apply` Job (`data_tier.py`, `omnigraph-cluster-apply-*`)
writes the storage root directly while the server is serving. From 0.12 on,
servers and direct CLI writers take the same exclusive admission, and direct
apply requires an ownership transfer before serving (upstream
`docs/user/deployment.md` § Writer topology; 0.12 discovery). Verified on 0.13:
`optimize`, which takes the same admission, is refused beside a running
server. Two models were considered:

- **Chosen: entrypoint direct apply only.** The Job no longer applies. Its
  job (converge the baked schemas and `cluster.yaml` before serving) is
  already done by the entrypoint in D3's boot sequence, under the lock the
  entrypoint just took over, with membership rendered from the live token
  map. One writer and one apply path, and no admin bearer token in a Job. The
  Deployment keeps `ol.mit.edu/config-hash` (cluster.yaml + image digest) on
  its pod template, so a `cluster.yaml` edit still restarts into a converged
  config, as today.
- Not now: a Job running `cluster apply --server` with an admin token and a
  `config_manage` grant. It would stop `cluster.yaml`-only changes (a new repo
  graph) from bouncing council. But the restart path still has to render
  membership while #434 is open, which leaves two apply paths to keep
  consistent. Revisit it with the OIDC evaluation (D9).

The pre-deploy Job stays, as a read-only gate. Today the Deployment
`depends_on` the apply Job, so a failing apply fails the Pulumi update while
the old pod keeps serving. Moving the apply into the entrypoint loses that:
`Recreate` stops the old pod first, so a bad schema or policy change would
become a full outage. Keep the Job, but have it run
`cluster plan --config <dir>` with the new image against the served root.
Upstream says plan observes without taking the writer lock, so the Job is not
a second writer. It refuses drift, migration restrictions and policy errors
before the old pod stops, and runs the graph-removal guard below early.

Consequences:

- `server.policy.yaml` needs no `config_manage` grant for the cutover. Direct
  apply authorizes as `storage_owner` (verified: `authority.kind` in the
  receipt).
- The `migration_armed` special case for the Job stays. A plan against the
  old-format root fails the same way the apply did.
- Real schema migrations now run inside the server's startup-probe budget,
  which is 135s today (`data_tier.py`, `initial_delay 20 + 23 × 5`). That
  budget was sized for a converge that is a no-op at about 1.2s per graph. If
  the boot outlives it, the kubelet kills the apply mid-run and D3's boot
  refuses the resulting outstanding deployment on every retry. Size the
  budget as the D3 waits (Lease, plus up to 35s for an old pod) plus the
  converge plus a representative migration timed on the largest graph before
  the cutover. Record the measured times here.
- Because removing a graph from `cluster.yaml` now deletes it (D1), the
  generated `cluster.yaml` must never drop a graph by accident. For example,
  a repo dropped from `build_cluster_graphs` deletes that repo's code graph on
  the next restart. The guard has to run in the entrypoint, not only in the
  plan Job. Pulumi updates the `omnigraph-cluster-config` ConfigMap before the
  Job runs, and a failed Job does not roll it back. The next restart from
  token-sync, a liveness kill or an eviction would then apply the new
  `cluster.yaml` and delete the graph. So the entrypoint runs `cluster plan`
  before `apply` and exits 1 if the plan removes a graph not in an explicit
  allow-list. Stack config supplies the allow-list to both the Job and the
  entrypoint.

## D5. Maintenance: one stop-window CronJob (decided)

**Verified**: `optimize --cluster <root> --graph <id>` is refused with
`state_lock_held` while the server's `serve` lock exists, and `cleanup` takes
the same admission. There are no HTTP maintenance routes. The nightly
`omnigraph-optimize` (`20 3 * * *`) and weekly `omnigraph-cleanup`
(`20 4 * * 0`) CronJobs therefore fail under 0.13 as written.

They are replaced by one CronJob (`concurrencyPolicy: Forbid`) that owns a
stop window:

1. Skip the run, successfully, if a migration is armed (the Deployment
   carries `ol.mit.edu/migration-armed`) or the Deployment is already at 0
   replicas. Otherwise record the current replica count.
2. Take the `omnigraph-unlock` Lease (D3) and hold it, renewing, until step 5,
   under D3's fail-closed rules.
3. Scale `omnigraph-server` to 0 and wait until no live pod matches its
   selector (D3's rule), then unlock the `serve` lock.
4. For each graph: `optimize`, then force-unlock the `graph_operation` lock it
   left. On the cleanup day, also run `cleanup` with the existing retention
   and unlock after it. The job unlocks only lock ids it created (the CLI
   prints `lock_id=` on stderr), and only after the child command has exited.
5. Restore the recorded replica count only if no migration was armed in the
   meantime (see the failure modes below), then release the Lease.

Shutdown has to restore service too. The container runs `/bin/sh -c` as PID
1, which ignores SIGTERM unless it traps it, and a trapping shell still waits
for the running child before acting. So:

- run each child in the background and `wait` for it;
- trap TERM and EXIT: forward TERM to the child, wait for it, unlock its lock
  (if the Lease is still confirmed), run step 5's conditional restore, release
  the Lease;
- wrap each child in `timeout`, and set `activeDeadlineSeconds` below the
  window, so the deadline is reached by the trap and not by a SIGKILL;
- set the pod's termination grace above one child's shutdown time.

The job runs as its own ServiceAccount: an IRSA role for S3 and a namespaced
Role limited to `get` on the `omnigraph-server` Deployment, `get`/`update` on
its `deployments/scale`, `list`/`watch` on its pods, and `get`/`update` on the
named Lease. Today the maintenance CronJobs reuse the
server's `omnigraph-server` ServiceAccount (`maintenance.py`). Binding the
scale Role there would let the server scale its own Deployment, which is the
hidden privilege `maintenance.py` warns against.

Failure modes:

- If the job pod dies hard (OOM, lost node), no trap runs and the Deployment
  stays at 0 with a `graph_operation` lock left behind. The Lease expires on
  its own. The council probe's first run after the window fails, and that is
  the alert. Losing the Lease mid-window (D3's fail-closed rules) ends the
  same way, except the child has exited cleanly first. Also alert when the Deployment has 0 available replicas outside
  the window. The runbook clears the lock and scales back up.
- If Pulumi scales the Deployment up mid-window, the new pod finds the Lease
  held and exits 1 until the window ends, so it neither unlocks nor serves
  until the job has released the Lease.
- Arming a migration suspends the CronJob but does not stop a running Job,
  and Pulumi does not reconcile `replicas` after it writes them. An
  unconditional restore would bring the server back with a migration armed,
  and the D8 pre-flight would then wait forever. Pulumi therefore sets the
  `ol.mit.edu/migration-armed` annotation on the Deployment in the same update
  that sets `replicas: 0`. The annotation also changes the Deployment's
  `resourceVersion`, even when `replicas` was already 0 because the job had
  scaled it down. At step 5 the job GETs the Deployment. It restores only if
  the annotation is absent and `spec.replicas` is still 0, by PUTting
  `deployments/scale` with the `resourceVersion` it read. An arm that lands
  between the read and the write makes that PUT fail with 409. The job then
  re-reads, finds the annotation and leaves the count at 0. The D8 pre-flight
  still waits for the Deployment to be at 0 replicas with no pods, as well
  as for maintenance Jobs to finish.

The window is a nightly outage for every witan user. Schedule: keep the 03:20
UTC slot, and the window must close before the 04:00 UTC CI indexer run.
Measure the window on CI, then QA, before Production. The council probe runs
every 15 minutes (`council_probe.py`), and a failed probe Job is the alert
signal. Its 03:30 and 03:45 runs would fail nightly. The probe has no
Kubernetes credentials (`automount_service_account_token=False`), so it
cannot read a window marker. Its schedule skips the hour instead
(`*/15 0-2,4-23 * * *`), which keeps the 04:00 run as the check that the
window ended.

Side effect: optimize no longer runs beside a live server. That removes the
route by which `maintenance.py` saw the pending-recovery wedge on `code-bridge`
(tk-production-code-bridge-graph-is-wedged-on-a-pend-8318a4), and every window
ends in a fresh open.

## D6. Readiness and `--require-all-graphs` (decided: keep unset)

0.13 `/readyz` reports `loading`, `serving`, `degraded`, `blocked` or
`draining`. It is 503 while any graph is loading. After startup it is 200 when
at least one graph is ready or the applied inventory is empty, and 503 when
every graph in a nonempty inventory is unavailable or shutdown has begun
(upstream `docs/user/deployment.md` at v0.13.0; healthy state verified). One blocked code
graph therefore leaves the pod Ready, which keeps the blast-radius decision at
`data_tier.py:194-229`. Leave `OMNIGRAPH_REQUIRE_ALL_GRAPHS` unset and update
that comment from 0.11's `quarantined_graph_count` to the 0.13 states.

New in 0.12/0.13: a blocked graph is not retried until the next restart, and
callers authorized on it get `503 graph_unavailable` (0.11: 404). Unknown
graphs are still 404. The council probe should treat `503 graph_unavailable`
on `council` as down.

## D7. Consumer compatibility (agent-kit)

These do not depend on D2–D5 and are safe to land on 0.11 first, each behind
the version that needs it:

- **Header.** Send `Omnigraph-Http-Api: 0.13` on every protected request in
  `omnigraph_http.py` and check it on responses. A missing or mismatched
  response header means unknown effects, never a replay. The same applies to
  ol-infrastructure's `check_council_health.py`.
- **DateTime.** 0.13 refuses a DateTime whose fraction has non-zero digits
  past the third. **Verified**: `.123456Z` →
  `invalid DateTime literal ... millisecond precision`; `.123Z`, `Z` and
  `.123+00:00` are accepted. `timeutil.now_iso()` emits microseconds. Truncate
  to milliseconds. Existing data is not affected. **Verified**: 0.11 stores
  and exports DateTime at millisecond precision (a `.123456+00:00` load
  exports as `.123`), and that export loads into 0.13.
- **Commit ids.** **Verified**: the bootstrap commit is a bare ULID, and
  every later write is `hb1.<ulid>.<slot>.<nonce>` with a decimal slot
  (`hb1.X.1`, `.2`, `.3`, ...). String comparison is wrong at the ULID→hb1
  boundary and from slot 10 on. Treat ids as opaque and order by
  `graph_manifest_version` or the response's parent chain instead
  (tk-stop-comparing-graph-commit-ids-as-strings-0-12--03da2d).
- **Errors.** `503 graph_unavailable` for blocked graphs. CLI `--json` errors
  carry `command_outcome` (**verified**:
  `{"execution":"not_started","effects":"none","action":"refresh"}` on a
  refused mutation), and the CLI gains exit code 75 (0.12 discovery). Classify on `command_outcome` and exit code, not stderr text.
- **Remote schema apply.** `witan/graph.py` `apply_schema` runs
  `omnigraph schema apply --schema <file> --server <url> --graph <id>` for a
  remote store. In 0.13 `schema apply` accepts only standalone storage
  (release notes; `omnigraph-cli` `client.rs`), so `witan migrate schema` and
  `witan migrate all` against the deployed tier would fail. That includes the
  break-glass path in ol-infrastructure `witan/break_glass.py`. Make the
  remote path refuse with a pointer to the entrypoint apply (D4), and update
  the break-glass runbook.
- **Retention.** `cleanup --keep N` counts graph commits (0.12 discovery).
  Update help text and wrappers.
- **Policy fixture.** `mcp/servers/witan/policy/cluster.yaml` declares the
  graph `code_example`. **Verified**: 0.13 `cluster apply` accepts it but
  `omnigraph-server` refuses to start (`graph_id must match
  ^[a-zA-Z0-9-]{1,64}$`). Deployed ids use hyphens. Rename the fixture graph
  to `code-example`. `check.sh` also drops `cluster import`.

## D8. Storage-format migration Job (ol-infrastructure)

`scripts/migrate_storage_format.py` keeps its shape: export with 0.11, init
and load into a parallel `fmt<N>` prefix with 0.13, verify, then cut over.
This is upstream's "same graph ID in a parallel cluster root". Changes:

1. Drop `cluster import` (`migrate_storage_format.py:997`). Direct
   `cluster apply` creates the ledger.
2. Release the lock between direct CLI steps. Apply leaves a `deployment`
   lock, and each `load` and each `optimize --store` in `build_indexes`
   (`migrate_storage_format.py:1055`) leaves a `graph_operation` lock. The
   Job is the only owner of a root nobody serves yet, so it unlocks the ids it
   created after each step. Graph operations print `lock_id=` on stderr.
   Direct apply prints `Admission lock: <id>`, and reading
   `cluster status --json` after any step works for both. It must leave no
   lock behind, or the server's first boot refuses it (D3).
3. The pre-flight (`writer_blockers`) also waits for the Deployment to be at
   0 replicas with no live pods (D3's rule; D5). The single maintenance CronJob replaces the
   two in `storage_migration.py`'s writer list.
4. No export rewrite. **Verified**: a 0.11.0 export (keyed edge with id
   `["src","dst"]`, top-level `id`, millisecond DateTime) loads into 0.13.0
   unchanged and re-exports identically. Upstream's `jq` identity relocation
   applies only to older exports.

Still needs a CI run of the patched Job with the new image against a copy of
a real format-9 CI root (S3, real volumes, our policy files under
`cluster validate`): tk-patch-the-storage-format-migration-job-for-fmt9--bbde00.

## D9. After the cutover

Each is its own task under the feature-adoption epic and blocked on the
cutover: GQ 2.1 compound `where`/`or`/`in`, correlated `count{}`/`exists{}`,
exact receipts for post-write verification, re-measuring the write ceiling
(read replicas remain unsupported, so throughput work stays in one process),
and `--oidc-identity-trust`. If OIDC removes the token-sync restart,
re-evaluate served apply (D4) at the same time.

## Sequencing

1. Before the cutover, in any order: D7 consumer fixes (agent-kit), safe on
   0.11; the D8 Job patch and its CI dry run (ol-infrastructure); the D6
   comment update; and timing a schema migration for the startup budget (D4).
   The D3 entrypoint rewrite (agent-kit) only works against 0.13: on 0.11
   the unlock after apply fails on a missing lock, and 0.11 has no persisted
   locks for the maintenance job to unlock. It ships in the 0.13 image, so it
   merges with the pin bump. The D4/D5 stack changes (ol-infrastructure) are
   one Pulumi program for CI, QA and Production, and only CI deploys
   automatically. They can merge early behind a per-environment stack setting
   (for example `omnigraph:storage_format: 14`), the way `migration_armed` is
   gated today. The setting selects the plan Job over the apply Job, the
   stop-window CronJob over optimize and cleanup, and the Lease and pod RBAC.
2. Pin and format bump (agent-kit), the last code step: tag `v0.13.0`, all
   three digests (each hashed from a complete download and matched against
   its published `.sha256`), `_OMNIGRAPH_INTERNAL_SCHEMA = 14`, a format-14
   row in `test_binary_contract.py`. It merges only in the cutover window,
   because merging ships the images. The entrypoint rewrite lands in the same
   change. Each environment's stack setting flips at its cutover.
3. Cutover per environment, CI then QA then Production: suspend the
   maintenance CronJob, witan-ci-indexer and witan-view-reaper; scale to
   zero; back up graph roots and `__cluster`; run the patched Job and read its
   verdict; roll the data tier and clients to 0.13; reindex code graphs.
   Local stores run `omnigraph upgrade` (standalone v9→v14) or
   `witan migrate storage`.
4. After each environment: verify on the live Deployment (image, the lock
   handoff across a `kubectl rollout restart` and a plain `kubectl delete pod`,
   one token-sync restart, one maintenance window including a TERM to the
   maintenance pod mid-run), and confirm the header on the council probe.
