# CLI reference

`witan-code` is a [cyclopts](https://cyclopts.readthedocs.io/) app (see
[`witan_code/cli.py`](../witan_code/cli.py)); every command below is also
available as `witan code <command>` when witan-code is installed alongside
the `witan` umbrella package. Run `witan-code --help` or `witan-code
<command> --help` for the live version of this table — it is generated from
the same docstrings.

Global flags: `--help` / `-h`, `--version`.

Boolean flags follow cyclopts' `--flag`/`--no-flag` convention; both forms are
always available even where only `--flag` is shown below.

## `index [PATH]`

Incrementally index `PATH` (file or directory). Files whose content hash is
unchanged since the last index are skipped.

| Argument | Type | Default | Notes |
|---|---|---|---|
| `PATH` / `--path` | `Path` | `.` | Positional or `--path`; file or directory |

Prints a summary line: `scanned/indexed/skipped/symbols/edges/bindings/errors`.

```bash
witan-code index                  # index the whole repo, cwd
witan-code index app/models.py    # index a single file
```

## `reindex [PATH]`

Force re-index `PATH`, ignoring content hashes — every matched file is
re-parsed and re-inserted even if unchanged.

| Argument | Type | Default | Notes |
|---|---|---|---|
| `PATH` / `--path` | `Path` | `.` | Positional or `--path` |

```bash
witan-code reindex   # rebuild the whole repo's store
```

## `repos`

List the repositories that have a code graph indexed (reads every
`<slug>.omni` store under `WITAN_CODE_DIR`, excluding the shared bridge
store). Table columns: repo URI, file count, on-disk size, last-indexed
timestamp.

No parameters.

```bash
witan-code repos
```

## `branches [--prune]`

List omnigraph branches per indexed repo store. Non-default git branches
index onto same-named omnigraph branches (see
[BRANCH_INDEXING.md](BRANCH_INDEXING.md)); branch stores are re-derivable
caches, so lifecycle is deletion, not merge.

| Flag | Type | Default | Notes |
|---|---|---|---|
| `--prune` / `--no-prune` | `bool` | `False` | Delete the **current** repo's store branches whose git branch no longer exists locally, plus the `_detached` scratch branch. Other repos' stores are only listed — their git refs aren't visible from the current checkout. |

```bash
witan-code branches
witan-code branches --prune
```

## `deps`

Visualize cross-repo dependencies from the shared bridge store. Prints a Rich
summary of "repo A depends on repo B" links (A consumes a contract B
provides; for `service` bindings, the deploying repo depends on what it
deploys).

| Flag | Type | Default | Notes |
|---|---|---|---|
| `--kind` | `env_var \| package \| service \| endpoint` (choice) | none | Filter to one contract kind |
| `--repo` | `str` | none | Keep only links touching a repo whose slug contains this substring |
| `--html` | `Path` | none | Also write a self-contained interactive force-directed HTML graph to this path (click an edge to list the individual linkages behind it) |
| `--open-browser` / `--no-open-browser` | `bool` | `False` | Open the generated HTML file in the default browser (requires `--html`) |
| `--min-precision` | `precise \| heuristic \| fuzzy` (choice) | `heuristic` | Minimum edge precision tier — see [EDGE_PRECISION_TIERS.md](EDGE_PRECISION_TIERS.md). `heuristic` preserves prior behavior (every consumer/provider link this command has always shown); `precise` keeps only edges also covered by a Stage-2 canonical-symbol join (fetches the symbol table only when requested) |

```bash
witan-code deps --kind env_var --repo mit-learn
witan-code deps --html deps.html --open-browser
witan-code deps --min-precision precise
```

## `symbols`

Print a repo's symbol table from the bridge store — one row per `(role,
symbol)`: `exported` rows are the repo's public contract surface, `external`
rows are unresolved references Stage 2 joins against other repos' exports.
See [SYMBOL_TABLE.md](SYMBOL_TABLE.md).

| Flag | Type | Default | Notes |
|---|---|---|---|
| `--repo` | `str` | detected from cwd | Canonical repo URI |
| `--role` | `exported \| external` (choice) | none | Filter to one role |
| `--scheme` | `str` | none | Filter to one symbol scheme (`http`/`env`/`pkg`/`svc`) |

```bash
witan-code symbols --role exported --scheme env
```

## `stitch`

Print Stage-2 precise cross-repo edges computed at read time from the bridge
store's symbol table — distinct from the coarser `witan-code deps`
`(kind, key_norm)` heuristic grouping. See
[STAGE2_STITCHING.md](STAGE2_STITCHING.md).

| Argument/Flag | Type | Default | Notes |
|---|---|---|---|
| `REPO` / `--repo` | `str` | none | Keep only edges/gaps touching this repo (either side). Omit for the whole store |
| `--unresolved` / `--no-unresolved` | `bool` | `False` | Print external references with no precise match instead of edges — gaps in indexing coverage (a provider isn't indexed yet, or none exists in this SOA) |

```bash
witan-code stitch --repo https://github.com/mitodl/mit-learn
witan-code stitch --unresolved
```

## `serve`

Run the code-graph MCP server standalone (`code_*` tools only — no `witan`
memory/task tools). Blocks, serving over stdio.

No parameters.

```bash
witan-code serve
```

## `setup [--dry-run]`

Install the pinned `omnigraph` binary release to `~/.local/bin/` for
standalone witan-code use (not needed if `witan setup` already ran, since it
installs the same binary to the same place). Always re-downloads, so
re-running after an omnigraph version bump refreshes the binary.

| Flag | Type | Default | Notes |
|---|---|---|---|
| `--dry-run` / `--no-dry-run` | `bool` | `False` | Preview without writing |

```bash
witan-code setup
witan-code setup --dry-run
```
