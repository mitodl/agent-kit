# agent-config-kit manifest file format + CLI command surface — Spec

Project: `wp-cross-agent-coding-agent-config-management-libra-5593b0`
Phase: implementation
Status: design only — nothing here is implemented yet.

Answers `tk-spec-manifest-file-format-cli-command-surface-fo-ccd46a`, the
single unblocking task for the `cli` extra epic
(`tk-add-manifest-driven-cli-to-agent-config-kit-cli--8772d0`). Builds on the
shipped library described in
[`agent-config-kit-spec.md`](./agent-config-kit-spec.md) (§4's canonical
models, §4.1's `apply`/`apply_all` orchestration) — that package is already
implemented and in use by `witan/setup.py`; this spec only adds a
*declarative, non-Python* entry point on top of it for consumers who don't
want to write a Python `RegistrationBundle` by hand.

## 1. Goal

`witan/setup.py` builds its `RegistrationBundle` in Python because it already
is Python. A project with no Python tooling of its own (a plain Node repo, a
shell-scripted dotfiles setup, a CI job) currently has no way to use
`agent-config-kit` without writing a throwaway Python script. This spec adds:

1. A manifest file format that declares the same `RegistrationBundle` content
   declaratively.
2. A `cli` extra (`pip install agent-config-kit[cli]`) exposing an
   `agent-kit` console script that loads a manifest and calls
   `apply`/`apply_all`, plus a dry-run-safe `validate` command for drift
   detection, and prune/uninstall support for entries removed from a
   manifest between runs.

The base package stays dependency-light (`pydantic` only, per the existing
spec's D3) — none of this is importable or required unless `[cli]` is
installed.

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| M1 | Manifest format | **TOML**, parsed with the stdlib `tomllib` (available on Python ≥3.11, matching D4 — no new dependency on the base install). Considered YAML: rejected because it requires a third-party parser (`PyYAML`/`ruamel`) that would either bloat the dependency-light base package or force a parser-only split between `manifest.py` (needs `tomllib`, free) and `[cli]` (everything else) for no real benefit. TOML's `[[array-of-tables]]` syntax maps cleanly onto the `hooks`/`skills` lists and `[table.key]` onto the `mcp_servers` dict; every other structure in this repo that's a hand-authored manifest is already TOML (`pyproject.toml`, `uv.lock`). |
| M2 | Manifest module location | `agent_config_kit/manifest.py`, part of the **base** package (imports only `tomllib` + existing `models`/`plan`), not gated behind `[cli]` — a caller might want `load_manifest()` without wanting the CLI/`cyclopts`/`rich` dependencies. `agent_config_kit/cli.py` (gated behind `[cli]`, §4) is the only new CLI-only module. |
| M3 | CLI framework | `cyclopts`, matching this repo's established convention (`witan`'s own CLI, and the `cyclopts-cli-scripts` skill) — not `argparse`/`click`/`typer`. |
| M4 | `cli` extra packaging | `[project.optional-dependencies] cli = ["cyclopts>=4,<5", "rich>=13"]` in `packages/agent-config-kit/pyproject.toml`, plus `[project.scripts] agent-kit = "agent_config_kit.cli:main"`. Console script name is `agent-kit`, not `agent-config-kit` — avoids the package-name-as-command-name default, and matches this repo's own name for less mental mapping between "the repo I'm in" and "the command I run". (Originally shipped as the shorter `ac-kit`; renamed in 0.2.1 to reduce that exact confusion. `ack` was considered and rejected both times as too likely to collide with the well-known `ack`/`ack-grep` code-search tool many dev machines already have on `PATH`.) Console script registration is unconditional (that's how `pip`/`uv` entry points work), but `cli.py`'s top-level imports are the only place `cyclopts`/`rich` are imported — running the console script without `[cli]` installed fails fast with a clear `ImportError`-derived message, not a silent partial failure. |
| M5 | Path resolution inside a manifest | All filesystem paths in a manifest (`skill_md_path`, `entry_path`) are resolved **relative to the manifest file's own directory**, not the process CWD — matching how every other tool with a project-relative manifest behaves (e.g. `pyproject.toml` paths, `docker-compose.yml` build contexts). Absolute paths pass through unchanged. Resolution happens in `load_manifest()` (§3.2), so `RegistrationBundle` instances it produces always carry absolute paths — `apply`/`apply_all` are untouched, they already only accept resolved `Path`s. **Extension (implemented, see §3.3):** a `skill_md_path`/`entry_path` value can also be a remote `https://`/`git+` URI, sniffed by prefix (no new manifest field) and fetched into a local cache before the same absolute-`Path` contract applies — `installers.py`/`plan.py`/`prune.py` remain untouched. |
| M6 | Drift detection scope | `validate` (§4.2) diffs *only the keys `agent-config-kit` would write* (by name: MCP server names, hook identity, skill names) against on-disk state — not a full-file diff. Unrelated hand-edited keys in the same file are never flagged as drift; that's the same "additive, override-by-key" contract `apply()` already has (spec §4.1). |
| M7 | Prune/uninstall mechanism | A **state file** recording what the *last* `apply` from a given manifest actually wrote, so a later `apply` with entries removed from the manifest can remove exactly those (and only those) keys — never touching keys the manifest never owned. Default path: `<manifest>.lock.json` next to the manifest (overridable with `--state-file`). Content and full behavior are this spec's biggest open question — detailed in §5; final shape is this spec's contribution, but the fiddly edge cases (concurrent manifests targeting the same file, state file loss) are left to the implementing task (`tk-design-implement-prune-uninstall-for-entries-rem-4dfcdb`) to resolve against this default design. |
| M8 | `instructions`/`lsp_servers` in the manifest | Modeled in the TOML schema (§3.1) for forward compatibility with `RegistrationBundle`'s existing (currently-unpopulated-by-any-v1-caller) fields, but `load_manifest()` only *populates* them if present — no v1 registry entry consumes them yet (spec D7), so a manifest that sets them will parse fine and `apply()` will simply no-op on them for every current platform, exactly as it does today for a hand-built `RegistrationBundle`. |

## 3. Manifest file format

### 3.1 Schema

```toml
# agent-config.toml — TOML mirror of agent_config_kit.plan.RegistrationBundle

instructions = "See AGENTS.md"    # optional; unpopulated by any v1 platform (M8)

[options]
scope = "global"                  # "global" | "project" — default: "global"
platforms = ["claude", "pi"]      # optional allow-list; default: apply_all's
                                   # detect_installed_platforms()

[mcp_servers.witan]
kind = "stdio"                    # "stdio" | "remote" — mirrors McpServer's discriminator
command = "uvx"
args = ["witan", "serve"]
env = { WITAN_AUTHOR = "team" }

[mcp_servers.hosted-tool]
kind = "remote"
url = "https://example.com/mcp"
transport = "streamable-http"     # "sse" | "http" | "streamable-http"

[lsp_servers.example-lsp]         # optional; unpopulated by any v1 platform (M8)
command = ["example-lsp", "--stdio"]

[[hooks]]
kind = "declarative"              # "declarative" | "plugin"
event = "user_prompt_submit"      # HookEvent value
command = "witan inject-context"

[[hooks]]
kind = "plugin"
entry_path = "extensions/pi/witan.ts"   # resolved relative to this file (M5)

[[skills]]
name = "witan-task"
skill_md_path = "skills/witan-task/SKILL.md"  # resolved relative to this file (M5)
```

Table/array names and field names are **identical to the Python model field
names** (`kind`, `command`, `args`, `env`, `event`, `entry_path`, ...) — no
separate naming scheme to keep in sync. `mcp_servers` and `lsp_servers` are
each a table-of-tables keyed by name (mirroring `RegistrationBundle.mcp_servers:
dict[str, McpServer]` and `lsp_servers: dict[str, LspServer]` respectively);
`hooks`/`skills` are arrays-of-tables (mirror the `list[...]` fields).
`instructions` is a plain top-level key — deliberately placed before any
table/array-of-tables in the example above, since TOML parses trailing
key-value pairs as belonging to whatever table/array entry precedes them, not
as top-level keys (a real gotcha the loader's own tests, §6 step 2, should
cover with a fixture that gets this wrong).

### 3.2 Loader API

```python
# agent_config_kit/manifest.py

from dataclasses import dataclass
from pathlib import Path

from .models import Scope
from .plan import RegistrationBundle


@dataclass
class ManifestOptions:
    scope: Scope = Scope.GLOBAL
    platforms: list[str] | None = None  # None => apply_all's own detection


@dataclass
class Manifest:
    bundle: RegistrationBundle
    options: ManifestOptions
    path: Path  # the manifest file this was loaded from


def load_manifest(path: Path, *, cache_dir: Path | None = None) -> Manifest: ...
```

`load_manifest` raises `ManifestError` (new, in the same module) with a
message that includes the manifest path and the offending table/key on any
`tomllib.TOMLDecodeError` or pydantic `ValidationError` — both wrapped, not
propagated raw, since a CLI user has no Python traceback context to interpret
a bare `pydantic.ValidationError`.

### 3.3 Remote (`https://`/`git+`) skill/hook sources

Answers `tk-support-remote-git-https-uris-for-skill-hook-plu-18590e`. A
`skill_md_path`/`entry_path` value is sniffed by prefix — `https://`,
`http://`, or `git+` — rather than adding a new field or a `Path | AnyUrl`
union type: `_resolve_path_field` (§3.2) already sits at the one place every
such value passes through before a `SkillSource`/`PluginRegistration` is
constructed, so remote resolution is an extra branch there, not a model
change. `installers.py`, `plan.py`, and `prune.py` are untouched — they only
ever see a resolved, absolute, local `Path`, exactly as before.

Implemented in `agent_config_kit/fetch.py`, **in the base package** —
resolving this spec's original open question of whether remote fetch needs
an opt-in extra (to avoid an HTTP client dependency on the dependency-light
base install, per the base spec's D3): it doesn't, because fetching is done
with stdlib-only tooling (`urllib.request` for `https://`/`http://`, the
`git` binary via `subprocess` for `git+`), not a new third-party dependency.
`load_manifest()` already does filesystem I/O to resolve relative paths; a
network fetch at the same step is judged an extension of that, not new
packaging weight.

- **`https://`/`http://`** fetches exactly one file — sufficient for
  `entry_path` (a single plugin script) or a single-file skill, but *not* a
  skill with supporting files (`scripts/`, `references/`, ...), since a bare
  GET has no directory on the other end.
- **`git+<url>[@ref][#subdirectory=<path>]`** (pip VCS-URL convention)
  shallow-clones the repo and resolves `subdirectory`, covering the
  multi-file skill case the HTTP path can't.
- **Caching/staleness:** cached under `<manifest_dir>/.agent-config-kit-cache/`
  by default (`cache_dir` param / CLI `--cache-dir`), keyed by a hash of the
  URI. Every `load_manifest()` call re-fetches — no separate "only on
  manifest change" mode — but HTTP(S) sends a conditional GET
  (`If-None-Match`/`If-Modified-Since` against a stored ETag/Last-Modified
  sidecar) so an unchanged remote is a cheap 304, and a connection-level
  failure (no response at all) falls back to the last successfully-fetched
  copy rather than failing an `apply`/`validate` that would otherwise have
  worked offline. A real HTTP error response (404, 403, ...) is not treated
  as transient and always raises, even with a stale cache present — that
  response means the resource is actually gone, not just unreachable this
  instant. `git+` follows the same fallback shape via a shallow `git fetch`.
- **Prune implication (§5):** none needed. A fetched file is tracked by
  `skill_files`/`hook_identity` exactly like any other local file — prune
  diffs by path/identity, not by fetch origin. The one real consequence:
  content drift for an *unchanged* URI between two applies is invisible
  unless a fetch actually notices a change, which is the same
  "content-level drift isn't actionable, only presence/absence is"
  reasoning §4.2 already establishes for ordinary local skills.
- **Not solved here (flagged as follow-up):** no auth support for private
  `https://` URLs or private git remotes (git's own credential helpers work
  for `git+` today since `git` itself does the clone; a bare `https://`
  single-file fetch has no equivalent). `git` must be on `PATH` — there is
  no pure-Python git client dependency, by design, so a `git+` URI on a
  machine without `git` fails with a clear `FetchError` rather than a
  cryptic one.

## 4. CLI command surface

Three `cyclopts` subcommands under the `agent-kit` console script (M3/M4):

```
agent-kit apply MANIFEST [--scope global|project] [--platform NAME]...
                       [--prune/--no-prune] [--dry-run] [--state-file PATH]
                       [--cache-dir PATH]
agent-kit validate MANIFEST [--scope global|project] [--platform NAME]... [--cache-dir PATH]
agent-kit platforms [--all]
```

- `--platform NAME` (repeatable) intersects with `[options.platforms]` from
  the manifest and with `detect_installed_platforms()`; CLI flag wins if both
  are given (explicit beats declarative, same precedence rule as most CLIs
  layering flags over config files).
- `--scope` on the CLI overrides `[options].scope` in the manifest for a
  one-off run; neither is required (both default to `global`, matching
  `apply`/`apply_all`'s own default).
- Output uses `rich` (table of platform → written/planned/skipped paths),
  matching `witan setup`'s existing console output style.
- Exit codes: `0` success/no drift, `1` `validate` found drift or `apply` hit
  a `skipped` entry, `2` manifest failed to load (`ManifestError`).

### 4.1 `apply`

Thin wrapper: `load_manifest(path)` → (optionally prune per §5) →
`apply(platform, bundle, ...)` per selected platform, or `apply_all(bundle,
...)` if no `--platform` given and the manifest has no `[options.platforms]`
allow-list. Prints each platform's `InstallResult` (§4.1 of the base spec)
as a `rich` table. `--dry-run` passes straight through to `apply`/`apply_all`
— no new dry-run logic needed here, the orchestration layer already supports
it.

### 4.2 `validate` (drift detection)

New core function, not CLI-only logic (so it's usable from Python too):

```python
# agent_config_kit/diff.py

@dataclass
class Drift:
    platform: str
    path: Path
    missing_keys: list[str]     # in manifest, absent on disk (JSON capabilities)
    mismatched_keys: list[str]  # in manifest, present on disk, different value
    missing_paths: list[Path]   # in manifest, absent on disk (filesystem capabilities)

def diff(platform_name: str, bundle: RegistrationBundle, *, scope: Scope = Scope.GLOBAL) -> Drift: ...
```

`agent-config-kit`'s two capability shapes need two different comparison
strategies, since not everything `apply()` writes is a JSON key:

- **MCP servers and declarative hooks** are JSON-config keys. `diff()` loads
  each target file read-only (via the existing `jsonio.load_json_object`),
  computes what `apply()` *would* write per-key (reusing the same
  per-platform `mcp_serialize`/`hooks_merge` projections so there is exactly
  one place that knows a platform's wire format — no duplicated
  serialization logic between `apply()` and `diff()`), and compares against
  what's actually there, populating `missing_keys`/`mismatched_keys`.
- **Skills and plugin-file hooks** are files/directories copied to disk, not
  JSON keys — `apply()`'s own `install_skills` writes a `SKILL.md` per skill
  under one or more dest dirs. `diff()` checks filesystem existence at the
  same dest path(s) `apply()` would compute (via `installers.install_skills`'s
  own dest-path logic, called with `dry_run=True`-equivalent semantics so
  nothing is written) and records anything missing in `missing_paths`. v1
  drift detection for these is existence-only — it does **not** hash file
  contents to catch a hand-edited `SKILL.md`, matching M6's "additive,
  override-by-key" contract: `apply()` itself always overwrites on every
  run regardless of on-disk content, so content-level drift is never
  actionable information the way a missing file is.

`diff()` never writes. The CLI's `validate` command runs it per selected
platform, prints any non-empty `Drift`s, and sets the exit code accordingly
(§4).

### 4.3 `platforms`

Wraps `known_platforms()` / `detect_installed_platforms()` (already exported
from `agent_config_kit/__init__.py`) — lists registry entries and flags which are
detected-installed. `--all` shows every known platform; default shows only
detected ones. Exists mainly so a manifest author can sanity-check
`[options.platforms]` values without reading the registry source.

## 5. Prune/uninstall design (M7 detail)

This is the one genuinely new mechanism (`apply`/`apply_all` are pure
additive-merge today, spec §4.1 — nothing currently removes a previously
written key). Full implementation is
`tk-design-implement-prune-uninstall-for-entries-rem-4dfcdb`'s job; this
section fixes the shape so that task isn't starting from nothing.

**State file** (`<manifest>.lock.json` by default, `--state-file` to
override): written by `apply --prune` (never by plain `apply`, which stays
side-effect-free beyond the target configs themselves — pruning is opt-in).
Shape:

```json
{
  "manifest_hash": "sha256:...",
  "platforms": {
    "claude": {
      "mcp_servers": ["witan"],
      "hooks": ["declarative:user_prompt_submit:witan inject-context"],
      "skills": ["witan-task"]
    }
  }
}
```

Hook identity (since hooks have no natural name) is `f"{kind}:{event}:
{command}"` for `DeclarativeHook` and `f"plugin:{entry_path.name}"` for
`PluginRegistration` — stable across runs as long as the hook's own fields
don't change, which is the same granularity `apply()` already uses implicitly
(a hook that changes its `command` is, for merge purposes, a different hook).

**Prune algorithm**, per platform, on `apply --prune`:
1. Load the previous state file (absent → treat as empty, i.e. first run
   never prunes anything).
2. Run the normal `apply()` merge for the current manifest.
3. For each capability, compute `previous_keys - current_manifest_keys` —
   entries that were written before but are no longer in the manifest.
4. Remove exactly those keys from the target file/directory (delete the
   `mcpServers.<name>` entry, delete the matching hook by identity, delete
   the skill's destination directory) — never touch keys outside that set,
   so a key a human hand-added directly to `~/.claude.json` is left alone
   even though it wasn't in the previous *or* current manifest.
5. Update the state file by merging the current manifest's keys **for the
   platform(s) actually applied this run** into the state loaded in step 1,
   then write the merged result back — never overwrite the whole file
   wholesale. This matters because `apply --prune --platform claude` (a
   single-platform run) must not erase the `pi` entry a prior `apply --prune`
   (with no `--platform` filter, i.e. all detected platforms) recorded — the
   state file describes *all* platforms this manifest has ever pruned
   against, not just the ones touched by the current invocation.

This deliberately does **not** try to detect "a human hand-edited a key
`agent-config-kit` previously wrote" (e.g. changed the MCP server's `args`)
— on the next `apply`, override-by-key merge simply overwrites it back to
what the manifest says, which is the existing, already-shipped `apply()`
behavior (spec §4.1's `MergeStrategy.OVERRIDE_BY_KEY`); prune only concerns
*removed* entries, not *modified* ones.

**Open question left to the implementing task:** two manifests independently
targeting the same platform (e.g. two different projects both writing
`~/.claude.json` global MCP servers) will clobber each other's state files if
both default to `<manifest>.lock.json` next to their own manifest — the state
file describes "what *this* manifest wrote," which is correct per-manifest,
but nothing currently reconciles two state files against one shared target
file. Flagging, not solving here: v1's answer may simply be "state files
track per-manifest ownership; if two manifests target the same platform, use
`--state-file` to point both at an explicitly shared location, or accept that
prune only prunes what its own manifest last wrote."

## 6. Sequencing

Matches the already-created task graph under
`tk-add-manifest-driven-cli-to-agent-config-kit-cli--8772d0`:

1. `tk-scaffold-agent-config-kit-cli-cli-extra-packagin-c32d6a` — `cli` extra
   in `pyproject.toml` (M4), empty `agent_config_kit/cli.py` with the
   `cyclopts` app skeleton and `main()` entry point.
2. `tk-implement-manifest-file-loader-agent-config-kit--51aa58` —
   `agent_config_kit/manifest.py` per §3.2, its own tests
   (`tests/test_manifest.py`): valid manifest round-trip, path resolution
   relative to manifest dir (M5), malformed-TOML and schema-validation error
   wrapping.
3. `tk-implement-agent-config-kit-apply-cli-command-2f2cb9` — `apply` command
   per §4.1 (no `--prune` support yet — that's step 5).
4. `tk-implement-drift-detection-validate-core-diff-cli-717f35` —
   `agent_config_kit/diff.py`'s `diff()` (§4.2) + `validate` command.
5. `tk-design-implement-prune-uninstall-for-entries-rem-4dfcdb` — state file +
   prune algorithm per §5, wires `--prune` into the `apply` command from
   step 3.
6. `tk-tests-readme-docs-for-agent-config-kit-cli-020aef` — CLI-level
   integration tests (`apply`/`validate`/`platforms` against a temp home
   dir, matching the existing `tests/test_plan.py` fixture style) + README
   section documenting the manifest format and command surface, cross-linking
   this spec the way the README already cross-links
   `agent-config-kit-spec.md`.

## 7. Open questions for implementation

- Whether `[options.platforms]` in the manifest should be validated against
  `known_platforms()` at load time (fail fast on a typo'd platform name) or
  left to `apply()` (which today does a bare dict lookup and would raise
  `KeyError` on an unknown name) — leaning toward validating in
  `load_manifest()` with a clearer `ManifestError`, a 5-minute call for
  whoever implements step 2.
- Whether `validate`'s exit-code-1-on-drift behavior needs a `--quiet`/
  machine-readable (`--json`) output mode for CI use — not designed here;
  add if a real CI consumer asks for it (D7-style "extension point, not
  built until there's a real caller").
- The prune state-file's shared-target collision case (§5, last paragraph)
  is explicitly deferred to the implementing task, not resolved here.
