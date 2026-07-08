# agent-config-kit

Unified interface for registering MCP servers, skills, and hooks/extensions
across coding-agent platforms (Claude Code, Pi, GitHub Copilot, OpenCode,
Kilo Code, ...) without reimplementing every agent's config-file quirks.

Canonical, capability-cluster `pydantic` models (`StdioServer`/`RemoteServer`,
`DeclarativeHook`/`PluginRegistration`, `SkillSource`, ...) describe *what* to
register; a small per-platform adapter and a shared read-merge-write
orchestration layer (`apply`/`apply_all`) handle *where* and in what wire
format each platform expects it.

```python
from agent_config_kit import RegistrationBundle, StdioServer, apply_all

bundle = RegistrationBundle(
    mcp_servers={"my-tool": StdioServer(command="uvx", args=["my-tool", "serve"])},
)
apply_all(bundle)
```

See `docs/design/agent-config-kit-spec.md` in this repo for the full design.

## CLI (`agent-kit`)

A project without its own Python tooling (a plain Node repo, a
shell-scripted dotfiles setup, a CI job) can drive the same
`RegistrationBundle`/`apply` machinery declaratively via a TOML manifest and
the `agent-kit` console script, gated behind the `cli` extra:

```bash
uv tool install 'agent-config-kit[cli]'
# or: pip install 'agent-config-kit[cli]'
```

### Manifest format

```toml
# agent-config.toml

instructions = "See AGENTS.md"    # optional — must come before any table/
                                   # array-of-tables header, or TOML parses
                                   # it as belonging to the preceding one

[options]
scope = "global"                  # "global" | "project" — default: "global"
platforms = ["claude", "pi"]       # optional allow-list; default: every
                                   # detected platform

[mcp_servers.witan]
kind = "stdio"
command = "uvx"
args = ["witan", "serve"]
env = { WITAN_AUTHOR = "team" }

[mcp_servers.hosted-tool]
kind = "remote"
url = "https://example.com/mcp"
transport = "streamable-http"      # "sse" | "http" | "streamable-http"

[[hooks]]
kind = "declarative"
event = "user_prompt_submit"       # see HookEvent for valid values
command = "witan inject-context"

[[hooks]]
kind = "plugin"
entry_path = "extensions/pi/witan.ts"   # resolved relative to this file

[skills]
validate-before-commit = "skills/validate-before-commit/SKILL.md"   # resolved relative to this file
cyclopts-cli-scripts   = "skills/cyclopts-cli-scripts/SKILL.md"

[profiles.universal]
mcp_servers = ["witan"]
skills = ["validate-before-commit"]

[profiles.python]
inherits = ["universal"]           # unions this profile's entries with its parents'
skills = ["cyclopts-cli-scripts"]
```

Table/field names mirror the Python model field names exactly
(`kind`, `command`, `args`, `env`, `event`, `entry_path`, ...) — see
`docs/design/agent-config-kit-cli-spec.md` for the full schema and rationale.

`[profiles.<name>]` tables (spec §4) each list a subset of entry keys drawn
from the manifest's own `[mcp_servers]`/`[skills]`/`[hooks]`/`[lsp_servers]`
tables, plus an optional `inherits` list of other profile names to union
with first. `agent-kit apply --profile <name>` (repeatable) resolves the
named profile(s), expanding `inherits` transitively; `[options]
default_profiles` sets the zero-flag default. Selecting no profile at all
(the default with neither flag nor `default_profiles` set) applies the
manifest's full, unfiltered bundle — profiles are opt-in filters, not
gates.

Skills follow the [Agent Skills specification](https://agentskills.io/specification):
`skill_md_path` must point to a file literally named `SKILL.md`, and its
parent directory is installed wholesale — `scripts/`, `references/`,
`assets/`, or any other supporting files alongside it are copied too, not
just `SKILL.md` itself. `name` must match the spec's frontmatter `name`
constraints (1-64 characters, lowercase alphanumeric segments separated by
single hyphens, no leading/trailing/consecutive hyphens) since it becomes
the installed skill's directory name.

#### Remote skill/hook sources

`skill_md_path` and `entry_path` also accept a remote URI instead of a
local, manifest-relative path — fetched and cached automatically, no
pre-cloning required:

```toml
[skills]
remote-skill = "https://raw.githubusercontent.com/org/repo/main/skills/remote-skill/SKILL.md"

[[hooks]]
kind = "plugin"
entry_path = "git+https://github.com/org/repo.git@v1.0.0#subdirectory=extensions/pi/witan.ts"
```

- A plain `https://`/`http://` URI fetches exactly one file — enough for
  `entry_path` or a single-file skill, but not a skill with supporting files
  (`scripts/`, `references/`, ...), since there's no directory on the other
  end of one GET.
- A `git+https://...#subdirectory=...` URI (optionally with `@ref` for a
  branch/tag/commit) shallow-clones the repo and resolves the subdirectory —
  use this for a skill that needs its full directory.
- Fetched sources are cached under `.agent-config-kit-cache/` next to the
  manifest by default (override with `agent-kit apply/validate --cache-dir`, or
  `load_manifest(path, cache_dir=...)` from Python). Every `apply`/`validate`
  re-fetches; HTTP(S) uses a conditional GET so an unchanged remote is a
  cheap 304, and a transient network failure falls back to the last good
  cache instead of failing a run that would otherwise have worked offline.
- `git` must be on `PATH` for `git+` URIs — there's no pure-Python git
  client dependency here, by design (no new dependency on the base
  package).

### `agent-kit apply`

Applies a manifest's MCP servers, hooks, and skills to one or more platforms:

```bash
agent-kit apply agent-config.toml
agent-kit apply agent-config.toml --platform claude --platform pi
agent-kit apply agent-config.toml --scope project --dry-run
agent-kit apply agent-config.toml --profile python
```

- `--platform NAME` (repeatable) overrides the manifest's
  `[options.platforms]`; with neither given, every detected platform is
  targeted.
- `--scope global|project` overrides the manifest's `[options].scope` for
  this run.
- `--profile NAME` (repeatable) selects one or more `[profiles.*]` entries,
  unioning their entries and expanding `inherits`; overrides the manifest's
  `[options].default_profiles`. Neither given applies the manifest's full,
  unfiltered bundle.
- `--dry-run` reports what would be written/removed without touching disk.
- `--prune` also removes entries a *previous* `apply --prune` of this same
  manifest wrote but that have since been dropped from it (e.g. a deleted
  `[mcp_servers.*]` table, a removed skill or hook). This is opt-in and only
  ever removes what it can prove it wrote itself, tracked in a state file
  (`<manifest>.lock.json` by default, override with `--state-file PATH`) — a
  manifest's first-ever `--prune` run removes nothing, since there's no
  recorded state yet to diff against, and a hand-edited key that's absent
  from both the previous and current manifest is never touched.
- `--cache-dir PATH` overrides where remote skill/hook sources are fetched
  and cached (default: `.agent-config-kit-cache` next to the manifest).

Exit codes: `0` success, `1` a platform's target couldn't be parsed as JSON,
`2` the manifest failed to load.

### `agent-kit validate`

Reports drift between a manifest and each platform's on-disk config, without
writing anything — useful in CI to catch configuration that's fallen out of
sync:

```bash
agent-kit validate agent-config.toml
agent-kit validate agent-config.toml --platform claude
```

Missing MCP servers/hooks, mismatched MCP server values, and missing skill/
plugin-hook files are all reported as drift; a target that fails to parse as
JSON is reported separately (not as drift, since there's nothing to compare).
`validate` never writes — pair it with `apply --prune` to actually
reconcile.

Exit codes: `0` no drift, `1` drift (or an unreadable target) found, `2` the
manifest failed to load.

### `agent-kit profiles`

Lists a manifest's `[profiles.*]` entries and each one's resolved entry
counts (after expanding `inherits`), without applying anything:

```bash
agent-kit profiles agent-config.toml
```

Prints a table of profile name, `inherits`, and the resolved
`mcp_servers`/`skills`/`hooks`/`lsp_servers` counts. Prints a short message
instead if the manifest defines no profiles.

Exit codes: `0` success, `2` the manifest failed to load.

### `agent-kit config init`

Bootstraps the global config file (`${XDG_CONFIG_HOME:-~/.config}/agent-config-kit/config.toml`,
overridable with `--config` or `AC_KIT_CONFIG`) — see
`docs/design/agent-config-kit-profiles-composition-spec.md` §7 for the full
schema this file drives (per-org and per-directory-prefix default manifests
for zero-arg `apply`).

```bash
agent-kit config init            # writes every key as a commented-out example
agent-kit config init --wizard   # interactively prompt for values instead
agent-kit config init --force    # overwrite an existing config file
```

Refuses to overwrite an existing file unless `--force` is given.
