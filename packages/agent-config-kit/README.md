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

## CLI (`ac-kit`)

A project without its own Python tooling (a plain Node repo, a
shell-scripted dotfiles setup, a CI job) can drive the same
`RegistrationBundle`/`apply` machinery declaratively via a TOML manifest and
the `ac-kit` console script, gated behind the `cli` extra:

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

[[skills]]
name = "witan-task"
skill_md_path = "skills/witan-task/SKILL.md"   # resolved relative to this file
```

Table/field names mirror the Python model field names exactly
(`kind`, `command`, `args`, `env`, `event`, `entry_path`, ...) — see
`docs/design/agent-config-kit-cli-spec.md` for the full schema and rationale.

Skills follow the [Agent Skills specification](https://agentskills.io/specification):
`skill_md_path` must point to a file literally named `SKILL.md`, and its
parent directory is installed wholesale — `scripts/`, `references/`,
`assets/`, or any other supporting files alongside it are copied too, not
just `SKILL.md` itself. `name` must match the spec's frontmatter `name`
constraints (1-64 characters, lowercase alphanumeric segments separated by
single hyphens, no leading/trailing/consecutive hyphens) since it becomes
the installed skill's directory name.

### `ac-kit apply`

Applies a manifest's MCP servers, hooks, and skills to one or more platforms:

```bash
ac-kit apply agent-config.toml
ac-kit apply agent-config.toml --platform claude --platform pi
ac-kit apply agent-config.toml --scope project --dry-run
```

- `--platform NAME` (repeatable) overrides the manifest's
  `[options.platforms]`; with neither given, every detected platform is
  targeted.
- `--scope global|project` overrides the manifest's `[options].scope` for
  this run.
- `--dry-run` reports what would be written/removed without touching disk.
- `--prune` also removes entries a *previous* `apply --prune` of this same
  manifest wrote but that have since been dropped from it (e.g. a deleted
  `[mcp_servers.*]` table, a removed skill or hook). This is opt-in and only
  ever removes what it can prove it wrote itself, tracked in a state file
  (`<manifest>.lock.json` by default, override with `--state-file PATH`) — a
  manifest's first-ever `--prune` run removes nothing, since there's no
  recorded state yet to diff against, and a hand-edited key that's absent
  from both the previous and current manifest is never touched.

Exit codes: `0` success, `1` a platform's target couldn't be parsed as JSON,
`2` the manifest failed to load.

### `ac-kit validate`

Reports drift between a manifest and each platform's on-disk config, without
writing anything — useful in CI to catch configuration that's fallen out of
sync:

```bash
ac-kit validate agent-config.toml
ac-kit validate agent-config.toml --platform claude
```

Missing MCP servers/hooks, mismatched MCP server values, and missing skill/
plugin-hook files are all reported as drift; a target that fails to parse as
JSON is reported separately (not as drift, since there's nothing to compare).
`validate` never writes — pair it with `apply --prune` to actually
reconcile.

Exit codes: `0` no drift, `1` drift (or an unreadable target) found, `2` the
manifest failed to load.
