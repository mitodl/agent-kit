# ol-agent-kit

Meta-package that installs the full mitodl agent toolkit in one shot:

- [`agent-config-kit[cli]`](../agent-config-kit/README.md) — the `agent-kit` CLI for
  applying `agent-config.toml` manifests across coding-agent platforms
- [`witan`](../../mcp/servers/witan/README.md) (PyPI: `witan-council`) — the agent
  memory, planning, and task-coordination MCP server
- [`witan-code`](../../mcp/servers/witan-code/README.md) — the tree-sitter code-graph
  MCP server, mounted under `witan code`

It carries no code of its own — it exists purely so `pip install ol-agent-kit` /
`uv tool install ol-agent-kit` pulls in all three at once. `agent-kit` was
already taken on PyPI (`agentkit` — PEP 503 normalizes hyphens/underscores/case,
so they collide), hence the `ol-` prefix on the *distribution* name; the
console script and command stay `agent-kit`.

```bash
uv tool install ol-agent-kit
agent-kit apply agent-config.toml
witan setup --agent claude
witan-code --help
```

## Versioning

Version bumps go through [`bump-my-version`](https://github.com/callowayproject/bump-my-version)
(config in `[tool.bumpversion]`), same as `agent-config-kit`, `witan`, and
`witan-code`. `dependencies` versions in `pyproject.toml` are open-ended
floors with no upper bound, so a new release of any of the three is picked up
by a fresh install automatically without needing a matching `ol-agent-kit`
release. The floors themselves move as the meta-package comes to depend on
newer behaviour — currently the two servers are floored at the releases that
speak MCP 2026-07-28 — so read them from `pyproject.toml` rather than from
here.
