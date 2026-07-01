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
