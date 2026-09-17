# agent-config-kit — inline config-level overlays — Spec

Status: design only — nothing here is implemented yet.

Builds on the shipped profiles/composition/scoping layer described in
[`agent-config-kit-profiles-composition-spec.md`](./agent-config-kit-profiles-composition-spec.md)
(manifests, `include`, `[[org]]`/`[[scope]]` routing, the global
`config.toml`). That spec's O2 zero-arg resolution order is the starting
point here; this spec doesn't change it, it adds a layer on top.

## 1. Motivation

A concrete gap, found while setting up a personal `config.toml`
(`~/.config/agent-config-kit/config.toml`): four MCP servers
(`filesystem`, `sequential-thinking`, `semble`, `Mantic`) are
general-purpose, used from every coding-agent platform regardless of repo,
and were hand-duplicated across `~/.pi/agent/mcp.json` and
`~/.config/Code/User/mcp.json` (copilot) — already drifted, one file has an
extra `memory` server the other doesn't.

The obvious fix is a personal manifest wired up via `default_manifest`. But
O2 resolution (`resolve.py:139-187`) is **first-hit-wins**: in any
`mitodl/*` repo, the `[[org]]` match resolves before `default_manifest` is
even consulted, so the personal manifest's entries are skipped for that
invocation entirely. They only stay present afterward because they're
global-scope writes that persist once applied from *some* other directory —
`apply` isn't layered, each zero-arg invocation picks exactly one manifest.

There is no way today to say "always apply these entries, regardless of
which org/scope/default manifest resolves" without pasting them into every
manifest that might fire — exactly the kind of duplication `config.toml`
was supposed to eliminate.

## 2. Goal

Let `config.toml` declare inline capability tables — `mcp_servers`,
`skills`, `hooks`, `lsp_servers` — that merge on top of whatever manifest
O2 resolves, at up to two levels per invocation:

1. **global** — merges in on every `agent-kit apply`, unconditionally.
2. **matched org/scope** — merges in only when that particular `[[org]]` or
   `[[scope]]` entry is the one O2 resolved (not unioned across all of
   them).

No new merge logic: reuse `manifest._merge_manifest_data` (`manifest.py:204-239`)
— the same function that already backs `include` — by building a synthetic,
in-memory manifest fragment from the config's own inline tables and merging
it onto the resolved manifest the same way an `include` ref would be.

## 3. Decisions (draft — for review)

| # | Question | Proposed decision |
|---|---|---|
| I1 | Overlay mechanism | A synthetic `ManifestBundle`-shaped fragment built from `config.toml`'s own inline tables, merged onto the resolved manifest's raw data via the existing `_merge_manifest_data`, before `load_manifest`'s validation/profile steps run. |
| I2 | Overlay schema | Reuses `ManifestBundle`'s own table shapes verbatim (`[mcp_servers.*]`, `[skills]`, `hooks = [...]`, `[lsp_servers.*]`) — one format to learn, not a parallel mini-schema. |
| I3 | Layering scope | Global overlay always merges in. Org/scope overlay merges in only for the entry that actually resolved this invocation — not a union across every configured org/scope. |
| I4 | Precedence on key collision | **Open, needs sign-off.** Candidate: overlay wins (extends C2's "local/overlay wins" — the overlay is the most-local, most-specific-to-this-machine layer, sitting logically "after" whatever manifest resolved). Alternative: resolved manifest wins, so a shared org manifest can't be silently shadowed by a stale personal entry. Leaning toward overlay-wins but this is the highest-risk-of-surprise decision in this spec. |
| I5 | Apply-output visibility | `agent-kit apply` must print which overlay(s) fired, e.g. `+ 2 inline entries from config.toml (global)`, mirroring the existing `resolved manifest from org 'mitodl'` legibility line (spec `agent-config-kit-profiles-composition-spec.md` §7.2). |
| I6 | `config init --wizard` support | Deferred to a follow-up. Overlay entries are hand-edited TOML in v1, same as manifest content itself isn't wizard-scaffolded today. |
| I7 | Applies with an explicit `MANIFEST` arg? | **Open.** Does the overlay still merge in when `agent-kit apply some/other.toml` is run directly (O2 step 1, not zero-arg)? Leaning yes — the "always-on personal tools" motivation doesn't stop mattering just because a manifest was named explicitly — but this is the second highest-risk-of-surprise decision here, since it means `config.toml` can silently affect a fully-explicit invocation. |

## 4. Schema

```toml
# ~/.config/agent-config-kit/config.toml

default_manifest = "~/code/personal/dotfiles/agent-config.toml"

[overlay]                              # NEW — merges into every resolved manifest
[overlay.mcp_servers.memory]
kind    = "stdio"
command = "npx"
args    = ["-y", "@modelcontextprotocol/server-memory"]

[[org]]
name     = "mitodl"
manifest = "git+https://github.com/mitodl/agent-kit@main#subdirectory=agent-config.toml"
profiles = ["universal", "infrastructure", "process"]

[org.overlay.skills.personal-notes]    # NEW — merges only when THIS org entry resolves
skill_md_path = "~/code/personal/dotfiles/skills/personal-notes/SKILL.md"

[[scope]]
match_prefix = "~/code/mit"
manifest     = "https://raw.githubusercontent.com/mitodl/agent-config/main/agent-config.toml"
profiles     = ["platform-eng"]

[scope.overlay.mcp_servers.local-proxy]
kind    = "stdio"
command = "my-local-proxy"
```

`[overlay]` at the top level is the global layer (I3). `[org.overlay]` /
`[scope.overlay]` nest inside their respective `[[org]]`/`[[scope]]` entry
and only apply when that specific entry resolves.

## 5. Merge order (most-wins-last, extends spec §5.3)

```
included manifests (existing C2, depth-first left-to-right)
  → resolved manifest's own top-level entries
    → matched [[org]]/[[scope]] overlay, if any (I3)
      → global [overlay] (I3)
        → CLI flags (--platform, --scope, --profile)
```

Per I4, "resolved manifest's own entries" vs. "overlay" ordering in this
chain is exactly the open question — the diagram above assumes overlay-wins
pending that decision.

## 6. Open questions

- **O-OVERLAY-PRECEDENCE** (I4) — overlay-wins vs. manifest-wins on a
  same-keyed entry. Blocks implementation; needs a decision.
- **O-OVERLAY-EXPLICIT** (I7) — does overlay merge in when `MANIFEST` is
  given explicitly on the CLI, or only for zero-arg resolution? Blocks
  implementation.
- **O-OVERLAY-PROFILES** — overlay entries have no `[profiles]` table of
  their own (no manifest file to attach one to). Do they always apply
  regardless of `--profile` selection (profile-independent, like
  `instructions` per O-INSTR), or does an unprofiled overlay entry need to
  be reachable by name from the *resolved* manifest's own profiles somehow?
  Leaning toward always-apply — simplest, matches the "personal tools that
  are always on" motivation — but not decided.
- **O-OVERLAY-VALIDATE** — does `agent-kit validate`/`agent-kit profiles`
  show overlay entries folded into the reported bundle, or break them out
  separately so drift-checking stays legible about *which* layer introduced
  a given entry?

## 7. Non-goals (v1)

- No new merge-strategy machinery beyond reusing `_merge_manifest_data`.
- No overlay-of-overlay composition — `config.toml`'s `[overlay]` doesn't
  itself get an `include`; if that's needed, point it at a real manifest
  file instead (the existing `include` mechanism already does this job).
- No wizard support for authoring overlay entries in v1 (I6).

## 8. Implementation sketch (once §3's open questions are resolved)

- `config.py`: add `overlay: dict[str, Any] = Field(default_factory=dict)`
  to `GlobalConfig`, and the same field to `OrgConfig`/`ScopeConfig` — kept
  as a permissive raw dict (not a typed model) at the config layer, same
  reasoning `ManifestBundle.skills` already uses (`manifest.py:30-38`):
  real per-entry validation happens once, downstream, against
  `ManifestBundle` itself, so a malformed overlay entry raises the same
  clean error a malformed manifest entry would.
- `resolve.py`: `ResolvedManifest` (`resolve.py:33-45`) gains an
  `overlay: dict | None` field, populated from `config.overlay` merged with
  the matched org/scope's own `overlay` (I3), following the exact pattern
  `profiles`/`write_scope` already use on that dataclass.
- `cli.py` `apply_command`/`validate_command`: after `load_manifest`, if the
  resolved source carries a non-empty `overlay`, merge it into the loaded
  manifest's raw bundle via `manifest._merge_manifest_data` before
  `resolve_profile` runs — same call `_load_raw_manifest` makes for each
  `include` ref (`manifest.py:356-364`) — then print the visibility line
  from I5.
