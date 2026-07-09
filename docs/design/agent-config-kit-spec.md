# Cross-agent coding-agent config management library — Spec

Project: `wp-cross-agent-coding-agent-config-management-libra-5593b0`
Phase: spec
Status: design only — nothing here is implemented yet.

Reference implementation: [`mcp/servers/witan/witan/setup.py`](../../mcp/servers/witan/witan/setup.py)
(installers) and [`mcp/servers/witan/witan/cli/setup_cmd.py`](../../mcp/servers/witan/witan/cli/setup_cmd.py)
(CLI wiring), plus its tests in
[`mcp/servers/witan/tests/test_setup.py`](../../mcp/servers/witan/tests/test_setup.py).

**Supersession note:** an earlier draft of this document scoped the package to
witan's original 5 platforms with an ad hoc `InstallPlan`/`AgentSpec` model.
Before that draft, a separate pair of tasks under this same project
(`tk-design-public-api-data-model-for-the-agent-regis-cb0714`,
`tk-evaluate-datamodel-code-generator-for-per-agent--40c6e6`) had already
surveyed 13–14 real coding-agent harnesses by cloning and reading their source,
and validated a richer capability-cluster data model. That work was recorded
only in witan memory (`pf-agent-config-kit-coalesced-shared-model-design-f-3e10fd`,
`pat-agent-config-kit-validated-pydantic-model-design-3f488e`,
`pf-datamodel-code-generator-survey-schema-availabil-993466`,
`pf-agent-config-kit-pi-coding-agent-survey-no-built-dd1851`,
`pf-agent-config-kit-consolidated-file-path-name-ref-aa4dca`) and was missed
when the first draft was written. This revision replaces §3–§5 of that draft
with the validated design as the basis, and folds in the remaining sections
(PyPI, sequencing) with adjustments where the richer model changes them.

## 1. Goal

No Python package exists (gap confirmed June 2026) that lets a tool register
itself — MCP servers, instructions/rules files, hooks/extensions, LSP servers,
skills — across the coding-agent landscape without reimplementing every
agent's config-file quirks. `witan/setup.py` solved a narrow slice of this
once (5 platforms, MCP + skills + hooks only, ~150 lines). A source-level
survey of 13–14 harnesses (Claude Code, Pi, GitHub Copilot/VS Code, OpenCode,
Kilo Code, Cline, Roo Code, Continue, Aider, Zed, Goose, Codex CLI, Gemini
CLI) shows the underlying structures are genuinely shared *by capability*,
not by agent — this package extracts that shared structure into a standalone,
reusable library, generalized well beyond what witan itself currently needs.

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | Package name | **`agent-config-kit`** (import name `agent_config_kit`). Confirmed unclaimed on PyPI as of 2026-07-01 (`https://pypi.org/pypi/agent-config-kit/json` → 404). **Re-check availability immediately before the first publish.** |
| D2 | Repo location | New top-level `packages/agent-config-kit/` in this repo (not a new repo, not under `mcp/servers/` — that tree is MCP-server-specific per `mcp/README.md`, and this isn't an MCP server). Mirrors the existing `mcp/servers/witan-code/` pattern: independent `pyproject.toml`, own test suite, consumed by `witan` as a path/registry dependency. Adds a new `packages/` row to `AGENTS.md`'s repository layout. |
| D3 | License / build backend | `BSD-3-Clause` (repo convention), `hatchling` build backend, `uv` for env management, `ruff` for lint — matching `witan`/`witan-code`. |
| D4 | Python version | `>=3.11`, matching both existing packages. Public API is `pydantic` v2 models (v2.13+ used during design validation). |
| D5 | Data model shape | **Capability-cluster canonical models, not one model per agent.** MCP servers, instructions files, hooks, LSP servers, and skills each get one (or two, for instructions) shared, hand-authored `pydantic` model covering the union of real-world shapes; per-agent differences are adapter-layer projections, not separate models. This is the actual finding of the harness survey (§3) — field-name variance (`env` vs `environment`), polarity inversion (`disabled` vs `enabled`), and structural variance (command-as-string vs command-as-array) are all adapter concerns, never fork the canonical model. |
| D6 | Per-platform wire-format models | Generate with `datamodel-code-generator` from each platform's published JSON Schema (Claude Code `settings.json`, OpenCode `config.json`) where one exists; hand-author a minimal JSON Schema for the fields this library actually writes where no upstream schema exists (Pi, Kilo Code, VS Code/Copilot `mcp.json`), then codegen from that. These generated models are **adapter-internal implementation detail**, never part of the public API, and are **vendored/committed**, regenerated on demand — not CI-enforced drift checking, since roughly half the platforms have no live schema to diff against. |
| D7 | v1 registry population vs. modeled-but-inert capabilities | The canonical models (§4) are designed to generalize across all 13–14 surveyed platforms without a redesign, but **v1 only populates registry entries for the 5 platforms witan already targets** (Claude Code, Pi, GitHub Copilot, OpenCode, Kilo Code) — matching real, current demand. LSP and instructions-file registration are real capabilities in the model but have **no v1 populated entry and no v1 caller** (witan's `setup.py` never registers either today); they exist so a future consumer, or a future platform addition, doesn't require a data-model change. Adding one of the other 8 surveyed platforms (Cline, Roo Code, Continue, Aider, Zed, Goose, Codex CLI, Gemini CLI) later is "write a registry entry + adapter," not "redesign the library." |
| D8 | Scope (global vs. project) dimension | Every capability gets an optional global `ScopeTarget` *and* an optional project `ScopeTarget`, plus a `MergeStrategy` (override-by-key / concatenate / deep-merge) — modeled per §4, because scope support and merge semantics are genuinely non-uniform across platforms (Roo Code has per-project MCP, its ancestor Cline does not; instructions files concatenate up a directory walk while MCP entries override by key). **v1 only writes at global scope** — witan's `setup.py` never writes project-scoped config today — but the model carries `project` so registering into `.mcp.json`/`.pi/mcp.json`/etc. later is additive, not a breaking change. |

## 3. Scope: capability clusters (from the 13–14-harness survey)

Full survey detail lives in witan memory (`pf-agent-config-kit-coalesced-shared-model-design-f-3e10fd`
for the conceptual synthesis, `pf-agent-config-kit-consolidated-file-path-name-ref-aa4dca`
for the concrete per-platform path table, `pf-agent-config-kit-pi-coding-agent-survey-no-built-dd1851`
for Pi's specific caveats). Summary:

1. **MCP servers** — one shared discriminated-union model, `StdioServer` /
   `RemoteServer`, covers ~12 of 13 surveyed tools (all but Aider, which has
   no MCP support at all). Field-name and structural variance (`env` vs
   `environment`; command-as-string vs command-folded-into-array on
   OpenCode/Kilo Code; `type`-field requirement on Claude Code/Copilot only)
   is adapter-layer, not modeled. **Approval/trust granularity is NOT
   unifiable into one field** — it genuinely varies (boolean trust, tool
   allow-list, per-tool approval mode, a wholly separate permission file, or
   no granularity at all) — modeled as a normalized `ApprovalPolicy` that
   each adapter projects down to its native shape.
2. **Instructions/rules files** — two distinct shared families: (a)
   plain-file convention (`AGENTS.md` is the emergent lowest-common-
   denominator name; read-by-default varies by platform, and the *search
   strategy* — upward directory walk vs. fixed first-match-across-candidates
   vs. simple global+project — is a separate axis from the filename list);
   (b) frontmatter-scoped rules (Continue/Cline-style: `name`/`description`/
   `globs`/`regex`/`alwaysApply` + body, appliable per-file by glob).
3. **Hooks** — one shared declarative command-hook model
   (`event`/`matcher?`/`command`/`timeout?`) covers Claude Code, Continue,
   Codex CLI, Gemini CLI, and (with light adaptation) Goose/Cline. A
   *structurally different* plugin-registration model (points at a JS/TS
   file, imperative event-subscription API — no JSON config at all) covers
   OpenCode, Kilo Code, and Pi. These are two real models, not one model
   with an optional field.
4. **LSP** — narrow: only Claude Code, OpenCode, and Kilo Code have a real
   agent-owned LSP registration surface; the other 10 surveyed platforms have
   none (either per-extension, like VS Code, or nonexistent). Modeled as an
   optional per-platform capability, not a required field on every registry
   entry. v1 has no populated entry or caller (D7).
5. **Skills** — least standardized; matches witan's existing single
   `SKILL.md`-per-directory convention already. No new model needed.

Cross-cutting **scope** dimension (D8) applies to all five clusters above.

Explicitly **out of scope** (stays in `witan`, not extracted):
- `install_omnigraph` / `_download_omnigraph` and the omnigraph
  version/asset tables — witan's own binary-distribution concern.
- Witan's actual MCP entry values (`uvx` invocation, `WITAN_AUTHOR` env) and
  hook commands (`witan inject-context`, `witan session-checkpoint`) — the
  library takes fully-formed model instances from the caller.
- A previously-flagged, unrelated bug: witan's current `install_kilo()`
  appears to write to the wrong file (VS Code `settings.json`'s
  `kilocode.mcpServers`, vs. Kilo Code's actual current
  `kilo.json`/`opencode.json` or legacy `mcp_settings.json` — unconfirmed
  which). Tracked separately as `tk-verify-witan-install-kilo-writes-to-the-correct--5bf64e`,
  not part of this package's design, but **the Kilo Code registry entry in
  this library should be built from the verified answer to that task, not
  copied from witan's possibly-stale current behavior** — flagged again in
  §6's open questions.

## 4. Public API — validated canonical models

This is the design validated in witan memory
(`pat-agent-config-kit-validated-pydantic-model-design-3f488e`): it was
executed under `uv run --with pydantic` (pydantic 2.13.4), including
serializing one real `StdioServer` instance (witan's own MCP entry) through
four different adapter functions to Claude Code's, Cline's, Gemini CLI's, and
a Pi-plugin's actual native JSON shapes, and round-tripping the discriminated
union through `model_dump`/`model_validate`. Reproduced here as the library's
target public API.

```python
from __future__ import annotations
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Union
from pydantic import BaseModel, Field

# Capability 1: MCP servers — discriminated union, covers 12/13 surveyed platforms.

class ApprovalMode(str, Enum):
    ALWAYS_ALLOW = "always_allow"; ASK = "ask"; NEVER = "never"

class ApprovalPolicy(BaseModel):
    mode: ApprovalMode = ApprovalMode.ASK
    allowed_tools: list[str] | None = None

class StdioServer(BaseModel):
    kind: Literal["stdio"] = "stdio"
    command: str
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float | None = None
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)

class RemoteServer(BaseModel):
    kind: Literal["remote"] = "remote"
    url: str
    transport: Literal["sse", "http", "streamable-http"] = "streamable-http"
    headers: dict[str, str] = Field(default_factory=dict)
    oauth: dict[str, Any] | None = None
    timeout_seconds: float | None = None
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)

McpServer = Annotated[Union[StdioServer, RemoteServer], Field(discriminator="kind")]

# Capability 2a: plain-file instructions (AGENTS.md-style)
class SearchStrategy(str, Enum):
    UPWARD_WALK = "upward_walk"; FIRST_MATCH = "first_match"; FIXED_DIRS = "fixed_dirs"

class InstructionsConfig(BaseModel):
    candidate_filenames: list[str]
    search_strategy: SearchStrategy
    read_by_default: bool = True  # False for Gemini CLI (opt-in only)

# Capability 2b: frontmatter-scoped rules (Continue/Cline-style)
class FrontmatterRule(BaseModel):
    name: str; rule: str; description: str | None = None
    globs: list[str] | None = None; regex: list[str] | None = None
    always_apply: bool = False

# Capability 3: hooks
class HookEvent(str, Enum):
    PRE_TOOL_USE = "pre_tool_use"; POST_TOOL_USE = "post_tool_use"
    SESSION_START = "session_start"; SESSION_END = "session_end"
    USER_PROMPT_SUBMIT = "user_prompt_submit"; STOP = "stop"

class DeclarativeHook(BaseModel):
    """Claude Code, Continue, Codex CLI, Gemini CLI, adapted Goose/Cline."""
    kind: Literal["declarative"] = "declarative"
    event: HookEvent; matcher: str | None = None; command: str
    timeout_seconds: float | None = None

class PluginRegistration(BaseModel):
    """OpenCode, Kilo Code, Pi — imperative callback files, not JSON."""
    kind: Literal["plugin"] = "plugin"
    entry_path: Path

Hook = Annotated[Union[DeclarativeHook, PluginRegistration], Field(discriminator="kind")]

# Capability 4: LSP (v1 scope: modeled, no populated entry/caller yet — see D7)
class LspServer(BaseModel):
    command: list[str]
    extensions: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    initialization_options: dict[str, Any] = Field(default_factory=dict)
    disabled: bool = False
    settings: dict[str, Any] | None = None
    fetch: dict[str, Any] | None = None

# Capability 5: skills (matches witan's existing SKILL.md convention)
class SkillSource(BaseModel):
    name: str; skill_md_path: Path

# Cross-cutting: scope (global vs project), D8
class Scope(str, Enum):
    GLOBAL = "global"; PROJECT = "project"

class MergeStrategy(str, Enum):
    OVERRIDE_BY_KEY = "override_by_key"
    CONCATENATE = "concatenate"
    DEEP_MERGE = "deep_merge"           # e.g. Pi's settings.json

class ScopeTarget(BaseModel):
    path: Path
    key_path: tuple[str, ...] = ()      # e.g. ("mcpServers",) or ("hooks","PreToolUse")

class CapabilityScope(BaseModel):
    global_: ScopeTarget | None = Field(default=None, alias="global")
    project: ScopeTarget | None = None
    merge_strategy: MergeStrategy = MergeStrategy.OVERRIDE_BY_KEY
    model_config = {"populate_by_name": True}

class AgentPlatform(BaseModel):
    name: str
    detect: Callable[[], bool] | None = None   # None => assumed always installed
    mcp: CapabilityScope | None = None
    mcp_conditional_on: str | None = None      # e.g. Pi: "requires a third-party MCP plugin"
    hooks: CapabilityScope | None = None
    instructions: InstructionsConfig | None = None
    skills: CapabilityScope | None = None
    lsp: CapabilityScope | None = None
    model_config = {"arbitrary_types_allowed": True}
```

**Implementation note carried over from validation:** `CapabilityScope.global_`'s
alias (`"global"`) is not applied by `model_dump_json()` by default — call
sites need `by_alias=True`, or switch to `serialization_alias`. Flagged during
validation, not yet fixed — will bite silently if forgotten in the real
package.

### 4.1 The missing layer: read-merge-write orchestration (`register_*`)

The validated design above covers the *data model*; it explicitly deferred
the functions that actually perform file I/O against these models
(`tk-design-public-api-data-model-for-the-agent-regis-cb0714`'s own
resolution: "the actual `register_mcp_server()`/`register_hook()`/etc.
public functions... this design covers the DATA MODEL and adapter-function
shape, not the file-I/O orchestration layer"). `witan/setup.py` already has
working reference code for exactly that layer (`_load_json_object`,
`_write_json`, JSONC tolerance, additive-merge semantics) — this spec reuses
and generalizes it rather than reinventing it:

```python
@dataclass
class InstallResult:
    platform: str
    written: list[Path]              # files actually written (empty when dry_run)
    planned: list[Path]              # files that would be written (always populated)
    skipped: list[tuple[Path, str]]  # (path, reason) — e.g. unparsable JSON

@dataclass
class RegistrationBundle:
    """What a consumer (e.g. witan) wants installed, in canonical-model terms."""
    mcp_servers: dict[str, McpServer] = field(default_factory=dict)
    hooks: list[Hook] = field(default_factory=list)
    skills: list[SkillSource] = field(default_factory=list)
    lsp_servers: dict[str, LspServer] = field(default_factory=dict)   # unpopulated in v1 callers
    instructions: str | None = None                                   # unpopulated in v1 callers

def known_platforms() -> list[str]: ...             # v1: claude, pi, copilot, opencode, kilo
def detect_installed_platforms() -> list[str]: ...   # AgentPlatform.detect() over the registry

def apply(platform: str, bundle: RegistrationBundle, *, scope: Scope = Scope.GLOBAL,
          dry_run: bool = False) -> InstallResult: ...
def apply_all(bundle: RegistrationBundle, *, scope: Scope = Scope.GLOBAL,
              dry_run: bool = False) -> dict[str, InstallResult]: ...

# Exported JSON helpers — bespoke callers can reuse these directly.
def load_json_object(path: Path) -> dict | None: ...
def write_json(path: Path, data: dict, dry_run: bool) -> None: ...
```

`apply()` looks up `AGENTS[platform]`, and for each populated field on the
`RegistrationBundle` whose corresponding `AgentPlatform` capability is not
`None`, merges the model's serialized form into `capability_scope`'s target
file at `key_path`, using `merge_strategy`, JSONC-tolerant per witan's
existing `_load_json_object`. Adapter-specific quirks (Claude Code/Copilot
requiring a literal `"type": "stdio"` field; OpenCode/Kilo Code folding
`command`+`args` into one array; polarity inversion on `disabled`/`enabled`)
live in small per-platform serialization functions called by `apply()`, not
in the canonical models themselves — this is the layering
`pat-agent-config-kit-validated-pydantic-model-design-3f488e` established:
canonical models are the public API, wire-format specifics are adapter-
internal (and, per D6, some of those wire-format models are codegen'd from
schema rather than hand-written).

## 5. v1 registry (5 platforms populated; 8 more are extension points)

Concrete registry entries for the platforms witan actually uses today,
built from the survey's per-platform path table
(`pf-agent-config-kit-consolidated-file-path-name-ref-aa4dca`):

| Platform | MCP scope | MCP quirks | Hooks | Skills |
|---|---|---|---|---|
| **Claude Code** | global `~/.claude.json` → `mcpServers` key, override-by-key | Requires literal `"type": "stdio"` field (adapter-injected, not modeled) | `DeclarativeHook` list merged into `~/.claude/settings.json` → `hooks` key | `~/.claude/skills/` |
| **Pi** | global `~/.pi/agent/mcp.json` → `mcpServers`; project `.pi/settings.json` (both scopes populated — honored by both surveyed third-party plugins) | `mcp_conditional_on="requires a third-party MCP plugin (pi-mcp-extension or pi-mcp-adapter); writing this file is a silent no-op without one"` | `PluginRegistration` (`.ts` files) into `~/.pi/agent/extensions/` | `~/.pi/agent/skills/` only — Pi natively unions this with `~/.agents/skills/` itself, so writing there too would duplicate the skill and trigger Pi's own name-collision warning |
| **GitHub Copilot / VS Code** | global `<vscode-user-dir>/mcp.json` → `servers` key | Requires literal `"type": "stdio"` field (adapter-injected) | none (per-extension LSP/hook model, nothing generic to target) | none generic |
| **OpenCode** | root config, `mcp` key (exact on-disk filename/global dir not fully pinned by the survey — confirm during implementation) | No `type` field; command/args folding TBD per adapter | `PluginRegistration` under `{plugin,plugins}/*.{ts,js}` | `skills: string[]` array in root config, not a fixed directory |
| **Kilo Code** | **Unverified — see open question below; do not copy witan's current `kilocode.mcpServers`-in-VS-Code-settings.json behavior without confirming first** | — | `PluginRegistration` (opencode-derived plugin engine) | presumed same as OpenCode's `skills` key, unconfirmed |

The other 8 surveyed platforms (Cline, Roo Code, Continue, Aider, Zed, Goose,
Codex CLI, Gemini CLI) have their full path/quirk data already captured in
`pf-agent-config-kit-consolidated-file-path-name-ref-aa4dca` and are not
populated in the v1 `AGENTS` registry — adding one later is a new
`AgentPlatform` entry plus its adapter, not a model change (D7).

## 6. Extraction plan (answers `tk-plan-extraction-of-setup-py-logic-into-standalon-f3043f`)

### 6.1 New package skeleton

```
packages/agent-config-kit/
├── pyproject.toml          # name="agent-config-kit", hatchling, BSD-3-Clause, py>=3.11
├── agent_config_kit/
│   ├── __init__.py         # re-exports canonical models + register_* API
│   ├── models.py           # StdioServer/RemoteServer/McpServer, Hook union, InstructionsConfig,
│   │                       # FrontmatterRule, LspServer, SkillSource, Scope/CapabilityScope/MergeStrategy
│   ├── registry.py         # AgentPlatform, AGENTS (5 populated in v1), known_platforms, detect_installed_platforms
│   ├── jsonio.py           # load_json_object, write_json (moved verbatim from witan/setup.py)
│   ├── paths.py            # vscode_user_dir() (moved verbatim)
│   ├── installers.py       # install_skills, install_files (moved, generalized to N dest dirs)
│   ├── adapters/           # one module per v1 platform: wire-format projection + quirks
│   │   ├── claude.py       # type-field injection, hooks merge into settings.json
│   │   ├── pi.py           # mcp_conditional_on handling, dual skill dest dirs
│   │   ├── copilot.py      # type-field injection
│   │   ├── opencode.py     # command/args folding TBD
│   │   └── kilo.py         # BLOCKED on tk-verify-witan-install-kilo-writes-to-the-correct--5bf64e
│   └── plan.py             # RegistrationBundle, InstallResult, apply, apply_all
└── tests/
    ├── test_models.py
    ├── test_registry.py
    ├── test_jsonio.py
    └── test_plan.py
```

### 6.2 Function-by-function move

| Today (`witan/setup.py`) | Becomes | Notes |
|---|---|---|
| `_load_json_object` | `agent_config_kit.jsonio.load_json_object` | Verbatim — already fully generic, including JSONC best-effort strip. |
| `_write_json` | `agent_config_kit.jsonio.write_json` | Verbatim. |
| `_vscode_user_dir` | `agent_config_kit.paths.vscode_user_dir` | Verbatim. |
| `_install_skills` | `agent_config_kit.installers.install_skills` | Generalized to N dest dirs (Pi needs 2). |
| `_install_files` | `agent_config_kit.installers.install_files` | Verbatim, already generic. |
| `is_pi_installed`, `is_copilot_installed`, `is_opencode_installed`, `is_kilo_installed` | `AgentPlatform.detect` closures in `registry.py` | Claude's `lambda: True` also becomes a registry entry — single source of truth, no special-casing in `apply_all`/`detect_installed_platforms`. |
| `install_claude`, `install_pi`, `install_copilot`, `install_opencode`, `install_kilo` | `agent_config_kit.plan.apply`, registry- and adapter-driven | Witan's actual MCP entry / hook commands / skill+hook source dirs move into the `RegistrationBundle` witan constructs (§6.3). Per-platform wire-format quirks move into `adapters/<platform>.py`, not into `apply()` itself. |
| `_merge_claude_hooks` | folded into `adapters/claude.py`'s hook-merge function | Same merge logic; hook list now comes from `RegistrationBundle.hooks` (filtered to `DeclarativeHook`) instead of a hardcoded tuple. |
| `_mcp_entry`, `install_omnigraph`, `_download_omnigraph`, `_OMNIGRAPH_VERSION`, `_OMNIGRAPH_ASSETS` | **stays in `witan/setup.py`** | Out of scope (§3). |

### 6.3 `witan/setup.py` after extraction

```python
def _witan_bundle(pkg_dir: Path, author: str) -> RegistrationBundle:
    return RegistrationBundle(
        mcp_servers={"witan": StdioServer(command="uvx", args=_WITAN_ARGS, env={"WITAN_AUTHOR": author})},
        skills=[SkillSource(name=d.name, skill_md_path=d / "SKILL.md") for d in sorted((pkg_dir / "skills").iterdir())],
        hooks=[
            DeclarativeHook(event=HookEvent.USER_PROMPT_SUBMIT, command="witan inject-context"),
            DeclarativeHook(event=HookEvent.STOP, command="witan session-checkpoint"),
            PluginRegistration(entry_path=pkg_dir / "extensions" / "pi" / "witan.ts"),
        ],
    )

def setup(agent: str, author: str, *, dry_run: bool = False) -> None:
    bundle = _witan_bundle(pkg_dir, author)
    if agent == "all":
        apply_all(bundle, dry_run=dry_run)
    else:
        apply(agent, bundle, dry_run=dry_run)
```

`install_omnigraph`/`_download_omnigraph` and the version/asset tables remain
untouched in `witan/setup.py`. The five wrapper functions
(`install_claude`/`install_pi`/etc.) are deleted outright — `setup_cmd.py`
calls `apply`/`apply_all`/`detect_installed_platforms` directly (it's the
only caller today).

`witan`'s `pyproject.toml` gains:
```toml
dependencies = [
    "agent-config-kit>=0.1,<1",
    ...
]
[tool.uv.sources]
agent-config-kit = { path = "../agent-config-kit", editable = true }  # until first PyPI release
```
(deleted once `agent-config-kit` is published and witan pins a released version)

### 6.4 Test porting

- **Generic behavior** (dry-run no-op, additive merge preserving unrelated
  keys, non-object JSON → skip-not-crash, hook dedup, discriminated-union
  round-trip, `ApprovalPolicy` projection per adapter) moves to
  `packages/agent-config-kit/tests/`, parametrized over synthetic
  `RegistrationBundle`s.
- **Witan-specific integration test** stays in
  `mcp/servers/witan/tests/test_setup.py`, shrunk to asserting that
  `_witan_bundle(...)` + `apply("claude", ...)` produces the *witan* MCP
  entry (`WITAN_AUTHOR` env, `uvx` command) and the *witan* hook commands —
  verifying wiring, not re-testing generic merge mechanics.

## 7. PyPI publishing plan (answers `tk-plan-pypi-publishing-setup-c4b1ad`)

Repo currently has **no** PyPI publish workflow — only the tag-triggered
GitHub-release ZIP packaging for skills (`AGENTS.md` §Key Commands / CI).
This is the first PyPI-published artifact from this repo. (Unchanged from
the prior draft — the richer data model doesn't affect the publishing
mechanics.)

| # | Question | Decision |
|---|---|---|
| P1 | Auth | **Trusted Publishing (OIDC)**, not an API token. Register a "pending publisher" on PyPI for `agent-config-kit` pointing at `mitodl/agent-kit`, workflow filename `publish-agent-config-kit.yml`, environment `pypi`. |
| P2 | Trigger | Tag push matching `agent-config-kit-v*` (prefixed — this repo may publish more than one package over time). |
| P3 | Versioning | Manual PEP 440 bump per release, no automated bump tooling. |
| P4 | CI workflow | New `.github/workflows/publish-agent-config-kit.yml`: `uv build` inside `packages/agent-config-kit/`, then `pypa/gh-action-pypi-publish` with `permissions: id-token: write`, `environment: pypi`. Check the tag version matches `pyproject.toml`'s `version` before publish. |
| P5 | Pre-publish CI | Reuse the existing `uv sync && uv run --group test pytest` pattern as a required check gating the publish job. |

## 8. Sequencing

1. Scaffold `packages/agent-config-kit/` (§6.1): canonical models (§4) +
   registry (§5) + jsonio/paths/installers moved verbatim, with their own
   tests — no witan involvement yet.
2. Build the 5 v1 adapters (§5) and the `apply`/`apply_all` orchestration
   layer (§4.1). Kilo Code's adapter is **blocked** on resolving
   `tk-verify-witan-install-kilo-writes-to-the-correct--5bf64e` first.
3. Decide the D6 codegen approach concretely: run `datamodel-code-generator`
   against Claude Code's and OpenCode's published schemas, hand-author
   minimal schemas for Pi/Copilot/Kilo Code, vendor the output.
4. Rewrite `witan/setup.py` to build a `RegistrationBundle` and call
   `apply`/`apply_all` (§6.3); add the `uv` workspace path dependency;
   port/shrink `test_setup.py` (§6.4).
5. Run `witan setup --agent all --dry-run` manually against a real home
   directory to confirm output is equivalent to pre-extraction behavior for
   the fields witan already writes (regression check — no intended
   behavior change to witan's own output, only to Kilo Code once the path
   bug is resolved, which is an intentional fix, not a regression).
6. Add the publish workflow (§7) and cut `agent-config-kit-v0.1.0`.
7. Update `AGENTS.md`'s repository layout with the new `packages/` row.

## 9. Open questions for implementation

- **Kilo Code's real MCP config path/key** — blocks §5's Kilo Code row and
  the `adapters/kilo.py` module. Resolve
  `tk-verify-witan-install-kilo-writes-to-the-correct--5bf64e` first; this
  library's Kilo Code registry entry should be built from that answer, not
  from witan's current (possibly-stale) behavior.
- **OpenCode's exact on-disk config filename/global dir**, and whether
  OpenCode, Goose, and Codex CLI genuinely support a per-project MCP/hooks
  config file (vs. only global config + directory-walked instructions) —
  not fully confirmed by the survey; only matters once one of those three is
  added to the registry (none are in v1).
- **Whether Zed is an actual future registration target or purely a design
  reference** (it has the richest LSP schema surveyed, useful as a
  superset reference for `LspServer`'s optional fields, but Zed is the
  editor itself, not something an external tool registers *into* in quite
  the same sense as the others) — a product decision for whenever LSP
  registration gets a real v1 caller, not blocking now.
- `CapabilityScope.global_`'s alias-serialization gotcha (§4) — needs
  `by_alias=True` at call sites or a `serialization_alias` fix; a 5-minute
  implementation detail, flagging so it isn't forgotten.
- Whether `agent-config-kit` should ship a `Literal`/enum for platform keys
  instead of a bare `str` — matches `setup_cmd.py`'s existing `AgentName`
  type today; a 5-minute implementation decision, not a design fork.
