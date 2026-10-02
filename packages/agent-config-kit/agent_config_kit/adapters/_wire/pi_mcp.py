# HAND-AUTHORED: Pi publishes no JSON Schema for mcp.json (per spec D6), so
# there is nothing to codegen from. This models the `mcpServers` entries of
# Pi's built-in MCP support (Pi 0.99 and later), reconciled 2026-10-02
# against docs/mcp.md and validateMcpServerConfig() in
# dist/core/mcp-servers.js of @earendil-works/pi-coding-agent 1.0.0.
# pi-mcp-adapter 5.x reads the same files through translatePiMcpServer()
# (config.ts), which accepts this shape and skips an entry Pi would reject.
#
# Earlier revisions modelled pi-mcp-adapter 2.x's own entry shape
# (lifecycle, idleTimeout, directTools, debug, a string `auth`, and
# oauth.redirectUri). Pi ignores the adapter-only keys, but its validator
# rejects a string `auth`, so they are gone rather than kept as harmless.
#
# Only the fields agent-config-kit writes or Pi validates are modelled;
# ``extra="forbid"`` intentionally rejects anything else, so an adapter that
# starts emitting a new field has to be reconciled here first. `type` stays
# out: Pi infers stdio from `command` and streamable HTTP from `url`.

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Exposure = Literal["direct", "deferred", "codemode", "codemode-deferred", "hidden"]


class OAuthConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    clientId: str | None = None
    clientSecret: str | None = None
    callbackPort: int | None = Field(default=None, ge=1, le=65535)
    callbackUrl: str | None = None
    scope: str | None = None
    clientName: str | None = Field(default=None, min_length=1)
    authServerMetadataUrl: str | None = None


class ProviderAuth(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    provider: str = Field(min_length=1)


class PiMcpServer(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    command: str | None = None
    args: list[str] | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    oauth: OAuthConfig | None = None
    auth: ProviderAuth | None = None
    exposure: Exposure | None = None
    toolExposure: dict[str, Exposure] | None = None
    enabled: bool | None = None
    timeout: float | None = Field(default=None, gt=0)
    description: str | None = None


class PiMcpConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    autoEnableCodemode: bool | None = None
    mcpServers: dict[str, PiMcpServer] | None = None
