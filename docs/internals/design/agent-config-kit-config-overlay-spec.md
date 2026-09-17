# agent-config-kit — config overlays & staleness detection — Spec

Status: design only — nothing here is implemented yet.

Builds on the shipped profiles/composition/scoping layer described in
[`agent-config-kit-profiles-composition-spec.md`](./agent-config-kit-profiles-composition-spec.md)
(manifests, `include`, `[[org]]`/`[[scope]]` routing, the global
`config.toml`). That spec's O2 zero-arg resolution order is the starting
point here; neither feature below changes it.

Two independent features, each usable without the other:

1. **Inline config-level overlays** (§1-8) — let `config.toml` declare
   capability entries that merge onto whatever manifest resolves, so
   "always-on" personal tools don't get silently skipped by O2's
   first-hit-wins resolution.
2. **Staleness detection for skills/plugin hooks** (§9) — let `validate`/
   `apply` notice when an installed skill's or plugin hook's content no
   longer matches what the manifest's current source would install, not
   just whether it's present.

# Part A — inline config-level overlays

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
| I8 | Relative-path resolution inside an overlay | **Decided:** relative to `config.toml`'s own directory, never to whichever manifest happened to resolve. The existing loader resolves a manifest's own `skill_md_path`/`entry_path` relative to *that manifest's* directory (`manifest.py:95-118`, `manifest_dir = path.parent`) — an overlay entry has no such natural anchor, since the manifest it merges onto varies by which O2 branch fired (a local repo path, or a fetched `git+`/`https://` cache dir under `~/.cache/agent-config-kit/manifests/<hash>/`). Anchoring to the *resolved manifest's* directory instead would make the same overlay entry in the same `config.toml` resolve to a different file — or silently fail — depending on which org/scope won this invocation, which is not something an overlay author can reasonably predict or test for. Anchoring to `config.toml`'s own directory is stable regardless of which manifest resolves, matching how the rest of `config.toml` already treats its own paths (`default_manifest`, `[[org]].manifest`, `[[scope]].manifest` all resolve — or are expected as absolute/`~`-expanded — independent of CWD or any other manifest). |

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

Per I8, a relative `skill_md_path`/`entry_path` inside any `[overlay]`
table (global, org, or scope) resolves against `config.toml`'s own
directory — never against whatever manifest this overlay ends up merged
onto. The example above uses absolute/`~`-expanded paths throughout to
sidestep the question entirely; a relative path is also valid, but always
means "relative to `config.toml`."

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
- `manifest.py`: `load_manifest()` gains an `overlay: dict | None = None`
  keyword parameter. **Corrected from an earlier draft of this sketch**,
  which had the merge happening "after `load_manifest`" against a "raw
  bundle" that doesn't exist by that point: `load_manifest()` returns a
  validated `Manifest` dataclass (`manifest.py:88-92`), not a dict, and
  `_merge_manifest_data` only operates on the still-raw dict
  `_load_raw_manifest` produces (`manifest.py:204-239`) — there is nothing
  left to merge into once validation has already run. The merge has to
  happen *inside* `load_manifest`, between `_load_raw_manifest()` resolving
  the `include` chain and `ManifestBundle.model_validate`/`_parse_profiles`
  consuming the result: `data = _merge_manifest_data(data, overlay, path)`
  (overlay as the `overlay` argument, the resolved+included manifest as
  `base`, so I4's precedence decision controls the direction) right after
  `data = _load_raw_manifest(path, resolved_cache_dir, [])` in
  `manifest.py:546`, before `options_data = data.pop("options", {})`.
- `cli.py` `apply_command`/`validate_command`: build the resolved overlay
  dict (I3's global ∪ matched org/scope) before calling `load_manifest`, and
  pass it straight through as `load_manifest(manifest_path,
  cache_dir=cache_dir, overlay=resolved_overlay)`. Print the I5 visibility
  line once that call returns.

# Part B — staleness detection for skills/plugin hooks

## 9. Staleness detection

### 9.1 Motivation

`validate`'s drift detection is presence/absence only, never content.
`_diff_skills` (`diff.py:117-134`) checks exactly one thing per skill —
`dest.exists()` — and reports nothing else. A skill's `SKILL.md` (or a
supporting file under `scripts/`/`references/`/`assets/`) can change
upstream — a newer commit on a `git+` ref, a new tag, a local repo edit —
and `agent-kit validate` reports zero drift as long as the installed copy
is still *there*. `fetch.py`'s own module docstring already names this as a
known limitation: "content-level drift is never actionable, only
presence/absence is."

The prune lock file already records a `manifest_hash` — a SHA-256 of the
manifest file's own raw bytes (`prune.py:97-98`, written at `prune.py:138`)
— but nothing ever reads it back: a repo-wide grep for `manifest_hash`
turns up only its own definition and the one write site, zero comparisons
anywhere. It's dead weight today, and even wired up it wouldn't close this
gap: it hashes the manifest *file's own bytes*, not the content at the
paths that file merely points to, so a `[skills]` entry whose path is
unchanged but whose target content changed leaves `manifest_hash` unchanged
too.

### 9.2 Goal

Let `agent-kit validate` report when an installed skill's or plugin hook's
on-disk content no longer matches what the manifest's currently-resolved
source would install — a new **stale** category, distinct from *missing*
— and let a plain `agent-kit apply` print a short warning for the same
condition before it does its normal (already-idempotent, always-overwrites)
merge. No `--prune` required.

### 9.3 Decisions (draft — for review)

| # | Question | Proposed decision |
|---|---|---|
| S1 | What gets hashed | Per skill: the sorted concatenation of every file `skill_files()` (`installers.py`) already walks for that skill — the exact tree `install_skills` copies, so the hash is precisely "would this copy differ." Per plugin hook: the `entry_path` file's own bytes. Inline manifest content (`mcp_servers`, declarative hooks, `options`) is a separate, already-recorded (if unused) concern — see S6. |
| S2 | Where hashes live | A **new** state file, sibling to the existing prune lock file, written unconditionally on every `apply` — not gated on `--prune`. Reusing `<manifest>.lock.json` directly was considered and rejected: that file's mere existence today signals "this manifest has prune ownership tracking" (its entries are what a later `--prune` is allowed to remove); making a plain `apply` always write it would silently opt every manifest into prune bookkeeping nobody asked for. |
| S3 | Comparison direction | At `validate`/`apply` time, recompute each skill/hook's hash from the manifest's *currently resolved* source — already fetched via the normal `load_manifest()` path, no extra network round trip — and compare against the last-recorded hash. A resolved skill with no recorded hash (first-ever apply) is new, not stale. |
| S4 | `apply` behavior on staleness | Print a warning (`⚠ 2 skill(s) changed upstream since last apply: commit, webapp-testing`) before the merge, then proceed with the normal apply. Never blocking — `apply` already re-copies every skill on every run regardless of whether it changed, so the "stale" condition is corrected as a side effect of the very `apply` that reported it. |
| S5 | `validate` behavior on staleness | New `Drift.stale_keys` field, parallel to `missing_keys`/`mismatched_keys`, participating in `has_drift` (and thus `validate`'s exit code 1) the same as the existing categories — so a CI-style `validate` run catches upstream skill changes, not just missing installs. |
| S6 | Inline manifest-content staleness | Out of scope for v1. Wiring up the existing (currently dead) `manifest_hash` for a manifest's own inline content is a smaller, separate follow-up; this spec closes the skill/plugin-hook content gap specifically, since that one has *zero* coverage today versus `manifest_hash`'s "recorded but unused." |

### 9.4 Schema

New sibling file, `<manifest>.applied.json` (parallel to the existing
`<manifest>.lock.json`), keyed by platform:

```json
{
  "manifest_hash": "sha256:...",
  "platforms": {
    "claude": {
      "skills": {
        "commit": "sha256:...",
        "webapp-testing": "sha256:..."
      },
      "hooks": {
        "plugin:witan.ts": "sha256:..."
      }
    }
  }
}
```

Deliberately narrower than `PlatformState` (`prune.py:52-59`) — no
`mcp_servers` entry, since S1 scopes this file to content the manifest only
*points at*, not content the manifest inlines directly.

### 9.5 Interaction with `--prune`

A manifest can have neither, either, or both sibling files, independently:

- `<manifest>.applied.json` alone — staleness detection on plain
  `apply`/`validate`, no prune ownership.
- `<manifest>.lock.json` alone (today's behavior, unchanged) — prune
  ownership, no staleness detection.
- Both — a manifest that's also been `--prune`d at some point. No shared
  fields; each file is read/written independently, so neither feature's
  bug can corrupt the other's state.

### 9.6 Open questions

- **O-STALE-SCOPE** (S6) — extend to inline manifest content (finally
  wiring up `manifest_hash` itself) in v1, or defer? Leaning defer, but
  flagging since it's the more complete fix and touches the same
  machinery.
- **O-STALE-WRITE-COST** — writing `<manifest>.applied.json` on every
  `apply` (S2) costs one file read per skill/hook (to hash current
  content) plus one write, even when nothing changed. Negligible for a
  handful of skills; open whether a very large catalog warrants skipping
  the write when the computed content is unchanged from what's recorded.
- **O-STALE-REMOTE-COST** — hashing "current" content for a `git+`-sourced
  skill piggybacks on the shallow clone `fetch_remote` already performs on
  every `load_manifest()` call (`fetch.py:20-30`) — no additional network
  cost, but worth stating explicitly since it's easy to assume otherwise.

### 9.7 Non-goals (v1)

- No semantic diff of *what* changed in a skill — changed-or-not only,
  matching the "presence/absence, never content-level detail" posture
  `fetch.py` already documents for the analogous remote-source case.
- No staleness detection for inline manifest content (S6/O-STALE-SCOPE).
- No new re-apply trigger — `apply` already re-copies every skill on every
  run; this only adds visibility into what changed, not a new action.

### 9.8 Implementation sketch (once §9.3's open questions are resolved)

- `installers.py`: add a `skill_content_hash(skill: SkillSource) -> str`
  next to the existing `skill_files()` — same walk, hashes bytes instead of
  listing relative paths.
- `prune.py`: new `AppliedState`/`load_applied_state`/`write_applied_state`
  mirroring `PlatformState`/`load_state`/`write_state` (`prune.py:52-151`)
  but for the narrower S4 schema, written unconditionally from
  `plan.apply()`/`apply_all()` — unlike `PlatformState`, never gated on the
  `--prune` flag.
- `diff.py`: `_diff_skills` (`diff.py:117-134`) gains a hash comparison
  alongside its existing `dest.exists()` check, populating the new
  `Drift.stale_keys` (S5).
- `cli.py`: `apply_command` prints the S4 warning after `load_manifest`,
  before calling `apply`/`apply_all`/`apply_with_prune`; `validate_command`'s
  `_report_drift` gains a "stale" column alongside its existing
  missing/mismatched/missing-paths/unreadable ones.
