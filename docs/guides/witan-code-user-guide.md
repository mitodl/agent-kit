<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit mcp/servers/witan-code/docs/USER_GUIDE.md instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`mcp/servers/witan-code/docs/USER_GUIDE.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan-code/docs/USER_GUIDE.md).

# User guide

## What it is

witan-code is a tree-sitter-based code graph for a single repository. It
parses source files into symbols (functions, methods, classes, modules) and
their relationships (defines, contains, calls, references, imports,
inherits), stores them in a local [Omnigraph](https://github.com/ModernRelay/omnigraph)
graph, and exposes definition / reference / caller / impact queries to a
coding agent through MCP tools and a CLI. The problem it solves: an agent
working in a large repo needs "where is this defined", "who calls this", and
"what breaks if I change this" answered from the actual syntax tree instead
of grep-and-guess.

A second layer, the **cross-repo bridge**, extends this past the repo
boundary: it extracts interface contracts (env vars, HTTP endpoints,
published packages, deployed services) that couple repos in a
service-oriented architecture, and answers "what depends on this repo" /
"who provides this contract" across every repo you've indexed.

witan-code is Layer 2 of a two-layer stack. Layer 1, `witan`, holds
team-synced memory (patterns, project facts, lessons, workflow traces) in a
shared store. The two compose through **soft symbol-id references**: a
Layer-1 node can record a symbol id of the form
`repo#relative/path.py::QualifiedName`, which resolves in the code graph via
`code_find_definition` — there is no hard cross-store edge, so the
team-synced memory graph stays independent of any one machine's local code
index. See the main [README](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan-code/README.md) for the full two-layer table.

## Feature set

- **Symbol indexing** across Python, TypeScript/JS/JSX/TSX, Bash/Zsh, and
  YAML, with signatures, docstrings, and decorators attached to each symbol
  — see the [supported languages table](#supported-languages) below.
- **Definition / reference / caller / impact queries** —
  `code_find_definition`, `code_find_references`, `code_callers`,
  `code_impact` (transitive BFS), `code_symbols_in_file`,
  `code_search_symbol` (BM25).
- **Cross-repo interface bindings** — `env_var` / `endpoint` / `package` /
  `service` contracts linking repos in an SOA; see [README §
  Cross-repo context bridge](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan-code/README.md#cross-repo-context-bridge-layer-25).
- **Precision-tiered edges** — every cross-repo tool accepts `min_precision`
  (`precise` / `heuristic` / `fuzzy`) to filter by trust level; see
  [EDGE_PRECISION_TIERS.md](../explanation/code-graph/edge-precision-tiers.md).
- **Precise (Stage 2) symbol stitching** — canonical-symbol-string joins
  computed at read time, no cross-repo edge ever stored; see
  [STAGE2_STITCHING.md](../explanation/code-graph/stage2-stitching.md) and
  [SYMBOL_TABLE.md](../explanation/code-graph/symbol-table.md).
- **Branch-aware indexing** — a non-default git branch indexes onto its own
  view, named for its writer as well as the branch, so in-flight work never
  overwrites the shared `main` view nor another checkout of the same branch;
  see [BRANCH_INDEXING.md](branch-indexing.md).
- **Dependency visualization** — `witan-code deps` prints a text summary and
  can emit an interactive HTML force-directed graph of cross-repo links.

## Installation

witan-code is usually installed as part of the `witan` umbrella package —
`witan setup` wires both servers together and installs the shared
`omnigraph` binary in one step. See the [witan README](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/README.md)
for that one-step setup.

To use witan-code **standalone** (code graph only, no memory/task tools):

```bash
# One-shot:
uvx --from witan-code witan-code index

# Persistent CLI install:
uv tool install witan-code
witan-code index
```

To track pre-release/unreleased code instead of the latest PyPI release,
install from the git repo directly:

```bash
# One-shot:
uvx --from git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan-code \
    witan-code index

# Persistent CLI install:
uv tool install git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan-code
witan-code index
```

Standalone use also needs the `omnigraph` binary on `PATH`; if `witan` isn't
installed to provide it, run `witan-code setup` once (see [Troubleshooting](#troubleshooting)).
To wire witan-code as a standalone MCP server (no witan memory/task tools),
copy the matching snippet from `config/` into your agent's config — see
[README § Install](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan-code/README.md#install).

## First-run walkthrough

1. **Install the omnigraph binary** (skip if `witan setup` already ran):

   ```bash
   witan-code setup
   ```

2. **Index the repo** from its root:

   ```bash
   cd ~/code/my-repo
   witan-code index
   ```

   This creates the repo's store lazily on first run and prints a summary
   (`scanned=…  indexed=…  skipped=…  symbols=…  edges=…  bindings=…
   errors=…`). Re-running `index` is incremental — unchanged files are
   skipped by content hash; use `reindex` to force a full rebuild.

3. **Ask "where is this defined"** — via the CLI or, more commonly, through
   an agent calling the MCP tool directly:

   ```bash
   # MCP tool (what an agent calls):
   code_find_definition(name="ServiceClient.run")
   ```

   Returns matching symbols with their file, line, signature, and docstring.

4. **Ask "who calls this"**:

   ```bash
   code_callers(symbol_id="https://github.com/org/repo#app/client.py::ServiceClient.run")
   code_impact(symbol_id="...", max_depth=5, max_nodes=200)  # transitive callers
   ```

   `code_impact` walks the caller graph breadth-first up to `max_depth`
   hops or `max_nodes` results — use it before changing a function's
   signature to see the blast radius. Remember `Calls`/`References` are
   **heuristic** (syntactic name matching, not verified dispatch) — treat
   the result as a high-recall starting point, not ground truth.

5. **Ask "what depends on this repo"** once more than one repo is indexed:

   ```bash
   witan-code deps --repo my-repo
   # or, for the precise tier only:
   witan-code deps --repo my-repo --min-precision precise
   ```

   Cross-repo tools only see something once at least two repos that share a
   contract (an env var, an endpoint, a package, a deployed service) have
   both been indexed — the bridge is populated incrementally as you index
   each repo, with no separate registration step.

## Supported languages

| Language | Extensions | Symbols extracted |
|---|---|---|
| Python | `.py` `.pyi` | functions, classes, methods, module |
| TypeScript / JS / JSX / TSX | `.ts` `.tsx` `.mts` `.cts` `.js` `.jsx` `.mjs` `.cjs` | functions, arrow consts, classes, methods (incl. arrow fields), interfaces, types, enums |
| Bash / Zsh | `.sh` `.bash` `.zsh` | functions |
| YAML | `.yaml` `.yml` | mapping keys as dotted paths (e.g. `jobs.build.steps`) |

Each symbol also carries a full `signature`, a `docstring` (Python
docstrings, TS/JS JSDoc), and `decorators` — returned by
`code_find_definition` / `code_search_symbol` / `code_symbols_in_file`. See
[README § Supported languages](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan-code/README.md#supported-languages) for what's
not indexed yet (HCL/Terraform is the leading candidate).

## Cross-repo bridge in brief

Per-repo indexing stops at the repo boundary; the bridge is a separate,
shared, local-only store (`_bridge.omni`) that links repos through contracts
they share — an env var one repo's infra sets and another reads, an HTTP
endpoint one serves and another calls, a package one publishes and others
import, a service one repo deploys. It is **zero-config**: every `index` /
`reindex` of any repo also extracts that repo's bindings into the bridge
store automatically — there's no registry of repos to maintain.

Two tiers of cross-repo linking exist and are merged into one
precision-filterable result:

- **Heuristic** (Stage 3) — bindings grouped by `(kind, key_norm)` with a
  confidence score. This is the original, always-on behavior.
- **Precise** (Stage 2) — a canonical-symbol-string join computed at read
  time, never stored. Higher trust, narrower coverage.

Don't re-derive the mechanics here — see:

- [SYMBOL_FORMAT.md](../explanation/code-graph/symbol-format.md) — the canonical symbol string format
  bindings are keyed on.
- [STAGE2_STITCHING.md](../explanation/code-graph/stage2-stitching.md) — the precise join algorithm.
- [EDGE_PRECISION_TIERS.md](../explanation/code-graph/edge-precision-tiers.md) — the `min_precision`
  parameter every cross-repo tool and the `deps`/`stitch` CLI commands
  accept.

## Troubleshooting

- **`omnigraph: command not found` / commands fail silently.** witan-code
  needs the `omnigraph` binary on `PATH`. If `witan` is installed, its
  `witan setup` already placed it; standalone, run `witan-code setup` (or
  `witan-code setup --dry-run` to preview). Re-run after an omnigraph
  version bump — `setup` always re-downloads, it doesn't skip an existing
  binary.
- **A tool returns `[]` / "No code graph yet."** Per-repo stores are created
  **lazily** on first `index` — there is no separate "create store" step.
  If you see the `No code graph yet. Run \`witan code index\` to build it.`
  hint, you haven't indexed this repo yet (or you're not in a directory
  witan-code recognizes as the repo — see `WITAN_REPO` in [README §
  Environment variables](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan-code/README.md#environment-variables)).
- **Bridge tools (`deps`, `code_interface_*`) return nothing.** The bridge
  store is also created lazily, on the first index that yields any bindings,
  and cross-repo links only appear once **both** sides of a contract have
  been indexed (e.g. the repo that reads an env var and the repo whose
  Pulumi config sets it). Indexing one repo alone won't show a link.
  Generic env names (`DEBUG`, `PORT`, `SECRET_KEY`, …) are deliberately
  excluded from cross-repo impact fan-out.
- **A feature-branch checkout doesn't see the same symbols as `main`.** Git
  branches other than the default index onto their own view, forked from
  `main` on first write — reads from that checkout follow the branch
  automatically. Branch names are sanitized (`[^A-Za-z0-9._-]` → `_`); a
  branch literally named `main` in a `master`-default repo maps to `_main`,
  and a detached HEAD checkout writes to a `_detached` scratch branch rather
  than ever touching `main`. `witan-code branches` lists what exists per
  store; `--prune` deletes the current repo's views whose git branch is gone.
  See [BRANCH_INDEXING.md](branch-indexing.md).
- **A teammate's in-flight work isn't in my results.** Each writer gets their
  own view of a shared branch (`act-<them>/feature-x`), so you see yours, not
  theirs. `witan-code branches --branch feature-x` lists every writer's view
  of that branch; pass one back as `--branch act-<them>/feature_x` (or as the
  `branch` argument of `code_find_definition` / `code_search_symbol` /
  `code_symbols_in_file`) to read it.
- **Caller/impact results look wrong or incomplete.** `Calls`, `References`,
  `Imports`, and `Inherits` are **heuristic** — syntactic name resolution
  that prefers same-file definitions. Dynamic dispatch, re-exports,
  shadowing, and cross-file resolution beyond name matching aren't modeled
  precisely. Treat them as a high-recall starting point for investigation,
  not a verified call graph. `Defines`/`Contains` (and Stage-2 precise
  cross-repo edges) are exact by contrast.
- **No pre-built `omnigraph` binary for your platform.** Only
  `linux/x86_64` and `darwin/arm64` have pinned release assets; `setup`
  prints a message and does nothing on other platforms — install the
  binary manually and put it on `PATH`.
