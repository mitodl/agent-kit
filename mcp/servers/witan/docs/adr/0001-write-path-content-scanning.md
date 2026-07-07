# 1. Write-path content scanning & pluggable data governance

- Status: Accepted
- Date: 2026-07-07
- Deciders: witan platform owners
- Tracking: project `wp-witan-write-path-content-scanning-pluggable-data-d2932a`, epic `tk-epic-write-path-content-scanning-pluggable-data--554db3`
- Supersedes: —
- Related: `wp-witan-multi-user-service-deployment-dcf6ee` (multi-user deployment; this ADR is a precondition)

## Context

witan persists free-text authored by agents and users — memory bodies, task
and project descriptions, session summaries, trace outcomes. Agents routinely
paste command output, config fragments, and stack traces into these fields, any
of which may contain secrets (API keys, tokens, private keys) or PII (emails,
phone numbers, SSNs, card numbers). Today there is **no content validation on
the write path** (confirmed: only shape coercion in `_store_memory`; branch
names are deliberately not sanitized per `repo.py:65`). A secret written into a
Memory is embedded, indexed for BM25 search, and shared with every teammate and
future session.

The risk is currently bounded by witan being local-per-user. It stops being
bounded the moment witan becomes a **shared, multi-user, deployed service** (the
sibling project): one user's accidental paste becomes another tenant's data
breach, and the store becomes a compliance liability (secret sprawl, PII at rest
with no deletion story, no audit trail). We want to prevent ingestion at write
time rather than scrub after the fact, and we want other organizations adopting
witan to enforce *their own* detection rules without forking.

### Forces

- **Single interception point exists.** Every persist funnels through
  `OmnigraphClient.change(query_file, query_name, params)` at `witan/graph.py:90`
  (schema DDL goes through `.apply_schema` at `:106`). The CLI does not write
  independently — `cli/_common.py:_fn()` unwraps and calls the same `@mcp.tool`
  server functions. Embedding + persistence happen inside the downstream
  `omnigraph mutate` subprocess (`graph.py:140`), gated by `WITAN_EMBED_ENABLED`.
  Anything run in Python before `change()` returns is strictly upstream of both.
- **Params are field-addressable.** The `params` dict carries every field by
  name (`content`, `description`, `outcome`, `summary`, `resolution`, `title`,
  `name`), so field-level context survives at the choke point.
- **A config-extension pattern is already established.** `RankConfig` +
  `load_rank_config()` (`config.py:39-122`): a frozen Pydantic model sourced from
  `WITAN_RANK_*` env vars and a `[rank]` TOML table with source-attributed
  validation errors, instantiated once at `server.py:59`.
- **No plugin mechanism exists yet.** There is no entry-point group, registry,
  or importlib discovery to hook into.
- **False positives are unacceptable friction.** If the guard blocks legitimate
  writes (author emails, file paths, code snippets that look like secrets),
  agents will disable it. Detection quality and suppression matter as much as
  coverage.
- **Detection rulesets are a maintenance burden** best borrowed, but witan today
  has only four runtime deps (`fastmcp`, `cyclopts`, `rich`, `pydantic`) and
  values staying lean and offline-capable.

## Decision

Add a content-scanning layer on the write path, structured as four decisions.

### D1 — Intercept at the `change()` choke point, not per-tool

Scanning is invoked inside `OmnigraphClient.change()`. For any `query_name`
starting `insert_`/`update_`, a **static `query_name → [free-text field]`
classification map** selects which `params` values to scan; the rest (enums,
slugs, timestamps, edge endpoints) are skipped. `apply_schema` is never scanned.

This guarantees **100% write coverage across every node type** — Memory, Topic,
WorkflowProject, WorkflowSession, WorkflowTrace, **Task**, CodeBranch — with one
integration point. Task and project writes are guarded by exactly the same code
path as memories; there is no separate guard to build or forget. New node types
are covered by adding a map entry, not by wiring a new call site. The map lives
next to the schema so field additions are a single-file change.

### D2 — A `Scanner` protocol + registry as the extension surface

A new `witan/scan/` package defines:

- **`Finding`** — `detector` (id), `category` (`secret` | `pii`), `span`
  (offsets), `severity`, `action`, and a **secret-free preview** (a masked or
  hashed fragment; never the raw match).
- **`Scanner`** — a `Protocol` with `scan(text: str, field: str, node_type: str)
  -> list[Finding]`. `field`/`node_type` let a scanner be context-aware (e.g.
  ignore the `author` field for email detection).
- **`ScannerRegistry`** — assembles active scanners from three sources:
  1. built-in scanners shipped with witan,
  2. `importlib.metadata` entry-points in group **`witan.scanners`**,
  3. config-referenced dotted import paths (`[scan].plugins`).

  The registry honors per-scanner enable/disable and per-scanner mode overrides
  from config.

Entry-points are the primary third-party extension mechanism: another
organization ships a package exposing `witan.scanners` entry-points, installs it
alongside witan, and enables it in config — no fork. This is the core
"adoptable under their own governance policies" requirement.

### D3 — Three enforcement modes, per-category defaults, fail-closed

Each finding resolves to one of:

- **block** — raise a `RuntimeError` from `change()` before the omnigraph
  subprocess runs, with a message naming the field + detector + secret-free
  preview. The write never happens; the agent sees the error and removes the
  value.
- **redact** — replace each finding span with a stable, non-reversible
  placeholder (e.g. `«redacted:aws_key»`), optionally flag the node with a
  redaction-count property, then proceed.
- **warn** — emit an audit event and proceed unchanged.

Defaults: **secrets → block** (fail-closed; a leaked credential must not land),
**PII → redact** (mask the span, keep the surrounding prose useful). `warn` is an
opt-in low-friction rollout mode. If a scanner itself raises, the default is
**fail-closed** (treat as block) so a broken detector cannot silently open the
gate — overridable for availability-sensitive deployments.

**Error and audit messages never echo the matched value.** This is a hard
invariant: the whole point is to *not* propagate the secret, so surfacing it in
an exception or log would defeat the control.

### D4 — Config via `ScanConfig`, policy admin-owned in server mode

A frozen Pydantic **`ScanConfig`** + `load_scan_config()` mirrors
`RankConfig`/`load_rank_config()`: resolve `WITAN_SCAN_*` env > `[scan]` TOML >
defaults, with source-attributed errors, instantiated once at server import.
Surface: `enabled` (default **off** initially, matching the `WITAN_EMBED_ENABLED`
convention, so the feature ships dark and is enabled deliberately), per-category
mode, detector allow/deny, plugin dotted-paths, allowlist config, and the
scanner-error policy.

In a **deployed multi-tenant server**, `WITAN_SCAN_*` env and user-supplied TOML
are client-controllable and therefore untrusted. Policy must be sourced
**authoritatively server-side** and must not be weakenable by a caller;
per-tenant/per-repo overlays compose with the CEDAR authz work in the multi-user
project. (Detailed mechanism deferred to task
`tk-multi-tenant-policy-control-admin-owned-scan-pol-1338d2`.)

### Built-in detectors (default, zero-dependency)

- **Secrets:** high-signal regex (AWS access/secret keys, GitHub
  `ghp_`/`gho_`/`ghs_`/`github_pat_`, Slack `xox[baprs]-`, Google API keys,
  `-----BEGIN … PRIVATE KEY-----` PEM blocks, JWTs, generic
  `password=`/`api_key=`/`secret=`/`token=` assignments) plus a Shannon-entropy
  heuristic for long high-entropy base64/hex strings.
- **PII:** email, phone (E.164/US), US SSN, and credit-card numbers validated
  with the **Luhn checksum** to cut false positives, with field-context
  suppression (skip `author`) and an allowlist.

A vendored engine (`detect-secrets`, `gitleaks` ruleset) is **not** a core
dependency; it is exposed as an optional plugin (decision spike
`tk-decision-spike-built-in-ruleset-vs-vendored-scan-eb8adb`).

### False-positive management

Per-repo/per-field allowlist regexes in `[scan]`, an inline pragma to permit a
specific value (e.g. a trailing `witan: allow-secret` marker), and a salted
value-hash allowlist (never plaintext). Suppressed findings downgrade to
audit-only.

## Options considered

### Where to scan

1. **Per-tool, in each `insert_*`/`update_*` builder in `server.py`.** Richest
   semantic context. Rejected: ~35 call sites, every new tool must remember to
   scan, high drift risk, easy to bypass by writing a new mutation.
2. **At the `change()` choke point (chosen).** One integration point, total
   coverage, future-proof. Slightly less context, recovered via the static field
   map. Small per-write regex cost over short text — acceptable.
3. **Inside omnigraph / a Rust engine hook.** Truly unbypassable and language-
   agnostic. Rejected for now: cross-repo change to the engine, slower iteration,
   no plugin story in Python where our detectors and adopters live. The choke
   point is the pragmatic 95% at a fraction of the cost.
4. **Post-write async sweep + delete.** Rejected: the secret is already embedded,
   indexed, and possibly read before the sweep runs; deletion from a Lance store
   is not a clean unwind. Prevention beats remediation here.

### Detector source

- **Built-in ruleset (chosen default)** — zero deps, offline, we own quality.
- **Vendored (`detect-secrets`/`gitleaks`)** — better coverage, community-
  maintained rules, but a heavier dep and license/packaging questions. Adopted as
  an *optional plugin* so the default stays lean and air-gap-friendly.

### Enforcement default

- **Block everything** — safest, highest friction; risks agents disabling the
  feature. **Warn everything** — lowest friction, fails the core goal. **Chosen:
  per-category (block secrets / redact PII), warn as opt-in** — matches the
  differing cost of a false positive per category.

## Consequences

**Positive**

- Secrets/PII are stopped before embedding + persistence, across all node types,
  through one auditable code path.
- Task and project writes are covered by construction — no separate guard.
- Other orgs extend detection via a documented `witan.scanners` entry-point
  without forking; witan ships a lean, offline default.
- Provides the audit trail and policy-enforcement point the multi-user
  deployment needs.

**Negative / costs**

- Per-write latency for scanning (bounded: regex + entropy over short text).
- False positives can block legitimate writes; mitigated by allowlists/pragmas
  and the `warn` rollout mode, but requires tuning.
- A new `witan/scan/` subsystem, config surface, and plugin contract to
  maintain and version.
- Detection is best-effort: novel secret formats and obfuscated values will slip
  through. This reduces accidental ingestion; it is not a guarantee against a
  determined writer, and must not be sold as one.
- Redaction mutates user content; the placeholder must be unambiguous and the
  behavior documented so it is not mistaken for data loss.

**Neutral**

- Feature ships **enabled** (opt-out); see the 2026-07-07 amendment below.
- `witan-code` has a **separate** write path (`witan_code/store.py`,
  `indexer.py`) that does not share this choke point; whether indexed-source
  secrets are in scope is a separate decision
  (`tk-evaluate-witan-code-write-path-scanning-indexed--150422`).

## Implementation

Sequenced in the epic backlog. Spine (p0): `ScanConfig` + `load_scan_config()`,
the `Scanner` protocol + registry, and the `change()` interception with the field
map and enforcement. Then built-in secret + PII detectors, entry-point plugin
discovery, redaction, allowlisting, audit logging, the `witan scan` CLI, the
multi-tenant policy control, tests, and docs.

## Amendment (2026-07-07): enabled by default

D4 originally shipped the feature **disabled**, following the
`WITAN_EMBED_ENABLED` opt-in precedent. Revised: `ScanConfig.enabled` now
defaults to **`true`** — scanning is opt-out (`WITAN_SCAN_ENABLED=false` or
`[scan] enabled = false` to turn it off), not opt-in. Rationale: an opt-in
default means most installs run unscanned unless an operator deliberately
turns it on — the exact accidental-ingestion risk this ADR exists to close.
The redact-by-default PII path and fail-closed secret blocking make an
enabled default low-friction; false positives are handled via the allow/deny
detector lists, per-category mode, and the eventual allowlist engine, not by
leaving the feature off. Everything else in D1–D4 is unchanged.
