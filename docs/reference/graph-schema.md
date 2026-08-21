<!--
  GENERATED FILE — DO NOT EDIT BY HAND.
  Regenerate with `just docs-gen` (or `./bin/gen_docs.py`).
  Source of truth: mcp/servers/witan/schema/schema.pg
-->

# Graph schema

The shape of the witan graph: what a memory, a task, a project, and a session are, and how they connect. Every MCP tool is ultimately a read or a write against these types.

Source of truth: [`mcp/servers/witan/schema/schema.pg`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/schema/schema.pg).

## Nodes

### `Memory`

Agent Memory — team-wide knowledge graph for coding agents.

One node type with a kind discriminator keeps cross-kind search simple.
Optional fields are populated only for the relevant kind:
pattern      → language
project_fact → category
lesson       → severity
agent_context → (no additional fields)

Slug convention:
pat-   pattern          e.g. pat-always-use-uv
pf-    project_fact     e.g. pf-ol-django-vault-secrets
les-   lesson           e.g. les-no-raw-sql-in-views
ctx-   agent_context    e.g. ctx-ticket-1234-approach

| Field | Type | Notes |
| --- | --- | --- |
| `slug` | `String @key` |  |
| `kind` | `enum(pattern, project_fact, lesson, agent_context) @index` |  |
| `title` | `String @index` |  |
| `content` | `String @index` | @index enables the BM25 search($m.content, …) queries |
| `repo` | `String? @index` |  |
| `language` | `String? @index` |  |
| `category` | `String? @index` |  |
| `severity` | `enum(info, warning, critical)? @index` |  |
| `author` | `String @index` |  |
| `created_at` | `DateTime @index` |  |
| `updated_at` | `DateTime` |  |
| `tags` | `[String]?` |  |
| `symbol_refs` | `[String]?` | soft refs into the code-graph store (repo#path::Name) |
| `confidence` | `F32?` | author/agent-set trust 0.0–1.0; null treated as default |

### `Topic`

Topic: a join-surface node memories attach to. One node type, several kinds:
topic    — promoted from a free-string tag
contract — name == bridge key_norm (env_var/endpoint/package/service)
symbol   — reserved; not populated in the first cut (symbols stay soft refs)
entity   — a named entity (service, library, person, concept)
Slug convention: tp-&lt;kind&gt;-&lt;slug(name)&gt;

| Field | Type | Notes |
| --- | --- | --- |
| `slug` | `String @key` |  |
| `name` | `String @index` |  |
| `kind` | `enum(topic, contract, symbol, entity) @index` |  |
| `created_at` | `DateTime @index` |  |

### `WorkflowProject`

WorkflowProject tracks an overarching engineering objective across
multiple sessions and phases. One project per logical unit of work,
regardless of how many Claude Code sessions contribute to it.

Slug convention: wp-&lt;sanitised-title&gt;-&lt;6hex&gt;
e.g. wp-add-vault-k8s-auth-a3f912

| Field | Type | Notes |
| --- | --- | --- |
| `slug` | `String @key` |  |
| `title` | `String @index` |  |
| `description` | `String` |  |
| `repos` | `[String]?` |  |
| `status` | `enum(active, completed, abandoned) @index` |  |
| `phase` | `enum(discovery, spec, implementation, delivery) @index` |  |
| `author` | `String @index` |  |
| `created_at` | `DateTime @index` |  |
| `updated_at` | `DateTime` |  |
| `completed_at` | `DateTime?` |  |
| `tags` | `[String]?` |  |
| `github_issue` | `String?` |  |
| `github_pr` | `String?` |  |
| `blocked_by` | `[String]?` | denormalized blocker project slugs (drives ready-work) |

### `WorkflowSession`

WorkflowSession tracks a single Claude Code session contributing to
a project. One project has many sessions; sessions may run in parallel.

project_slug is denormalized for fast indexed lookup without graph traversal.
Slug convention: ws-&lt;project-slug-prefix&gt;-&lt;6hex&gt;

| Field | Type | Notes |
| --- | --- | --- |
| `slug` | `String @key` |  |
| `project_slug` | `String @index` |  |
| `session_id` | `String @index` |  |
| `repo` | `String? @index` |  |
| `phase` | `enum(discovery, spec, implementation, delivery) @index` |  |
| `summary` | `String` |  |
| `tools_used` | `[String]?` |  |
| `files_changed` | `[String]?` |  |
| `author` | `String @index` |  |
| `started_at` | `DateTime @index` |  |
| `ended_at` | `DateTime?` |  |
| `tags` | `[String]?` |  |
| `superseded_by` | `String?` |  |

### `WorkflowTrace`

WorkflowTrace is an assembled, corpus-ready record of a completed project.
Created by workflow_project_complete. Immutable after creation.
Used for downstream pattern mining to generate skills and hooks.

Slug convention: wt-&lt;project-slug&gt;

| Field | Type | Notes |
| --- | --- | --- |
| `slug` | `String @key` |  |
| `project_slug` | `String @index` |  |
| `repos` | `[String]?` |  |
| `title` | `String @index` |  |
| `description` | `String` |  |
| `session_count` | `I32` |  |
| `phases` | `[String]` |  |
| `duration` | `I32?` |  |
| `outcome` | `String` |  |
| `lessons_slug` | `[String]?` |  |
| `patterns_slug` | `[String]?` |  |
| `author` | `String @index` |  |
| `created_at` | `DateTime @index` |  |
| `tags` | `[String]?` |  |

### `Task`

A dependency-aware task tracker (beads-like) living in the same graph as
memory and workflow so tasks can hard-link to projects, sessions, and
memories. The "ready work" query (open tasks with no open blocker) is the
core multi-agent coordination primitive.

Tasks are hierarchical: an `epic` decomposes into child tasks/sub-issues via
the ParentOf edge, with parent_slug denormalized for fast child lookup.

Slug convention: tk-&lt;sanitised-title&gt;-&lt;6hex&gt;
e.g. tk-wire-vault-sidecar-9c1d04

| Field | Type | Notes |
| --- | --- | --- |
| `slug` | `String @key` |  |
| `title` | `String @index` |  |
| `description` | `String` |  |
| `repo` | `String? @index` |  |
| `type` | `enum(bug, feature, task, chore, epic) @index` |  |
| `status` | `enum(open, in_progress, blocked, closed) @index` |  |
| `priority` | `enum(p0, p1, p2, p3) @index` |  |
| `project_slug` | `String? @index` | denormalized link to WorkflowProject |
| `parent_slug` | `String? @index` | denormalized hierarchy (epic → sub-issue) |
| `blocked_by` | `[String]?` | denormalized blocker slugs (drives ready-work) |
| `assignee` | `String? @index` | who owns it (vs author = creator) |
| `external_uri` | `String? @index` | GitHub issue/PR or any reference URI |
| `resolution` | `String?` | free-text note set when closed |
| `author` | `String @index` |  |
| `created_at` | `DateTime @index` |  |
| `updated_at` | `DateTime` |  |
| `closed_at` | `DateTime?` |  |
| `claimed_at` | `DateTime?` | advisory-claim lease start (assignee holds it) |
| `symbol_refs` | `[String]?` | soft refs into the code-graph store |
| `tags` | `[String]?` |  |

### `CodeBranch`

Links a git branch to the task/project it is carrying, so "which branch
carries task X" and "which tasks are in flight on branch B" are one-hop
graph queries. Coordination state — lives here (shared, durable), not in
witan-code's per-repo/bridge omnigraph stores, which are local
re-derivable caches that `witan-code branches --prune` may destroy at any
time. The coupling to witan-code stays one-way, via the raw git branch
name as the shared vocabulary: `branch` is always the branch as git names
it (e.g. "feature/new-api"), never witan-code's sanitized omnigraph
branch name ("feature_new-api") — that sanitization is a witan-code
storage detail and must not leak here.

Slug convention: "&lt;repo URI&gt;|&lt;git branch&gt;"

| Field | Type | Notes |
| --- | --- | --- |
| `slug` | `String @key` |  |
| `repo` | `String @index` |  |
| `branch` | `String @index` |  |
| `status` | `enum(active, merged, abandoned) @index` |  |
| `created_at` | `DateTime @index` |  |
| `updated_at` | `DateTime` |  |

## Edges

Edges are directional and typed. A traversal names the edge in lowercase (`supersedes`, `blocks`), while the schema declares it in PascalCase.

| Edge | From | To | Meaning |
| --- | --- | --- | --- |
| `Supersedes` | `Memory` | `Memory` | Supersedes: a newer memory replaces an older one. Link new → old when updating a pattern or lesson that has changed. |
| `AppliesTo` | `Memory` | `Memory` | AppliesTo: links a pattern or lesson to a project fact that provides context. e.g. a pattern "always use uv" AppliesTo project fact "ol-django uses uv". |
| `Refines` | `Memory` | `Memory` | Refines: a newer memory sharpens/extends an older one without replacing it. |
| `Contradicts` | `Memory` | `Memory` | Contradicts: two memories conflict. Symmetric in meaning; stored one direction and traversed both ways. Never hidden — surfaced for review. |
| `RelatedTo` | `Memory` | `Memory` | RelatedTo: soft associative link. Symmetric; stored one direction. |
| `Tagged` | `Memory` | `Topic` | Tagged: a Memory is about a Topic. Real Layer-1 edge (traversable). |
| `BelongsTo` | `WorkflowSession` | `WorkflowProject` | BelongsTo: links each WorkflowSession to its WorkflowProject. |
| `Produced` | `WorkflowProject` | `WorkflowTrace` | Produced: links a completed WorkflowProject to its WorkflowTrace (one-to-one). |
| `Informed` | `WorkflowProject` | `Memory` | Informed: links a WorkflowProject to Memory nodes consulted or created during the project (patterns, lessons, agent_context, project_facts). |
| `SessionProduced` | `WorkflowSession` | `Memory` | SessionProduced: a WorkflowSession created or substantively updated a Memory. Session-grain provenance (Informed is project-grain). The bare name `Produced` is taken (WorkflowProject -&gt; WorkflowTrace), hence the qualified name. |
| `ProjectBlocks` | `WorkflowProject` | `WorkflowProject` | ProjectBlocks: a blocking project must complete before the blocked project is "ready". |
| `Blocks` | `Task` | `Task` | Blocks: a blocker task must close before the blocked task is "ready". |
| `ParentOf` | `Task` | `Task` | ParentOf: hierarchy — an epic (or parent task) contains child tasks. |
| `DiscoveredFrom` | `Task` | `Task` | DiscoveredFrom: provenance — a task surfaced while working another task. |
| `TaskBelongsTo` | `Task` | `WorkflowProject` | TaskBelongsTo: a task rolls up to a WorkflowProject. |
| `Addresses` | `Task` | `Memory` | Addresses: a task is motivated by a Memory node (lesson, project fact). |
| `Closes` | `WorkflowSession` | `Task` | Closes: a WorkflowSession executed/closed a task. |
| `WorksOn` | `CodeBranch` | `Task` | WorksOn: a CodeBranch is carrying out a Task. |
| `ForProject` | `CodeBranch` | `WorkflowProject` | ForProject: a CodeBranch belongs to a WorkflowProject. |
