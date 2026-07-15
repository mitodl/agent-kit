# Extract `witan-core` shared package — spec

Status: accepted (spec phase)
Project: `wp-extract-witan-core-shared-package-e2660b`
Epic: task `tk-epic-extract-witan-core-shared-package-6182db`
Source memories: `pf-witan-witan-code-share-a-large-copy-paste-surfac-05dc6b`
(duplicated-surface inventory), `ctx-witan-core-extraction-must-coordinate-with-the-m-b2e7a4`
(coordination with the multi-user deploy).

## Goal

Collapse the duplicated surface that `mcp/servers/witan` (dist `witan-council`,
import `witan`) and `mcp/servers/witan-code` (dist `witan-code`, import
`witan_code`) carry from being built copy-paste-and-diverge, into a third shared
`packages/witan-core` (import `witan_core`) that both depend on — mirroring the
already-proven `packages/agent-config-kit` model. This ends the "fix it twice,
watch it silently drift" tax on code that is *contractually required* to stay
identical (the repo-key canonicalizer; the omnigraph binary version) while
leaving genuinely-diverged, same-shaped-but-different-feature code local to each
package.

## Central architectural decision (decided)

**Extracting `witan-core` deliberately reverses the "no cross-package import"
convention** that both packages document verbatim (in `graph.py`, `_detach.py`,
`maintenance.py` docstrings). The mitigation is the exact pattern
`agent-config-kit` already establishes:

- `witan-core` is a third `packages/` sibling, wired into each server via a
  `[tool.uv.sources]` **editable path** (dev/CI resolve the workspace copy) plus
  a published **PyPI version range** `witan-core>=0.1,<1` (published wheels /
  `uvx --from git+…` installs resolve PyPI).
- **`witan_core` imports neither `witan` nor `witan_code`.** This preserves the
  current one-directional mount DAG: `witan` optionally mounts `witan-code` as
  `witan code` (try/except ImportError in `cli/__init__.py`); `witan-code` never
  imports `witan`. `witan_core` sits *below* both — a leaf.
- Every extraction is **behavior-preserving**. A package keeps its diverged tail
  by **subclassing / parameterizing** the shared core, never by unifying
  same-shaped-but-different-feature code. Both servers' existing test suites must
  stay green at every incremental merge, and each is independently installable.

The removed docstrings' rationale is updated as copies are deleted, and each
extraction PR references this decision.

## `witan_core` package layout (decided)

```
packages/witan-core/
  pyproject.toml
  witan_core/
    __init__.py            # curated re-exports (see below)
    _detach.py             # popen_detached
    omnigraph_install.py   # binary installer (was setup.py's omnigraph section)
    omnigraph.py           # OmnigraphClient base + OmnigraphConflict + constants
    elicit.py              # confirm() / text()  (NOT repo_or_detect — witan-local)
    repo_key.py            # normalise() + _find_git_config()  (join-key contract)
    timeutil.py            # now_iso()
    maintenance.py         # throttled-optimize skeleton (parameterized)
    cli.py                 # app_factory, AGENT_NAMES, author-resolution, report()
    visualize.py           # (optional) vis-network HTML render kit
  tests/
```

`__init__.py` re-exports the stable public API (`popen_detached`,
`OmnigraphClient`, `OmnigraphConflict`, `normalise`, `now_iso`, `confirm`,
`text`, installer entrypoints) so both servers import from the package root, the
same way `agent_config_kit/__init__.py` does. Module file names above are the
target; the implementation task may adjust a name, but the public import surface
is fixed by `__init__`.

## Dependencies & extras (decided)

Keep the base footprint minimal and gate every heavier concern behind an
**optional-dependency extra**, so neither server pulls weight it doesn't use:

```toml
[project]
name = "witan-core"
version = "0.1.0"
requires-python = ">=3.11"
license = "BSD-3-Clause"
dependencies = []                         # base modules are stdlib-only

[project.optional-dependencies]
cli = ["cyclopts>=4,<5", "rich>=13"]      # cli.py, the installer's rich output
mcp = ["fastmcp>=3.4.2,<4"]               # elicit.py (AcceptedElicitation)
```

- `_detach`, `repo_key`, `timeutil`, `omnigraph` (subprocess wrapper),
  `maintenance` (uses `popen_detached` + stdlib) are **stdlib-only** → base.
- `omnigraph_install.py` prints via `rich.Console`. Rather than force `rich`
  into the base, the installer takes an **injected printer** (a
  `Callable[[str], None]`, default a no-styling `print`); the rich console is
  supplied by each server's `setup` CLI, which already depends on `rich`. (If the
  implementation prefers to keep the rich-styled strings, it may instead put the
  installer behind the `cli` extra — decide at implementation, but base must not
  hard-require `rich`.)
- `elicit.py` imports `fastmcp.server.elicitation.AcceptedElicitation` → `mcp`
  extra.

Both servers already declare `fastmcp`, `cyclopts`, `rich`, so each depends on
`witan-core[cli,mcp]` (exact extras per what each actually uses) and no new
transitive weight is added. Build backend `hatchling`, `packages =
["witan_core"]`, `[tool.bumpversion]` mirroring `agent-config-kit`.

## Wiring both servers (scaffold task `tk-scaffold-…-55159e`)

In **both** `mcp/servers/witan/pyproject.toml` and
`mcp/servers/witan-code/pyproject.toml`, alongside the existing
`agent-config-kit` stanza:

```toml
# [project.dependencies]
"witan-core>=0.1,<1",

# [tool.uv.sources]  (same dev/CI-uses-workspace-copy comment as agent-config-kit)
witan-core = { path = "../../../packages/witan-core", editable = true }
```

Acceptance: `uv sync` resolves in both servers; both `import witan_core` with no
cycle; both existing suites pass with `witan-core` installed **but not yet used**
(no behavior change). This lands the convention reversal; nothing is deleted yet.

## Per-extraction contracts

Ranked by confidence, matching the child tasks. Each row is an independently
mergeable PR that deletes the now-duplicated copies and keeps both suites green.

### 1. `popen_detached` — byte-identical (task `…-8c4171`)

`witan/witan/_detach.py` and `witan_code/witan_code/_detach.py` are **code-identical**;
they differ only in docstring prose. Move verbatim to `witan_core/_detach.py`;
both packages re-import. Trivial.

### 2. Omnigraph binary installer (task `…-66452e`, removes Renovate hack)

Extract the "Omnigraph binary" section of `witan/witan/setup.py`
(`_OMNIGRAPH_VERSION`, `_OMNIGRAPH_ASSETS`, `_VERSION_RE`, `_installed_version`,
`_download_omnigraph`, `install_omnigraph`) into `witan_core/omnigraph_install.py`.
The **rest of `setup.py` stays witan-local** (`witan_bundle`,
`prune_legacy_hook_entries`, `install_default_config` are witan-specific).
`witan-code/witan_code/setup.py`'s copy is deleted and re-imports the core.

**This deletes the Renovate lockstep hack.** After extraction `_OMNIGRAPH_VERSION`
exists in exactly one file, so the `renovate.json` custom manager collapses:

```jsonc
// managerFilePatterns: single file, drop the "must stay in lockstep" rationale
"/^packages/witan-core/witan_core/omnigraph_install\\.py$/"
```

Also update the two GitHub workflows that reference the setup.py omnigraph
version (`.github/workflows/publish-witan.yml`, `witan-tests.yml`) to the new
path. Behavior: `witan setup` / `witan-code setup` still fetch the pinned release
into `~/.local/bin/omnigraph`.

### 3. Elicitation primitives + `now_iso` (task `…-507571`)

- `confirm()` and `text()` from `witan/witan/elicit.py` → `witan_core/elicit.py`
  (the two clients compared identical). **`repo_or_detect` stays witan-local** —
  it offers to elicit a *repo*, a witan-layer concern, and its `from . import
  repo` keeps pointing at witan's local `repo` shim.
- `now_iso()`: the helper `def _now_iso(): return
  datetime.now(timezone.utc).isoformat()` is defined **three times byte-identically**
  (`witan/server.py:276`, `witan_code/bridge.py:24`, `witan_code/indexer.py:1041`).
  Hoist one copy to `witan_core/timeutil.now_iso`; all three call sites re-import.

### 4. `repo.normalise` + `_find_git_config` — contract-bound join key (task `…-31ce66`)

**Highest-correctness item.** `_normalise` is the canonicalizer behind the
cross-layer symbol join key `repo_uri#path::Name`; if the two copies drift, the
memory/task/workflow layer and the code-graph layer stop joining. Extract
`normalise`/`_normalise` and `_find_git_config` (and the shared `_parse_remote`
helper they anchor) from `witan/witan/repo.py` into `witan_core/repo_key.py`.

- **`detect()` and `current_branch()` stay package-local** — they are
  intentionally diverged (witan's `current_branch` never sanitizes; witan-code
  sanitizes for omnigraph-safe branch storage). Each package's `repo.py` becomes
  a thin module that re-exports `witan_core.repo_key.normalise` and keeps its own
  `detect`/`current_branch`.
- **Contract test (required, lives in `witan-core/tests/`):** a golden table of
  input→canonical-URI cases (`git@github.com:org/repo.git`,
  `https://github.com/org/repo`, `ssh://…`, gitlab subgroups, trailing `.git`/`/`,
  userinfo) locking the canonical form. This is the regression guard that makes
  the single-source-of-truth safe.

### 5. `OmnigraphClient` base — superset design (task `…-20abd9`, **linchpin**)

The two implementations have **diverged on two orthogonal axes**, so neither is a
subset of the other:

| Concern | `witan` (`graph.py`) | `witan-code` (`graph.py`) |
|---|---|---|
| retry/repair loop, `_acquire_write_lock`, `_repair`, `_find_binary`, `read()` row-envelope unwrap, `optimize()`, `cleanup()` | ✓ | ✓ (same shape) |
| storage-version-mismatch friendly error (`_is_storage_version_mismatch`, `_friendly_storage_error`) | ✓ | ✗ |
| admission-cap HTTP-429 self-backoff (jittered; PR #121) | ✓ | ✗ |
| `guard` write-callback + `surface_conflict`/`OmnigraphConflict` (CAS) | ✓ | ✗ |
| `apply_schema()` | ✓ | ✗ |
| `branch` support: `list_branches`/`ensure_branch`/`delete_branch`/`_branch_args` | ✗ | ✓ |
| bulk `load()` (JSONL temp file) | ✗ | ✓ |
| write-subcommand set / command assembly | `_execute` split; writes = {mutate,load} | single `_run`; writes = {mutate,load,optimize,cleanup} + branch args |

**The real extraction axis is LOCAL vs REMOTE, not witan vs witan-code**
(per coordination memory): local `<path>.omni` uses flock + optimistic-concurrency
retry; remote `http(s)/s3` omnigraph-server skips flock and hits the per-actor
admission cap. Both packages will run against the deployed server, so the
**admission-cap 429 self-backoff belongs in the shared base** (already moved into
witan's copy by PR #121 — carry that shape forward, don't re-derive a stale one).

**Base class `witan_core.omnigraph.OmnigraphClient`** owns everything
LOCAL/REMOTE-generic:

- ctor `(graph_uri, queries_dir, token=None)`; `read()`, `change()`,
  `optimize()`, `cleanup()`; `_run`/`_execute`, `_acquire_write_lock`, `_repair`,
  `_find_binary`; the constants (`_WRITE_SUBCOMMANDS`, `_RETRYABLE`,
  `_NEEDS_REPAIR`, `_MAX_ATTEMPTS`); storage-version-mismatch handling; the
  jittered admission-cap backoff; `OmnigraphConflict`.
- Two **extension hooks** on the base so subclasses inject without forking `_run`:
  1. `_extra_args(subcommand) -> list[str]` (default `[]`) — witan-code overrides
     to inject `--branch …`/`--from main`.
  2. a `guard` injection point in `change()` and a `surface_conflict` param on
     the write path (default off) — witan uses both; witan-code leaves them at
     default.
  Unify writes so `optimize`/`cleanup` are in the base write set and route
  through one code path (witan today reaches them via `_execute(is_write=True)`,
  witan-code via `_WRITE_SUBCOMMANDS` — same effect).

**`witan.graph.OmnigraphClient(base)`** adds: `guard`/`surface_conflict` usage,
`apply_schema()`. **`witan_code.graph.OmnigraphClient(base)`** adds: `branch`
ctor arg, `load()`, `list_branches`/`ensure_branch`/`delete_branch`,
`_extra_args` override. Behavior preservation is proven by each package's
existing `test_graph.py` passing unchanged.

**Blocking dependencies (carry forward, do not paper over):**
- The unverified "flock-skip-for-remote is a safe write-serialization strategy"
  assumption is a launch blocker tracked by spike
  `tk-spike-validate-omnigraph-server-remote-write-ser-1a8058`. The base must keep
  the flock-skip-for-remote branch as-is; the spike validates it separately.
- Actor identity: local `--as <ACTOR>` vs remote server-resolves-from-bearer-token.
  Base supports both via the `token` arg; witan's JWT→actor→token mapping
  (`identity.py`, ADR-0004) **stays at the witan layer** and supplies the token.
- **Must rebase onto the multi-user deploy's current `graph.py`** (project
  `wp-witan-multi-user-service-deployment-dcf6ee`, in-flight), not a stale
  snapshot.

### 6. Throttled-optimize maintenance skeleton (task `…-0ce376`)

`witan/witan/maintenance.py` and witan-code's copy share the shape:
`optimize_interval()` (env-overridable window), a per-store stamp file with
atomic `os.replace` write, `due()`, `spawn_background_optimize()` →
`popen_detached`, plus the identical `_OPTIMIZE_INTERVAL = 24*3600` /
`_REMOTE_PREFIXES` constants. Extract the **throttle+detach skeleton** into
`witan_core/maintenance.py` **parameterized** on everything that diverges:

- the detached command (`[sys.executable, "-m", "witan", "optimize", …]` vs
  `"-m", "witan_code", …`);
- the env var name (`WITAN_OPTIMIZE_INTERVAL` vs `WITAN_CODE_OPTIMIZE_INTERVAL`);
- the **stamp-path resolver** — witan hashes with `sha1[:16]` and writes into
  `session_state.session_state_dir()/witan-optimize-<digest>.json`; witan-code
  hashes with `sha256[:16]` and writes into `tempfile.gettempdir()/…`. Inject a
  callable that returns the stamp `Path` for a store key (so `session_state`
  stays witan-local and is **not** extracted); the digest algorithm is an
  implementation detail of that callable.
- the store key type: witan keys on `graph_uri: str`, witan-code on
  `store: str | Path`. Base normalizes via `str(store)`.
- witan-code's extra `if not Path(store).exists(): return False` guard in
  `due()` — expose as an optional predicate (default: no extra check).

Depends on the `popen_detached` extraction (#1).

### 7. CLI scaffolding (task `…-5f62f9`, p2)

Lower-confidence — the two CLIs are structurally different (witan: a `cli/`
**package** of submodules using `rich`; witan-code: a single `cli.py` using plain
`print`). Extract only the genuinely shared scaffolding:

- `AGENT_NAMES` dict (`setup_cmd.py:24` ≈ `cli.py:462`) — byte-identical map.
- author resolution around `WITAN_AUTHOR` (env → resolved author string).
- an `app_factory` for the cyclopts `App` construction pattern.
- a `report()` printer **parameterized on a print function** — witan passes a
  `rich` console (`_report` at `setup_cmd.py:33`), witan-code passes builtin
  `print` (`_report_setup` at `cli.py:471`). The rich-vs-plain divergence stays
  at the call site, not in core.

Depends on the installer extraction (#2) because `setup_cmd`/`cli.py` `setup`
commands consume both. If a piece resists clean parameterization, leave it local
— this task is explicitly allowed to extract a subset.

### 8. (Optional) vis-network HTML render kit (task `…-f56cb1`, p3)

`witan/witan/visualize.py` and witan-code's HTML renderer share a vis-network
scaffold but diverge on node/edge shaping. Extract only the generic
HTML-document/vis-network boilerplate as a render kit each package feeds its own
nodes/edges into. `build_graph` on both sides stays local. Extract only if it
pays for itself; skippable.

## Explicitly out of scope — keep package-local

Do **not** unify these same-shaped-but-different-feature files: `config.py`
(witan 721-line policy config vs witan-code 65-line store-path config — different
domains); all `schema/*.pg` + `queries/*.gq` (disjoint node types);
`repo.detect()`/`current_branch` (intentionally diverged); witan-code's branch
subsystem + `store.py`; witan `readiness.py`/`identity.py`;
witan-code `package_map.py`/`stitch.py`/`edges.py`/`bridge*.py`/`indexer.py`;
both `build_graph`; both `*_bundle` builders; both `inject_context`;
`session_state` (passed into the maintenance skeleton, not extracted).

### Evaluate-first (not in this phase)

Remote-MCP-client proxy + OIDC device-auth (`witan/remote/`, ADR-0005, task
`…-e3d194`) is a *plausible* future `witan_core.remote`, but it is brand-new and
still churning under the multi-user deploy. **Extract only after ADR-0005 settles
and the deploy owner signs off** — else it fights in-flight changes.

## Coordination & sequencing (decided)

The extraction shares three interaction points with the in-flight multi-user
deploy (`wp-witan-multi-user-service-deployment-dcf6ee`): `graph.py`,
`cli/_common.py` (ADR-0005 `RemoteServerProxy` via `WITAN_REMOTE_URL`), and
`remote/`. The deploy has already restructured them.

**Sequencing rule:**
1. **Now, deploy-independent:** scaffold (#0), `popen_detached` (#1), omnigraph
   installer (#2), elicit + `now_iso` (#3), `repo_key` (#4), maintenance (#6).
   These don't touch the deploy's surface.
2. **Coordinate with the deploy:** `OmnigraphClient` base (#5) and CLI (#7) —
   rebase onto the deploy's current `graph.py`/`cli/_common.py`; keep `_srv`'s
   proxy-vs-module switch witan-local.
3. **Evaluate-first, after ADR-0005 settles:** remote proxy/OIDC (`…-e3d194`).

## Task DAG (already created; this spec ratifies it)

```
epic …-6182db
└─ scaffold …-55159e (open, ready — root of everything)
   ├─ popen_detached …-8c4171 ──→ maintenance …-0ce376
   ├─ omnigraph installer …-66452e ──→ cli scaffolding …-5f62f9
   ├─ elicit+now_iso …-507571
   ├─ repo_key …-31ce66
   ├─ OmnigraphClient base …-20abd9   (coordinate w/ deploy; spike …-1a8058)
   ├─ vis-network kit …-f56cb1 (optional)
   └─ remote proxy/OIDC …-e3d194 (blocked; evaluate-first)
verify: independent-installability + no-cycle …-22f313
        (blocked by all extractions above)
housekeeping: delete stale root-level witan-code/queries/ …-f1bc5b (independent)
```

## Acceptance (whole epic)

- `packages/witan-core` exists, mirrors `agent-config-kit`, imports neither
  `witan` nor `witan_code`.
- Every listed duplicated copy is deleted from both servers and re-imported from
  `witan_core`; the "deliberately duplicated" docstrings are gone/updated.
- The Renovate omnigraph custom manager targets one file; the lockstep rationale
  is removed.
- `repo_key` has a golden contract test; both servers' full suites pass at each
  incremental merge.
- Both servers remain independently installable (`uvx --from git+…`) with no
  import cycle — proven by verification task `…-22f313`.
