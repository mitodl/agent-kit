# Agent Memory — Implementation Guide

A team-wide shared knowledge graph for coding agents, backed by
[Omnigraph](https://github.com/ModernRelay/omnigraph). Stores coding patterns,
project/repo facts, lessons, and agent context. Exposed over
[MCP](https://modelcontextprotocol.io/) so every agent platform (pi, Claude
Desktop, GitHub Copilot) can read and write without platform-specific code.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Agent (any platform)                    │
│   pi / Claude Desktop / GitHub Copilot / Claude Code           │
└───────────────────────────┬─────────────────────────────────────┘
                            │  MCP protocol
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│              witan  (FastMCP server)                 │
│                                                                 │
│  ┌──────────────┐  ┌────────────────┐  ┌────────────────────┐  │
│  │  repo detect │  │  config / env  │  │   5 MCP tools      │  │
│  │  (.git/conf) │  │  (URI, token,  │  │   memory_search    │  │
│  │              │  │   author)      │  │   memory_store     │  │
│  └──────────────┘  └────────────────┘  │   memory_get       │  │
│                                        │   memory_get_      │  │
│  ┌──────────────────────────────────┐  │     project_facts  │  │
│  │      OmnigraphClient             │  │   memory_list_     │  │
│  │  subprocess → omnigraph CLI      │  │     patterns       │  │
│  │  reads: omnigraph read ...       │  └────────────────────┘  │
│  │  writes: omnigraph change ...    │                          │
│  └──────────────────────────────────┘                          │
└───────────────────────────┬─────────────────────────────────────┘
                            │  omnigraph CLI (subprocess)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Omnigraph Graph URI                        │
│                                                                 │
│  local disk     ~/.local/share/witan/graph.omni     │
│  local S3       s3://omnigraph-local/agent-memory/             │
│  team remote    http://omnigraph.internal:8080  (S3-backed)    │
└─────────────────────────────────────────────────────────────────┘
```

**Design constraints:**

- No persistent server process required for local-disk mode. The MCP server
  shells out to the `omnigraph` CLI binary per operation.
- Switching from local → team remote is a single env var change.
- The MCP layer is platform-agnostic. The pi extension (v2) will add proactive
  injection on top without replacing any of this.

---

## Local Development Setup

Wire both MCP servers, the hooks, and the skills into your local Claude Code and
Pi agents, running the servers straight from your checkout so edits take effect
without publishing. Requires [`uv`](https://docs.astral.sh/uv/) and the
`omnigraph` binary — run `mcp/servers/witan/install.sh` once to install
the binary and initialise the local graph.

Set `REPO` to your checkout path for the snippets below:

```bash
REPO=/path/to/agent-kit
```

### 1. MCP servers — run from the local checkout

Add both servers under `mcpServers` in `~/.claude/settings.json` (Claude Code)
**and** `~/.pi/agent/mcp.json` (Pi). The local pattern uses `uv run --directory`
(not the published `uvx --from git+…` snippet in each server's `config/`), so the
server always runs your working tree:

```json
"witan": {
  "command": "uv",
  "args": ["run", "--directory", "REPO/mcp/servers/witan", "witan"],
  "env": { "WITAN_AUTHOR": "Your Name" }
},
"witan-code": {
  "command": "uv",
  "args": ["run", "--directory", "REPO/mcp/servers/witan-code", "witan-code"],
  "env": { "WITAN_AUTHOR": "Your Name" }
}
```

Replace `REPO` with the absolute path. **Restart the agent** after editing its
config so it picks up the new servers (the first `witan-code` launch
installs tree-sitter — one-time, slow).

### 2. Hooks — Claude Code

Symlink the three hooks and register them. The scripts resolve their real
location through the symlink (via `readlink -f`), so the symlink install just works:

```bash
mkdir -p ~/.claude/hooks
ln -sf "$REPO/configs/hooks/workflow-context-inject.sh"     ~/.claude/hooks/
ln -sf "$REPO/configs/hooks/workflow-session-checkpoint.sh" ~/.claude/hooks/
ln -sf "$REPO/configs/hooks/codegraph-session-init.sh"      ~/.claude/hooks/
ln -sf "$REPO/configs/hooks/codegraph-reindex.sh"           ~/.claude/hooks/
```

Register them in `~/.claude/settings.json` under `hooks` —
`UserPromptSubmit` → context-inject, `Stop` → session-checkpoint,
`SessionStart` → codegraph-session-init (seeds/refreshes the whole code graph in
the background), and `PostToolUse` (matcher `Edit|Write`) → codegraph-reindex
(keeps edited files fresh). See
[`configs/hooks/README.md`](../configs/hooks/README.md) for the exact JSON.

**Pi** has no hooks but provides the equivalent via extension events. Symlink the
mirror extensions into `~/.pi/agent/extensions/` (codegraph index/reindex and
workflow context injection) — see [`configs/pi/README.md`](../configs/pi/README.md):

```bash
ln -sf "$REPO/configs/pi/extensions/codegraph.ts"        ~/.pi/agent/extensions/
ln -sf "$REPO/configs/pi/extensions/workflow-context.ts" ~/.pi/agent/extensions/
```

### 3. Skills — both agents

`witan setup` installs the bundled Witan skills automatically. For local
development from a checkout, symlink the bundled skill directories into the
shared `~/.agents/skills/` catalog, then into each agent:

```bash
for skill in witan-memory witan-workflow witan-task witan-project-tracker; do
  ln -sfn "$REPO/mcp/servers/witan/witan/skills/$skill" ~/.agents/skills/"$skill"
  ln -sfn "../../.agents/skills/$skill"    ~/.claude/skills/"$skill"
  ln -sfn "../../../.agents/skills/$skill" ~/.pi/agent/skills/"$skill"
done
```

### 4. Code-graph indexer CLI — optional, faster hook path

Install the indexer on `PATH` so the `PostToolUse` hook uses its fast path and you
can seed a repo manually. `--editable` keeps it pointed at the working tree:

```bash
uv tool install --editable "$REPO/mcp/servers/witan-code"
```

With the `SessionStart` hook wired (step 2), the whole-repo seed and refresh happen
automatically in the background — you don't need to run `index` by hand. To seed a
repo immediately (or under Pi, which has no hooks), run it manually:

```bash
witan-code index .   # inside the repo
```

Without the CLI on `PATH`, the hooks fall back to `uvx --from <local pkg>` (correct,
just slower).

### 5. Graph schema upkeep

The shared graph lives at `~/.local/share/witan/graph.omni`. After
pulling schema changes, re-apply:

```bash
omnigraph schema apply \
  --schema "$REPO/mcp/servers/witan/schema/schema.pg" \
  ~/.local/share/witan/graph.omni
```

A field **rename** (the schema uses snake_case identifiers) can't be migrated in
place — re-initialise a fresh graph instead (move the old one aside first):

```bash
mv ~/.local/share/witan/graph.omni{,.bak}
omnigraph init \
  --schema "$REPO/mcp/servers/witan/schema/schema.pg" \
  ~/.local/share/witan/graph.omni
```

The per-repo code-graph stores under `~/.local/share/witan/code/` are
disposable — delete and re-index freely.

---

## Repository Structure

Everything lives under `mcp/servers/witan/` in `agent-kit`:

```
mcp/servers/witan/
├── README.md                  # User-facing setup guide
├── install.sh                 # Install omnigraph binary + init local graph
├── omnigraph.yaml             # CLI project config (output format, aliases)
├── pyproject.toml             # Python package metadata
│
├── schema/
│   └── schema.pg              # Omnigraph graph schema
│
├── queries/
│   ├── read.gq                # All read queries
│   └── mutations.gq           # All insert / update queries
│
├── witan/          # Python package
│   ├── __init__.py
│   ├── __main__.py            # Entry point: python -m witan
│   ├── server.py              # FastMCP app + tool definitions
│   ├── config.py              # Config loaded from env vars
│   ├── repo.py                # Git remote → canonical repo slug
│   └── graph.py               # OmnigraphClient (CLI subprocess wrapper)
│
└── config/
    ├── pi.json                # Snippet for ~/.pi/agent/mcp.json
    ├── claude.json            # Snippet for claude_desktop_config.json
    └── copilot.json           # Snippet for .vscode/mcp.json
```

The skill lives at:

```
mcp/servers/witan/witan/skills/witan-memory/
└── SKILL.md
```

---

## 1. Omnigraph Schema — `schema/schema.pg`

A single `Memory` node type with a `kind` discriminator covers all four memory
categories. This keeps cross-kind search simple (one query, one index). Type-
specific fields are optional and only populated for the relevant kind.

```pg
// Agent Memory — team-wide knowledge graph for coding agents.
//
// One node type with a kind discriminator keeps cross-kind search simple.
// Optional fields are populated only for the relevant kind:
//   pattern      → language
//   project_fact → category
//   lesson       → severity
//   agent_context → (no additional fields)
//
// Slug convention:
//   pat-   pattern          e.g. pat-always-use-uv
//   pf-    project_fact     e.g. pf-ol-django-vault-secrets
//   les-   lesson           e.g. les-no-raw-sql-in-views
//   ctx-   agent_context    e.g. ctx-ticket-1234-approach

node Memory {
    slug: String @key
    kind: enum(pattern, project_fact, lesson, agent_context) @index
    title: String @index
    content: String
    repo: String? @index
    language: String? @index
    category: String? @index
    severity: enum(info, warning, critical)? @index
    author: String @index
    createdAt: DateTime @index
    updatedAt: DateTime
    tags: [String]?
}

// Supersedes: a newer memory replaces an older one.
// Link new → old when updating a pattern or lesson that has changed.
edge Supersedes: Memory -> Memory

// AppliesTo: links a pattern or lesson to a project fact that provides context.
// e.g. a pattern "always use uv" AppliesTo project fact "ol-django uses uv".
edge AppliesTo: Memory -> Memory
```

**Notes:**

- FTS indexes are built automatically by `omnigraph schema apply` and
  `ensure_indices()` for any text column referenced by `search()`/`fuzzy()`/
  `bm25()` queries. No `@fulltext` annotation is needed.
- `@key` on `slug` implies a BTREE index on that column.
- `@index` on `kind`, `repo`, `language`, `category`, `severity`, `author`,
  `createdAt`, and `title` enables efficient scalar filtering.
- `tags` is a nullable list. Pass `null` or omit in inserts when not relevant.

---

## 2. Query Files

### `queries/read.gq`

```gq
// Agent Memory — read queries
//
// Search dispatch strategy:
//   The MCP server selects the query name based on which optional filters
//   the caller provided. This avoids relying on undefined behavior for
//   null param filtering in match bindings.

// ── Search (BM25 on content) ─────────────────────────────────────

query search_all($query: String) {
    match {
        $m: Memory
        search($m.content, $query)
    }
    return {
        $m.slug, $m.kind, $m.title, $m.content,
        $m.repo, $m.language, $m.category, $m.severity,
        $m.author, $m.tags, $m.createdAt,
        bm25($m.content, $query) as score
    }
    order { bm25($m.content, $query) desc }
    limit 20
}

query search_by_repo($query: String, $repo: String) {
    match {
        $m: Memory { repo: $repo }
        search($m.content, $query)
    }
    return {
        $m.slug, $m.kind, $m.title, $m.content,
        $m.repo, $m.language, $m.category, $m.severity,
        $m.author, $m.tags, $m.createdAt,
        bm25($m.content, $query) as score
    }
    order { bm25($m.content, $query) desc }
    limit 20
}

query search_by_kind($query: String, $kind: String) {
    match {
        $m: Memory { kind: $kind }
        search($m.content, $query)
    }
    return {
        $m.slug, $m.kind, $m.title, $m.content,
        $m.repo, $m.language, $m.category, $m.severity,
        $m.author, $m.tags, $m.createdAt,
        bm25($m.content, $query) as score
    }
    order { bm25($m.content, $query) desc }
    limit 20
}

query search_by_repo_and_kind($query: String, $repo: String, $kind: String) {
    match {
        $m: Memory { repo: $repo, kind: $kind }
        search($m.content, $query)
    }
    return {
        $m.slug, $m.kind, $m.title, $m.content,
        $m.repo, $m.language, $m.category, $m.severity,
        $m.author, $m.tags, $m.createdAt,
        bm25($m.content, $query) as score
    }
    order { bm25($m.content, $query) desc }
    limit 20
}

// ── Single node fetch ─────────────────────────────────────────────

query get_memory($slug: String) {
    match {
        $m: Memory { slug: $slug }
    }
    return {
        $m.slug, $m.kind, $m.title, $m.content,
        $m.repo, $m.language, $m.category, $m.severity,
        $m.author, $m.tags, $m.createdAt, $m.updatedAt
    }
}

// ── Project facts ─────────────────────────────────────────────────

query get_project_facts($repo: String) {
    match {
        $m: Memory { kind: "project_fact", repo: $repo }
    }
    return {
        $m.slug, $m.title, $m.content,
        $m.category, $m.author, $m.tags, $m.createdAt
    }
    order { $m.category, $m.createdAt desc }
}

// ── Patterns ──────────────────────────────────────────────────────

query patterns_all() {
    match {
        $m: Memory { kind: "pattern" }
    }
    return {
        $m.slug, $m.title, $m.content,
        $m.repo, $m.language, $m.author, $m.tags, $m.createdAt
    }
    order { $m.createdAt desc }
    limit 50
}

query patterns_by_repo($repo: String) {
    match {
        $m: Memory { kind: "pattern", repo: $repo }
    }
    return {
        $m.slug, $m.title, $m.content,
        $m.repo, $m.language, $m.author, $m.tags, $m.createdAt
    }
    order { $m.createdAt desc }
}
```

### `queries/mutations.gq`

Note: per the Omnigraph constraint (D₂), inserts/updates and deletes cannot be
mixed in a single query. `insert_memory` and `update_memory` are insert/update
queries; a future `delete_memory` must be a separate query file.

```gq
// Agent Memory — mutation queries

// ── Insert ────────────────────────────────────────────────────────

query insert_memory(
    $slug: String,
    $kind: String,
    $title: String,
    $content: String,
    $repo: String?,
    $language: String?,
    $category: String?,
    $severity: String?,
    $author: String,
    $tags: [String]?,
    $createdAt: DateTime,
    $updatedAt: DateTime
) {
    insert Memory {
        slug: $slug,
        kind: $kind,
        title: $title,
        content: $content,
        repo: $repo,
        language: $language,
        category: $category,
        severity: $severity,
        author: $author,
        tags: $tags,
        createdAt: $createdAt,
        updatedAt: $updatedAt
    }
}

// ── Update ────────────────────────────────────────────────────────
// Updates ALL mutable fields. The MCP server reads the current node first,
// merges caller-supplied changes, then calls this query with the full set.
// This avoids needing per-field update queries.

query update_memory(
    $slug: String,
    $title: String,
    $content: String,
    $repo: String?,
    $language: String?,
    $category: String?,
    $severity: String?,
    $tags: [String]?,
    $updatedAt: DateTime
) {
    update Memory
        set {
            title: $title,
            content: $content,
            repo: $repo,
            language: $language,
            category: $category,
            severity: $severity,
            tags: $tags,
            updatedAt: $updatedAt
        }
        where slug = $slug
}

// ── Edges ─────────────────────────────────────────────────────────

query link_supersedes($from: String, $to: String) {
    insert Supersedes { from: $from, to: $to }
}

query link_applies_to($from: String, $to: String) {
    insert AppliesTo { from: $from, to: $to }
}
```

---

## 3. MCP Server

### `pyproject.toml`

```toml
[project]
name = "witan"
version = "0.1.0"
description = "Agent memory MCP server backed by Omnigraph"
requires-python = ">=3.11"
dependencies = [
    "fastmcp>=0.1.0",
]

[project.scripts]
witan = "witan.__main__:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

### `witan/config.py`

Reads all configuration from environment variables. No config file is required;
the env vars are documented in `config/pi.json` and the README.

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# Path to the bundled query files, resolved relative to this file.
_QUERIES_DIR = Path(__file__).parent.parent / "queries"
_DEFAULT_GRAPH_URI = Path.home() / ".local" / "share" / "witan" / "graph.omni"


@dataclass(frozen=True)
class Config:
    graph_uri: str
    """Local path, s3://, or http:// URI pointing at the graph."""

    graph_token: str | None
    """Bearer token. Required when graph_uri is http://. Unused for local/S3."""

    author: str
    """Attribution string written to Memory.author on every insert."""

    queries_dir: Path
    """Directory containing read.gq and mutations.gq."""


def load() -> Config:
    """Load config from environment. All variables are optional with sensible defaults."""
    return Config(
        graph_uri=os.environ.get("WITAN_MEMORY_URI", str(_DEFAULT_GRAPH_URI)),
        graph_token=os.environ.get("WITAN_MEMORY_TOKEN"),
        author=os.environ.get(
            "WITAN_AUTHOR",
            os.environ.get("USER", "unknown"),
        ),
        queries_dir=_QUERIES_DIR,
    )
```

**Environment variable reference:**

| Variable | Required | Default | Description |
|---|---|---|---|
| `WITAN_MEMORY_URI` | No | `~/.local/share/witan/graph.omni` | Graph URI — local path, `s3://`, or `http://` |
| `WITAN_MEMORY_TOKEN` | Only for `http://` | — | Bearer token for remote server auth |
| `WITAN_AUTHOR` | No | `$USER` | Attribution on every insert |
| `WITAN_REPO` | No | — | Repo slug override (bypasses git detection) |

### `witan/repo.py`

Detects the current repository from the `.git/config` of the working directory.
Normalises SSH and HTTPS remote URLs to a canonical HTTPS project URI
(`https://github.com/mitodl/ol-django`). This URI is the shared join key across
every layer — memory, workflow, tasks, and the code graph — so they all derive it
identically. (The embedded snapshot below predates that change; see
`witan/repo.py` for the current `_normalise`.)

```python
from __future__ import annotations

import configparser
import os
import re
from pathlib import Path


def detect(override: str | None = None) -> str | None:
    """
    Return a canonical repo slug for the current working directory.

    Resolution order:
      1. ``override`` parameter (explicit caller value)
      2. ``WITAN_REPO`` environment variable
      3. ``origin`` remote URL from the nearest ``.git/config``
      4. ``None`` — no repo context available
    """
    if override:
        return override

    if env_repo := os.environ.get("WITAN_REPO"):
        return env_repo

    git_config_path = _find_git_config(Path.cwd())
    if git_config_path is None:
        return None

    return _parse_origin(git_config_path)


def _find_git_config(start: Path) -> Path | None:
    """Walk up from ``start`` until a .git/config is found."""
    for directory in [start, *start.parents]:
        candidate = directory / ".git" / "config"
        if candidate.exists():
            return candidate
    return None


def _parse_origin(git_config: Path) -> str | None:
    """
    Parse .git/config and return the normalised ``origin`` remote URL.

    Uses configparser; falls back to None if the file is malformed or
    ``remote "origin"`` is absent.
    """
    parser = configparser.RawConfigParser()
    try:
        parser.read(git_config)
    except configparser.Error:
        return None

    section = 'remote "origin"'
    if not parser.has_option(section, "url"):
        return None

    return _normalise(parser.get(section, "url"))


def _normalise(url: str) -> str:
    """
    Normalise a git remote URL to a canonical slug.

    Examples
    --------
    git@github.com:mitodl/ol-django.git  →  github.com/mitodl/ol-django
    https://github.com/mitodl/ol-django  →  github.com/mitodl/ol-django
    https://github.com/mitodl/repo.git   →  github.com/mitodl/repo
    """
    # Strip trailing .git
    url = re.sub(r"\.git$", "", url)

    # SSH: git@host:org/repo
    if m := re.match(r"git@([^:]+):(.+)", url):
        return f"{m.group(1)}/{m.group(2)}"

    # HTTPS / HTTP: https://host/org/repo
    if m := re.match(r"https?://([^/]+)/(.+)", url):
        return f"{m.group(1)}/{m.group(2)}"

    # Unknown format — return as-is
    return url
```

### `witan/graph.py`

Thin wrapper around the `omnigraph` CLI. Runs each operation as a subprocess
and parses JSON output. Raises `RuntimeError` with the CLI's stderr on failure.

```python
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


class OmnigraphClient:
    """Subprocess wrapper for the omnigraph CLI."""

    def __init__(
        self,
        graph_uri: str,
        queries_dir: Path,
        token: str | None = None,
    ) -> None:
        self.graph_uri = graph_uri
        self.queries_dir = queries_dir
        self.token = token
        self._binary = self._find_binary()

    # ── Public API ────────────────────────────────────────────────

    def read(
        self,
        query_file: str,
        query_name: str,
        params: dict,
    ) -> list[dict]:
        """Run a named read query. Returns a list of result rows."""
        result = self._run(
            "read",
            "--query", str(self.queries_dir / query_file),
            "--name", query_name,
            "--params", json.dumps(params),
        )
        if not result.strip():
            return []
        try:
            return json.loads(result)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"omnigraph returned non-JSON: {result!r}") from exc

    def change(
        self,
        query_file: str,
        query_name: str,
        params: dict,
    ) -> None:
        """Run a named mutation query."""
        self._run(
            "change",
            "--query", str(self.queries_dir / query_file),
            "--name", query_name,
            "--params", json.dumps(params),
        )

    # ── Internals ─────────────────────────────────────────────────

    def _run(self, subcommand: str, *args: str) -> str:
        cmd = [self._binary, subcommand, *args, self.graph_uri]
        env = dict(os.environ)
        if self.token:
            env["OMNIGRAPH_SERVER_BEARER_TOKEN"] = self.token

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"omnigraph {subcommand} failed (exit {result.returncode}):\n"
                f"{result.stderr.strip()}"
            )
        return result.stdout

    @staticmethod
    def _find_binary() -> str:
        binary = shutil.which("omnigraph")
        if binary is None:
            raise RuntimeError(
                "omnigraph binary not found on PATH. "
                "Run mcp/servers/witan/install.sh first."
            )
        return binary
```

### `witan/server.py`

The FastMCP application. Each tool auto-detects the repo from the caller's CWD
unless an explicit `repo` override is passed. All tools surface meaningful error
messages when the graph is unavailable.

```python
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastmcp import FastMCP

from . import config as cfg_module
from . import repo as repo_module
from .graph import OmnigraphClient

# ── Startup ───────────────────────────────────────────────────────

cfg = cfg_module.load()
client = OmnigraphClient(cfg.graph_uri, cfg.queries_dir, cfg.graph_token)

mcp = FastMCP(
    "witan",
    description=(
        "Team-wide agent memory backed by Omnigraph. "
        "Stores and retrieves coding patterns, project facts, lessons, "
        "and agent context scoped to repositories."
    ),
)

# ── Helpers ───────────────────────────────────────────────────────

MemoryKind = Literal["pattern", "project_fact", "lesson", "agent_context"]

_KIND_PREFIX = {
    "pattern": "pat",
    "project_fact": "pf",
    "lesson": "les",
    "agent_context": "ctx",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_slug(kind: str, title: str) -> str:
    """Generate a stable, human-readable slug from kind and title."""
    prefix = _KIND_PREFIX.get(kind, "mem")
    sanitised = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48]
    short_id = uuid.uuid4().hex[:6]
    return f"{prefix}-{sanitised}-{short_id}"


# ── Tools ─────────────────────────────────────────────────────────


@mcp.tool()
def memory_search(
    query: str,
    repo: str | None = None,
    kind: MemoryKind | None = None,
) -> list[dict]:
    """
    Search agent memories by text.

    Returns the top-20 matching memories ranked by BM25 relevance. The search
    is automatically scoped to the current git repository unless ``repo`` or
    ``WITAN_REPO`` overrides it.

    Parameters
    ----------
    query:
        Free-text search query. Searched against ``content``.
    repo:
        Canonical repo slug (e.g. ``github.com/mitodl/ol-django``).
        Auto-detected from ``.git/config`` if omitted.
    kind:
        Optional filter: ``pattern``, ``project_fact``, ``lesson``,
        or ``agent_context``.
    """
    detected = repo_module.detect(override=repo)

    if detected and kind:
        return client.read(
            "read.gq",
            "search_by_repo_and_kind",
            {"query": query, "repo": detected, "kind": kind},
        )
    if detected:
        return client.read(
            "read.gq",
            "search_by_repo",
            {"query": query, "repo": detected},
        )
    if kind:
        return client.read(
            "read.gq",
            "search_by_kind",
            {"query": query, "kind": kind},
        )
    return client.read("read.gq", "search_all", {"query": query})


@mcp.tool()
def memory_store(
    kind: MemoryKind,
    title: str,
    content: str,
    repo: str | None = None,
    language: str | None = None,
    category: str | None = None,
    severity: Literal["info", "warning", "critical"] | None = None,
    tags: list[str] | None = None,
) -> dict:
    """
    Store a new memory in the graph.

    Returns the slug of the created node so callers can link to it.

    Parameters
    ----------
    kind:
        ``pattern``      — coding convention or reusable technique
        ``project_fact`` — structural fact about a repo/service
        ``lesson``       — a correction or cautionary finding
        ``agent_context``— information a future agent on this task should know
    title:
        Short, human-readable label. Used in listings and search.
    content:
        Full text of the memory. Be specific: include the what, why, and any
        examples. This is the primary search target.
    repo:
        Canonical repo slug. Auto-detected from ``.git/config`` if omitted.
    language:
        Programming language (for ``pattern`` kind). e.g. ``python``, ``typescript``.
    category:
        Thematic category (for ``project_fact`` kind).
        e.g. ``architecture``, ``deployment``, ``testing``, ``dependencies``.
    severity:
        Importance level (for ``lesson`` kind).
        ``info`` | ``warning`` | ``critical``.
    tags:
        Optional list of free-form tags for grouping.
    """
    now = _now_iso()
    slug = _make_slug(kind, title)
    detected_repo = repo_module.detect(override=repo)

    client.change(
        "mutations.gq",
        "insert_memory",
        {
            "slug": slug,
            "kind": kind,
            "title": title,
            "content": content,
            "repo": detected_repo,
            "language": language,
            "category": category,
            "severity": severity,
            "author": cfg.author,
            "tags": tags,
            "createdAt": now,
            "updatedAt": now,
        },
    )
    return {"slug": slug, "kind": kind, "repo": detected_repo}


@mcp.tool()
def memory_get(slug: str) -> dict | None:
    """
    Retrieve a single memory by its slug.

    Returns the full node or ``null`` if not found.
    """
    rows = client.read("read.gq", "get_memory", {"slug": slug})
    return rows[0] if rows else None


@mcp.tool()
def memory_get_project_facts(repo: str | None = None) -> list[dict]:
    """
    Return all project facts for a repository.

    Use this at the start of a session in an unfamiliar codebase to load
    structural context: architecture, deployment topology, testing conventions,
    known dependencies and quirks.

    Parameters
    ----------
    repo:
        Canonical repo slug. Auto-detected from ``.git/config`` if omitted.
    """
    detected = repo_module.detect(override=repo)
    if not detected:
        return []
    return client.read("read.gq", "get_project_facts", {"repo": detected})


@mcp.tool()
def memory_list_patterns(
    repo: str | None = None,
    language: str | None = None,
) -> list[dict]:
    """
    List coding patterns, optionally scoped to a repo and/or language.

    Use before writing code in a familiar service to check what conventions
    the team has documented. When both ``repo`` and ``language`` are provided,
    the server fetches by ``repo`` and post-filters by ``language`` in Python
    (avoiding combinatorial query variants).

    Parameters
    ----------
    repo:
        Canonical repo slug. Auto-detected from ``.git/config`` if omitted.
    language:
        Optional language filter applied after fetching. e.g. ``python``.
    """
    detected = repo_module.detect(override=repo)

    if detected:
        rows = client.read("read.gq", "patterns_by_repo", {"repo": detected})
    else:
        rows = client.read("read.gq", "patterns_all", {})

    if language:
        rows = [r for r in rows if (r.get("language") or "").lower() == language.lower()]

    return rows
```

### `witan/__main__.py`

```python
from .server import mcp


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
```

---

## 4. Omnigraph Project Config — `omnigraph.yaml`

This file configures the `omnigraph` CLI for use within this project. It sets
JSON output format (required for MCP server subprocess parsing) and declares
the query roots. Operators override the `graph` URI via env var or by editing
the file for their deployment.

```yaml
project:
  name: Agent Memory

cli:
  # Override graph target via WITAN_MEMORY_URI or edit here.
  # local disk:   ~/.local/share/witan/graph.omni
  # local S3:     s3://omnigraph-local/agent-memory/
  # team remote:  http://omnigraph.internal:8080
  graph: ~/.local/share/witan/graph.omni
  branch: main
  output_format: json

query:
  roots:
    - queries
    - .

aliases:
  search:      { command: read,   query: queries/read.gq,      name: search_by_repo,    args: [query, repo] }
  facts:       { command: read,   query: queries/read.gq,      name: get_project_facts, args: [repo] }
  patterns:    { command: read,   query: queries/read.gq,      name: patterns_by_repo,  args: [repo] }
  get:         { command: read,   query: queries/read.gq,      name: get_memory,        args: [slug] }
  store:       { command: change, query: queries/mutations.gq, name: insert_memory,     args: [slug, kind, title, content, author, createdAt, updatedAt] }
```

---

## 5. Installation Script — `install.sh`

The install script is idempotent. Running it twice is safe.

```bash
#!/usr/bin/env bash
# install.sh — Install omnigraph binary and initialise the local agent-memory graph.
#
# Usage:
#   ./install.sh                  # local-disk mode (default)
#   RUSTFS=1 ./install.sh         # local RustFS/S3 mode (requires Docker)
#
# After running, set WITAN_MEMORY_URI if you want a non-default graph path:
#   export WITAN_MEMORY_URI=s3://omnigraph-local/agent-memory/
set -euo pipefail

GRAPH_DIR="${WITAN_DATA_DIR:-${HOME}/.local/share/witan}"
GRAPH_PATH="${GRAPH_DIR}/graph.omni"
SCHEMA_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/schema/schema.pg"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Checking for omnigraph binary..."
if ! command -v omnigraph &>/dev/null; then
    echo "    Not found — installing from GitHub releases..."
    curl -fsSL https://raw.githubusercontent.com/ModernRelay/omnigraph/main/scripts/install.sh | bash
    # The installer puts binaries in ~/.local/bin; ensure it's on PATH.
    export PATH="${HOME}/.local/bin:${PATH}"
fi
echo "    omnigraph: $(omnigraph --version)"

# ── Local RustFS mode ─────────────────────────────────────────────
if [[ "${RUSTFS:-}" == "1" ]]; then
    echo ""
    echo "==> Starting local RustFS (S3-compatible) storage..."
    echo "    Requires Docker. This may take a minute on first run."
    BUCKET=omnigraph-local \
    PREFIX=agent-memory \
    BIND=127.0.0.1:8081 \
        curl -fsSL https://raw.githubusercontent.com/ModernRelay/omnigraph/main/scripts/local-rustfs-bootstrap.sh | bash
    echo ""
    echo "    RustFS running. Set:"
    echo "    export WITAN_MEMORY_URI=s3://omnigraph-local/agent-memory/"
    echo "    export AWS_ACCESS_KEY_ID=rustfsadmin"
    echo "    export AWS_SECRET_ACCESS_KEY=rustfsadmin"
    echo "    export AWS_REGION=us-east-1"
    echo "    export AWS_ENDPOINT_URL=http://127.0.0.1:9000"
    echo "    export AWS_ENDPOINT_URL_S3=http://127.0.0.1:9000"
    echo "    export AWS_ALLOW_HTTP=true"
    echo "    export AWS_S3_FORCE_PATH_STYLE=true"
    exit 0
fi

# ── Local-disk mode ───────────────────────────────────────────────
echo ""
echo "==> Initialising local graph at ${GRAPH_PATH}..."

if [[ -d "${GRAPH_PATH}" ]]; then
    echo "    Graph already exists — checking schema..."
    omnigraph schema plan --schema "${SCHEMA_FILE}" "${GRAPH_PATH}"
    echo "    Run 'omnigraph schema apply --schema ${SCHEMA_FILE} ${GRAPH_PATH}' to apply any changes."
else
    mkdir -p "${GRAPH_DIR}"
    omnigraph init --schema "${SCHEMA_FILE}" "${GRAPH_PATH}"
    echo "    Graph initialised."
fi

echo ""
echo "==> Building indexes..."
# ensure_indices is safe to re-run; it builds FTS and BTREE indexes.
omnigraph schema apply --schema "${SCHEMA_FILE}" "${GRAPH_PATH}" 2>/dev/null || true

echo ""
echo "Done."
echo ""
echo "Next steps:"
echo "  1. Add the MCP server to your agent config:"
echo "     pi:      copy config/pi.json into ~/.pi/agent/mcp.json"
echo "     Claude:  copy config/claude.json into claude_desktop_config.json"
echo "     Copilot: copy config/copilot.json into .vscode/mcp.json"
echo ""
echo "  2. Optional — override defaults in your shell profile:"
echo "     export WITAN_MEMORY_URI=${GRAPH_PATH}"
echo "     export WITAN_AUTHOR=\$(git config user.name)"
```

---

## 6. Operating Modes

### Local Disk (default)

No extra infrastructure. The `omnigraph` CLI reads and writes a directory at
`~/.local/share/witan/graph.omni`.

```bash
# No env vars required. This is the default.
export WITAN_AUTHOR="Alice Smith"
```

**Limitation:** not shared across machines. Use for personal-only mode or
before a team server is deployed.

### Local RustFS (S3-compatible)

Runs a Docker-backed S3-compatible store locally. Enables the full S3 code path
without team infrastructure — useful for testing the team mode locally.

```bash
RUSTFS=1 ./install.sh

export WITAN_MEMORY_URI=s3://omnigraph-local/agent-memory/
export AWS_ACCESS_KEY_ID=rustfsadmin
export AWS_SECRET_ACCESS_KEY=rustfsadmin
export AWS_REGION=us-east-1
export AWS_ENDPOINT_URL=http://127.0.0.1:9000
export AWS_ENDPOINT_URL_S3=http://127.0.0.1:9000
export AWS_ALLOW_HTTP=true
export AWS_S3_FORCE_PATH_STYLE=true
```

### Remote Team Server

The shared mode. An `omnigraph-server` process runs in your infrastructure,
pointed at an S3-backed graph. Each team member sets two env vars:

```bash
export WITAN_MEMORY_URI=http://witan.internal:8080
export WITAN_MEMORY_TOKEN=<bearer-token>
export WITAN_AUTHOR="Alice Smith"
```

**Deploying the server:**

```bash
# On the server host or in a container:
OMNIGRAPH_SERVER_BEARER_TOKEN="<token>" \
AWS_REGION="us-east-1" \
AWS_ACCESS_KEY_ID="<key>" \
AWS_SECRET_ACCESS_KEY="<secret>" \
omnigraph-server s3://mitodl-agent-memory/graph.omni \
  --bind 0.0.0.0:8080
```

The graph must already exist. Bootstrap it once:

```bash
# From any machine with AWS credentials and the omnigraph binary:
omnigraph init \
  --schema mcp/servers/witan/schema/schema.pg \
  s3://mitodl-agent-memory/graph.omni
```

**Promoting a local graph to S3:**

```bash
# Export your local memories to JSONL
omnigraph export \
  ~/.local/share/witan/graph.omni \
  > memories.jsonl

# Load them into the S3-backed graph
omnigraph load \
  --data memories.jsonl \
  --mode merge \
  s3://mitodl-agent-memory/graph.omni
```

---

## 7. MCP Config Snippets

### `config/pi.json` — for `~/.pi/agent/mcp.json`

```json
{
  "mcpServers": {
    "witan": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan",
        "witan"
      ],
      "env": {
        // Required for remote team server mode:
        // "WITAN_MEMORY_URI": "http://witan.internal:8080",
        // "WITAN_MEMORY_TOKEN": "<your-bearer-token>",

        // Optional — defaults to local disk if unset:
        // "WITAN_MEMORY_URI": "/home/you/.local/share/witan/graph.omni",

        "WITAN_AUTHOR": "<your-name>"
      }
    }
  }
}
```

### `config/claude.json` — for `claude_desktop_config.json`

```json
{
  "mcpServers": {
    "witan": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan",
        "witan"
      ],
      "env": {
        "WITAN_AUTHOR": "<your-name>"
      }
    }
  }
}
```

### `config/copilot.json` — for `.vscode/mcp.json`

```json
{
  "servers": {
    "witan": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/mitodl/agent-kit#subdirectory=mcp/servers/witan",
        "witan"
      ],
      "env": {
        "WITAN_AUTHOR": "${env:USER}"
      }
    }
  }
}
```

> **Note on `uvx`:** `uvx` installs the package in an isolated environment on
> first run and caches it. No manual install step is required beyond having
> `uv` on `PATH`. The `omnigraph` binary itself must still be installed via
> `install.sh` (it is a Rust binary, not a Python package).

---

## 8. Agent Skill — `mcp/servers/witan/witan/skills/witan-memory/SKILL.md`

```markdown
---
name: witan-memory
description: >
  Read from and write to the team's shared agent memory graph. Use when
  starting work in a repository (load project facts and patterns), after
  solving a non-obvious problem (store a pattern), when discovering
  structural information about a codebase (store a project fact), or when
  a correction was needed (store a lesson). Requires the witan
  MCP server to be configured.
---

# Agent Memory

The team's shared knowledge graph stores four kinds of memories, all
backed by Omnigraph and accessible via the `witan` MCP server.
The repo is auto-detected from `.git/config` — you rarely need to pass it
explicitly.

## When to Use Each Tool

### `memory_get_project_facts` — load context at session start

Call this **first** whenever you start working in a repository you haven't
used in this session:

```
memory_get_project_facts()
```

Returns all structural facts for the current repo: architecture, deployment
topology, testing conventions, known dependencies, environment quirks. Read
these before writing code, choosing a library, or making deployment decisions.

### `memory_list_patterns` — check conventions before writing code

Before implementing something non-trivial, check what patterns the team has
already documented:

```
memory_list_patterns()                          # all patterns in this repo
memory_list_patterns(language="python")         # filtered by language
```

### `memory_search` — find relevant context by topic

When you need to know if the team has encountered something similar before:

```
memory_search("vault secrets injection")
memory_search("database migration rollback strategy")
memory_search("rate limiting approach", kind="pattern")
```

### `memory_store` — record something worth remembering

**Store a `pattern`** after solving a problem in a non-obvious way, or when
you apply a team convention that should be made explicit:

```
memory_store(
    kind="pattern",
    title="Always use uv, never pip",
    content="All Python work in this repo uses uv for environment management. ...",
    language="python",
    tags=["tooling", "environment"]
)
```

**Store a `project_fact`** when you learn something structural about a
codebase that a future agent would need to know:

```
memory_store(
    kind="project_fact",
    title="Vault secrets injected via env at runtime",
    content="This service reads secrets from Vault at startup via the ...",
    category="deployment"
)
```

**Store a `lesson`** when a mistake was made or a correction was needed:

```
memory_store(
    kind="lesson",
    title="Do not run migrations without a backup in staging",
    content="On 2025-05-10, a migration was run without a prior snapshot ...",
    severity="warning"
)
```

**Store `agent_context`** when handing off a task or leaving breadcrumbs
for a future agent session:

```
memory_store(
    kind="agent_context",
    title="Ticket 1234 — approach taken",
    content="Chose to use the existing TaskQueue infrastructure rather than ...",
    tags=["ticket-1234"]
)
```

## Quality Guidelines

- **Be specific.** Vague memories degrade search quality. Include the what,
  why, and any relevant examples in `content`.
- **One idea per memory.** Split broad topics. "uv for packaging" and "pytest
  config conventions" are two memories, not one.
- **Don't store transient state.** Session-specific observations that won't
  be useful after the current task ends don't belong in the graph.
- **Check before storing.** Run `memory_search` first. If a similar memory
  already exists, consider whether to update it instead of creating a
  duplicate.

## Updating an Existing Memory

Use `memory_get` to fetch the current content, decide what to change, then
`memory_store` a corrected version. If the old memory is superseded rather
than simply wrong, note the old slug and store both — the `Supersedes`
relationship can be linked manually via the CLI if needed (v2 will expose
this as a tool).
```

---

## 9. v2 Roadmap

The following are **explicitly deferred** from this implementation. They do
not affect the v1 interface.

| Item | Notes |
|---|---|
| **pi extension (proactive injection)** | A thin pi extension calls `memory_get_project_facts` + `memory_search` in `before_agent_start` and injects the results into the system prompt. The MCP server is the dependency; the extension adds on top. |
| **Branching / review workflow** | Personal branches (`user/<name>`) let agents propose memories without writing to `main` directly. A merge step (manual or CI) promotes to the shared graph. Omnigraph's `branch create` / `branch merge` CLI makes this straightforward to add. |
| **Personal preference namespace** | Personal branches are permanent for preferences that should not be team-promoted. The `memory_search` tool gains a `branch` parameter to scope reads. |
| **Vector / hybrid search** | Add `Vector(1536)` field to `Memory` with `@embed("content")`, configure an embedding provider, run `omnigraph embed`, and switch search queries to use `rrf(bm25(...), nearest(...))` for hybrid ranking. BM25-only v1 is a clean upgrade path. |
| **`memory_update` tool** | Expose `update_memory` query as a first-class tool. v1 workaround: `memory_get` + `memory_store`. |
| **`link_supersedes` / `link_applies_to` tools** | Expose edge mutations so agents can express relationships between memories without the CLI. |
| **`memory_delete` tool** | Requires a separate `delete.gq` file (D₂ constraint: cannot mix deletes with inserts/updates). Deliberately omitted to prevent accidental data loss in v1. |
```

---

## Workflow Tracking

The witan server also tracks end-to-end engineering projects across
multiple Claude Code sessions. This lets you trace a project from discovery
through delivery without explicit handoffs between sessions, support parallel
sessions, and build a corpus of completed workflow traces for pattern mining.

### Node Types

Three new node types extend the base schema:

| Node | Slug Prefix | Purpose |
|---|---|---|
| `WorkflowProject` | `wp-` | Overarching engineering objective with lifecycle phases |
| `WorkflowSession` | `ws-` | A single Claude Code session linked to a project |
| `WorkflowTrace` | `wt-` | Assembled at completion; immutable corpus record |

Edges: `BelongsTo` (session → project), `Produced` (project → trace),
`Informed` (project → Memory).

### Auto-Linking Sessions

The `UserPromptSubmit` hook at `configs/hooks/workflow-context-inject.sh`
runs before every prompt in a repo that has active projects. It injects
context like:

```
## Active Workflow Projects

This repository has 1 active tracked project:

- **Add Vault K8s auth to ol-django** (slug: `wp-add-vault-k8s-auth-a3f912`)
  Phase: implementation
  Issue: github.com/mitodl/ol-django/issues/847
```

The agent reads this and calls `workflow_session_start` with the slug — no
user intervention required. Parallel sessions each call `workflow_session_start`
independently.

### Workflow Tools

See [MCP Tools](#mcp-tools) in the server README for signatures. Full usage
documentation is in
[`mcp/servers/witan/witan/skills/witan-project-tracker/SKILL.md`](../mcp/servers/witan/witan/skills/witan-project-tracker/SKILL.md).

### Session State File

`workflow_session_start` writes a JSON state file to
`/tmp/workflow-session-<session_id>.json`. The `Stop` hook at
`configs/hooks/workflow-session-checkpoint.sh` reads this file to auto-close
sessions that did not call `workflow_session_end` explicitly. The file is
deleted after the hook runs (or after `workflow_session_end` is called).

### Corpus and Pattern Mining

When `workflow_project_complete` is called, a `WorkflowTrace` is assembled
from all linked sessions. The trace records:

- `phases` — ordered list of phases that occurred
- `sessionCount` — number of sessions
- `duration` — hours from first session start to last session end
- `outcome` — free-text narrative of what was delivered
- `lessonsSlug` / `patternsSlug` — Memory nodes linked via `Informed`

Query completed traces:

```python
workflow_project_list(status="completed")  # list finished projects
# construct trace slug as wt-{project_slug} and pass to workflow_project_get
```

Future tooling (planned) will sweep `WorkflowTrace` nodes to identify
repeated phase sequences, tool usage patterns, and common decision types,
then emit those as new skill templates.

### Hook Setup

```bash
mkdir -p ~/.claude/hooks
cd /path/to/agent-kit/configs/hooks
ln -sf "$(pwd)/workflow-context-inject.sh" ~/.claude/hooks/
ln -sf "$(pwd)/workflow-session-checkpoint.sh" ~/.claude/hooks/
```

Add to `~/.claude/settings.json`:

```json
"hooks": {
  "UserPromptSubmit": [
    {
      "matcher": "",
      "hooks": [{"type": "command", "command": "bash ~/.claude/hooks/workflow-context-inject.sh"}]
    }
  ],
  "Stop": [
    {
      "matcher": "",
      "hooks": [{"type": "command", "command": "bash ~/.claude/hooks/workflow-session-checkpoint.sh"}]
    }
  ]
}
```

See `configs/hooks/README.md` for full details.

---

## Task Tracking (Layer 1)

A dependency-aware, hierarchical task tracker (beads-like) lives in the **same
graph** as memory and workflow. Tasks belong here rather than in a separate
system because they share the work-coordination lifecycle — low-churn,
agent/human-authored, team-shared — and integrate via hard edges.

### Node + edges

`Task` (`tk-` slug) with `type` (bug/feature/task/chore/epic), `status`
(open/in_progress/blocked/closed), `priority` (p0–p3), and:

- **Hierarchy** — `parentSlug` (denormalized) + `ParentOf` edge let an `epic`
  decompose into sub-issues.
- **Dependencies** — `blockedBy` (denormalized list) + `Blocks` edge drive the
  ready-work query without graph traversal.
- **External links** — `externalUri` points a task at a GitHub issue/PR or any URI.
- **Cross-layer** — `projectSlug`/`TaskBelongsTo` → WorkflowProject;
  `Addresses` → Memory; `Closes` (WorkflowSession → Task); `symbolRefs` → code graph.

### Tools

`task_create`, `task_get`, `task_list`, `task_update`, `task_close`,
`task_link`, and **`task_ready`** — open tasks whose blockers are all closed,
ordered by priority. `task_ready` is the multi-agent coordination primitive: any
session or the `UserPromptSubmit` hook can surface the next actionable item.

The `/witan-task` skill (`mcp/servers/witan/witan/skills/witan-task/SKILL.md`) is the interactive
entry point; the `workflow-context-inject.sh` hook now also injects a **Ready
Tasks** section. Multi-user rides the existing model (`author` = creator,
`assignee` = owner, team-remote S3).

---

## Code Graph (Layer 2)

A **separate** package, `mcp/servers/witan-code/`, maintains a
tree-sitter symbol graph (`CodeFile`/`Symbol` + `Defines`/`Contains`/`Calls`/
`References`/`Imports`/`Inherits`). It is deliberately **not** folded into the
shared memory graph: it is machine-derived, high-churn, re-derivable, per-repo,
and **local-only** (never synced to the team S3 remote).

It composes with Layer 1 by **soft symbol-ID references** — strings of the form
`https://github.com/org/repo#path/file.py::Qualified.Name` stored in the
`symbolRefs` field on `Task` and `Memory` — exactly as memory already composes
with workflow via the shared repo key. There is no hard cross-store edge.

The reference resolves **both directions**: forward, an agent looks a symbol up
with the codegraph `code_*` tools and stores its id in `symbolRefs`; reverse, the
Layer-1 `context_for_symbol(symbol_id)` tool returns every memory and task whose
`symbolRefs` include that id (scoped by the repo prefix of the id), answering
"what lessons and open tasks concern this function?" before you edit it.

`Calls`/`References`/`Imports`/`Inherits` are **heuristic** (syntactic,
import-aware name resolution), suitable for agent navigation and impact hints,
not a compiler-grade call graph. See that package's README for the indexer CLI,
MCP tools (`code_find_definition`, `code_callers`, `code_impact`, …), and the
`codegraph-reindex.sh` PostToolUse hook that keeps the graph fresh during a session.
