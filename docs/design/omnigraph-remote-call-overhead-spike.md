# Spike: subprocess-per-call overhead for remote omnigraph-server calls

Task: `tk-spike-subprocess-per-call-overhead-for-remote-om-d6ceac`
(project `wp-witan-multi-user-service-deployment-dcf6ee`). Measured 2026-08-05
against omnigraph 0.8.1.

## The question

`OmnigraphClient` shells out `omnigraph query` / `omnigraph mutate` as a
subprocess for every read and write, identically for a local Lance directory and
for a remote `http(s)://` omnigraph-server. Locally a subprocess has to run
anyway. Remotely it is pure overhead on top of the HTTP roundtrip. Is it worth a
second, connection-pooled HTTP backend, or is it noise next to the graph
operation itself?

**Answer: it is not noise — but only once the store is compacted.** On a healthy
store the subprocess wrapper is ~78-81% of every read. On a fragmented store the
graph operation is 20x more expensive and the subprocess disappears into it.
Compaction is worth far more than the transport change, and the transport change
only pays off after compaction is reliable.

## Method

One local `omnigraph-server` 0.8.1 in the deployed shape — `--cluster` boot,
bearer tokens via `OMNIGRAPH_SERVER_BEARER_TOKENS_FILE`, and the real
`mcp/servers/witan/policy/` Cedar bundles applied — serving a `council` graph
populated through witan's own client. Three paths compared on the same server:

| path | what it measures |
| --- | --- |
| **A** `omnigraph --version` | fork/exec + binary startup floor, no graph work |
| **B** `OmnigraphClient.read/change` | today's path: subprocess + HTTP + graph op |
| **C** `POST /graphs/<id>/{query,mutate}` | keep-alive `HTTPConnection` per thread, same query text and params |

B and C are **interleaved inside one timed run** (alternating per iteration) so
both see the same graph size, cache state and competing load. An earlier
phase-per-variant design gave the write comparison to whichever variant ran
first, because every insert grows the store.

Concurrency 1 / 4 / 8 threads, mirroring FastMCP worker threads. 40 iterations
per variant per level.

**Loopback removes network RTT.** That makes these numbers an *upper bound* on
the subprocess share: adding real network latency can only shrink the fraction,
never grow it. The load generator also shares a machine with the server, so
absolute latencies are pessimistic under concurrency; the B-vs-C *ratio* is the
result, not the absolute values.

## Results — reads, compacted store (913 Memory rows, 1 fragment)

| workers | CLI p50 | CLI p95 | HTTP p50 | HTTP p95 | subprocess share of p50 |
| --- | --- | --- | --- | --- | --- |
| 1 | 25.9 ms | 31.8 ms | **5.1 ms** | 6.9 ms | **80.5%** |
| 4 | 33.3 ms | 40.9 ms | **6.8 ms** | 9.0 ms | **79.5%** |
| 8 | 45.8 ms | 66.2 ms | **10.7 ms** | 24.0 ms | **76.7%** |

A full-scan read (`list_memories`) tracks the point lookup almost exactly
(27.8 → 5.5 ms at 1 worker, 78-80% share), which is itself the finding: at this
graph size the query is so cheap that scan and point lookup are
indistinguishable, and the wrapper is the whole cost.

The bare spawn floor is only 6.7 ms (1w) to 14.7 ms (8w) — so **the subprocess
cost is roughly 2-3x the raw fork/exec**. The rest is the CLI's own per-invocation
work: config resolution, `.gq` parse, client construction, connection setup with
no reuse. A pooled client skips all of it, not just the fork.

Server-side read concurrency is healthy: HTTP p50 degrades only 5.1 → 10.7 ms
from 1 to 8 workers while throughput rises ~4x. Reads are not the bottleneck.

## Results — writes

| workers | CLI p50 | HTTP p50 | subprocess share | per-variant rps |
| --- | --- | --- | --- | --- |
| 1 | 69.5 ms | 47.6 ms | 31.5% | 7.9 |
| 4 | 643.1 ms | 488.3 ms | 24.1% | 3.4 |
| 8 | 1653.6 ms | 1429.8 ms | 13.5% | 2.5 |

Writes are a different regime. The subprocess share shrinks to 14% at 8 workers
because **write latency is dominated by server-side serialization**: p50 grows
24x from 1 to 8 concurrent writers, and aggregate throughput *falls* (15.8 → 5.0
combined rps). Adding writers makes the system slower in absolute terms.

No HTTP 429s were logged, so this is not the per-actor admission cap
(`OMNIGRAPH_PER_ACTOR_INFLIGHT_MAX` / `_BYTES_MAX`) and witan's blocking
`_admission_cap_backoff` never fired. It is the Lance commit path serializing.
A pooled HTTP client would not fix it.

## The finding that outranks the question: fragmentation

Every single-row `insert` creates a new Lance dataset version. After 913
sequential inserts the `council` store held **912 fragments**, and reads had
degraded catastrophically:

| store state | point read HTTP p50 | point read CLI p50 |
| --- | --- | --- |
| 913 rows, **912 fragments** | 167.0 ms | 196.0 ms |
| 913 rows, **1 fragment** (after `omnigraph optimize`) | **7.7 ms** | **45.3 ms** |

`omnigraph optimize` took 11.8 s and made reads **21x faster**. Row count did
not change. This dwarfs any transport decision, and it reframes the whole spike:

- On a **fragmented** store, the graph operation costs ~165 ms and the
  subprocess is ~5-15% — genuinely noise, which is what the earlier
  un-compacted measurements showed.
- On a **compacted** store, the graph operation costs ~5 ms and the subprocess
  is ~80%.

So "is the subprocess worth removing?" is conditional on the maintenance
CronJobs actually running. This is a direct, measured argument for
`tk-verify-the-omnigraph-maintenance-cronjobs-actual-9a7b68`: if `optimize`
is not running against the deployed S3 stores, every witan read is paying a
20x penalty that no client change can recover.

## Recommendation

1. **Verify and keep the `optimize` CronJob running first.** Highest-value,
   already-built, 20x on reads. Nothing else here matters until it is confirmed.
2. **Then add a pooled HTTP backend for `http(s)://` stores** — a second
   `OmnigraphClient` implementation selected by URI scheme, leaving the
   subprocess path for local paths and `s3://` roots. ~5x lower read latency and
   ~4x lower read p95, on a healthy store. The HTTP surface needs no upstream
   change and is already proven:
   - `POST /graphs/<id>/query` — `{"query": <gq text>, "params": {…}}` →
     `{"rows": […], "columns": […], "row_count": N}`
   - `POST /graphs/<id>/mutate` — same body →
     `{"affected_nodes": <int>, "affected_edges": <int>, "actor_id": …}`
     (independent counts — a single-node `insert_memory` returns
     `affected_nodes: 1, affected_edges: 0`)
   - `GET /graphs` — the graph listing, Cedar-gated on `graph_list`
   - Bearer token in `Authorization`; the server resolves the actor from it,
     exactly as it does for the CLI.
3. **Do not expect it to help writes.** Concurrent-write throughput is a
   server-side serialization limit and needs a different answer (batching
   multi-row mutations into one commit, or upstream work — see
   `tk-omnigraph-conditional-write-cas-precondition-on--94155f`).
4. **Batch writes wherever witan already writes several rows per tool call.**
   One commit per row is what produced the 912-fragment store; fewer, larger
   commits attack the fragmentation and the write ceiling at once.

## Reproducing

The rig is scripted but deliberately not committed — it depends on a scratch
cluster directory and a running server. To rebuild it: boot
`omnigraph-server --cluster <dir>` with a tokens file and the
`mcp/servers/witan/policy/` bundles wired into `cluster.yaml` `policies:`,
populate via `OmnigraphClient.change`, then compare `OmnigraphClient.read`
against `POST /graphs/<id>/query` with variants interleaved in one timed run.
