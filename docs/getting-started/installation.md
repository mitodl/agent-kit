# Installation

## Pick an install shape

Which one you want depends on whether your agent platform needs `witan` on
`PATH`.

=== "Claude Code / Pi"

    These shell out to `witan` directly from hooks and extensions, so it has to
    stay installed:

    ```bash
    uv tool install ol-agent-kit
    witan setup --agent claude      # or: pi
    ```

    `ol-agent-kit` is a meta-package — it pulls in `witan-council`,
    `witan-code`, and the `agent-kit` CLI together. To install only the
    coordination graph without the code index, use `uv tool install
    witan-council` instead.

=== "Copilot / OpenCode / Kilo"

    These launch the MCP server via `uvx` on demand, so nothing needs to remain
    installed:

    ```bash
    uvx --from ol-agent-kit witan setup --agent copilot   # or: opencode | kilo
    ```

=== "Tracking unreleased code"

    To run ahead of the latest PyPI release, install from the repository:

    ```bash
    uv tool install \
      "git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan"
    witan setup --agent claude
    ```

!!! tip "`witan-council`, not `witan`"

    The PyPI project is named `witan-council` — `witan` was already taken. The
    import path, the console command, and every tool and CLI name are still
    `witan`. Only the install artifact's name differs.

## What `witan setup` does

Four things, in order:

1. Downloads the pinned `omnigraph` binary to `~/.local/bin/omnigraph`, unless
   it is already there. witan shells out to this binary for every graph
   operation, so it must be present.
2. Writes a starter `~/.config/witan/config.toml`, with every optional setting
   commented out at its real default — unless a config already exists, which is
   never overwritten.
3. Copies the bundled skills and hooks into the agent's config directories
   (`~/.claude/skills/`, `~/.claude/hooks/`, and equivalents).
4. Merges the witan MCP server entry into that agent's config file.

Useful flags:

```bash
witan setup --dry-run                  # show what would change, write nothing
witan setup --author "Your Name"       # set graph attribution up front
witan setup --agent all                # register with every detected platform
```

!!! warning "Re-run it after every upgrade"

    Steps 3 and 4 copy files rather than linking them, and step 1 pins a
    specific `omnigraph` version. Upgrading the package does not refresh any of
    that on its own — re-run `witan setup` so the installed skills, hooks, and
    binary match the version you just installed.

## Verify

```bash
witan --version
witan tasks
```

From inside a git repository, `witan tasks` should print an empty ready-work
list rather than an error. If it complains about the store, check
[`WITAN_MEMORY_URI`](../reference/environment.md) — by default the graph lives
at `~/.local/share/witan/graph.omni` and is created on first use.

To confirm your *agent* can see the tools, start a session and ask it to call
`recall`. In Claude Code, `/witan-task` also lists the task tools if the skills
installed correctly.

## Attribution

Every node you create records an author. It resolves in this order:

1. [`WITAN_AUTHOR`](../reference/environment.md)
2. `author` in `~/.config/witan/config.toml`
3. `git config user.name`
4. `$USER`

Worth setting deliberately if you share a store with a team — it is how anyone
later works out who recorded a lesson, and who is holding a task.

---

**Next:** [Your first memory →](first-memory.md)
