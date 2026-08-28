<!--
  GENERATED FILE — DO NOT EDIT BY HAND.
  Regenerate with `just docs-gen` (or `./bin/gen_docs.py`).
  Source of truth: the registered FastMCP tool objects
-->

# MCP tools

witan exposes **63 MCP tools** across four domains. A single `witan serve` mounts all of them, so one MCP entry in your agent's config gets you the whole surface.

| Domain | Tools | What it covers |
| --- | --- | --- |
| [Memory & recall](memory.md) | 15 | Recording durable knowledge in the shared graph, and reading it back. |
| [Tasks](tasks.md) | 11 | The work-coordination layer: what needs doing, what blocks what, and who holds which piece of work right now. |
| [Workflow projects & sessions](workflow.md) | 19 | Tracking an engineering objective across many agent sessions without an explicit hand-off. |
| [Code graph](code.md) | 18 | Exact symbol lookups, caller graphs, change-impact analysis, and cross-repo contract tracing, served from a tree-sitter index. |
