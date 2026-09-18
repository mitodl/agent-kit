# agent-config-kit — config overlays & staleness detection — Spec

Status: both parts implemented — Part A (inline config-level overlays) and
Part B (staleness detection).

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

No new merge logic for combining the two overlay *layers* (global +
matched org/scope): reuse `manifest.merge_manifest_data` (`manifest.py`,
public — renamed from `_merge_manifest_data` to support this reuse) — the
same function that already backs `include`. Applying the combined overlay
*onto the target manifest*, though, is a separate, later step — see I1's
"implemented" note below, which supersedes this section's original
draft-time framing of "merge it onto the resolved manifest the same way an
`include` ref would be."

## 3. Decisions

| # | Question | Decision |
|---|---|---|
| I1 | Overlay mechanism | **Implemented, revised from the original draft.** The draft proposed merging the overlay into the manifest's raw TOML dict before Pydantic validation (`load_manifest`'s pipeline), the same stage `include` folds in at. Building it surfaced a real conflict with O-OVERLAY-PROFILES (below): merged at that stage, an overlay entry has no `[profiles]` membership and would be filtered out by `resolve_profile` under any `--profile` selection that doesn't happen to reference its key — the opposite of "always applies." So the overlay is instead validated into its own `RegistrationBundle` (`manifest.load_overlay_bundle`) and unioned onto the target manifest's bundle *after* `resolve_profile` runs (`manifest.apply_overlay`), where profile filtering can no longer touch it. `load_manifest()` itself is unchanged. |
| I2 | Overlay schema | Reuses `ManifestBundle`'s own table shapes verbatim (`[mcp_servers.*]`, `[skills]`, `hooks = [...]`, `[lsp_servers.*]`) — one format to learn, not a parallel mini-schema. |
| I3 | Layering scope | Global overlay always merges in. Org/scope overlay merges in only for the entry that actually resolved this invocation — not a union across every configured org/scope. Combining the two raw layers is `resolve.resolve_overlay`; global wins a key collision between them. |
| I4 | Precedence on key collision | **Decided: the resolved manifest wins**, not the overlay — the opposite of the draft's leaning. A shared org/scope manifest must never be silently shadowed by a stale or forgotten personal overlay entry; a same-named overlay entry is a harmless no-op rather than a surprise override. Implemented in `manifest.apply_overlay`. |
| I5 | Apply-output visibility | Implemented: `agent-kit apply`/`validate` print `+ N inline entries from config.toml (<source>)` (singular "entry" for N=1), where `<source>` is `global`, `org '<name>'`, `scope '<prefix>'`, or `org '<name>' + global`/`scope '<prefix>' + global` when both layers contributed — mirrors the existing `resolved manifest from org 'mitodl'` legibility line (`agent-config-kit-profiles-composition-spec.md` §7.2). |
| I6 | `config init --wizard` support | Deferred to a follow-up. Overlay entries are hand-edited TOML in v1, same as manifest content itself isn't wizard-scaffolded today. |
| I7 | Applies with an explicit `MANIFEST` arg? | **Decided: yes, on by default**, with a `--overlay`/`--no-overlay` flag on both `apply` and `validate` to opt out. Only the *global* `[overlay]` applies to an explicit `MANIFEST` — an explicit argument bypasses O2 entirely, so there is no org/scope match to consult. `--no-overlay` skips config-overlay computation entirely, including loading `config.toml` at all, for a fully self-contained explicit invocation. |
| I8 | Relative-path resolution inside an overlay | **Decided:** relative to `config.toml`'s own directory, never to whichever manifest happened to resolve. The existing loader resolves a manifest's own `skill_md_path`/`entry_path` relative to *that manifest's* directory (`manifest.py:95-118`, `manifest_dir = path.parent`) — an overlay entry has no such natural anchor, since the manifest it merges onto varies by which O2 branch fired (a local repo path, or a fetched `git+`/`https://` cache dir under `~/.cache/agent-config-kit/manifests/<hash>/`). Anchoring to the *resolved manifest's* directory instead would make the same overlay entry in the same `config.toml` resolve to a different file — or silently fail — depending on which org/scope won this invocation, which is not something an overlay author can reasonably predict or test for. Anchoring to `config.toml`'s own directory is stable regardless of which manifest resolves, matching how the rest of `config.toml` already treats its own paths (`default_manifest`, `[[org]].manifest`, `[[scope]].manifest` all resolve — or are expected as absolute/`~`-expanded — independent of CWD or any other manifest). Implemented in `manifest.load_overlay_bundle`. |

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

Two distinct merges happen at two distinct stages, not one:

```
Stage 1 — combining the overlay's own two layers (resolve.resolve_overlay):
  matched [[org]]/[[scope]] overlay, if any (I3)
    → global [overlay] (I3, wins collision)

Stage 2 — applying the combined overlay to the target manifest:
  included manifests (existing C2, depth-first left-to-right)
    → resolved manifest's own top-level entries
      → resolve_profile (--profile filtering, unaffected by the overlay —
        O-OVERLAY-PROFILES)
        → combined overlay from Stage 1 (manifest.apply_overlay — resolved
          manifest wins collision, I4)
          → CLI flags (--platform, --scope)
```

The draft's single-diagram framing assumed the overlay folds into the same
raw-dict merge chain manifests/`include` already use; I1 explains why it
doesn't (profile filtering would strip an overlay entry that isn't part of
any selected profile).

## 6. Open questions

All resolved as of Part A landing:

- **O-OVERLAY-PRECEDENCE** (I4) — *resolved: the resolved manifest wins.*
- **O-OVERLAY-EXPLICIT** (I7) — *resolved: yes, on by default, opt out via
  `--no-overlay`; only the global layer applies (no org/scope match exists
  for an explicit `MANIFEST`).*
- **O-OVERLAY-PROFILES** — *resolved: overlay entries always apply,
  regardless of `--profile` selection* — the reason I1 moved the merge
  point to after `resolve_profile` rather than before it.
- **O-OVERLAY-VALIDATE** — *resolved: folded into the reported bundle.*
  `validate_command` applies the overlay the same way `apply_command` does
  (via the same `_apply_overlay_to_bundle` helper in `cli.py`), so an
  overlay entry that isn't installed yet shows up as ordinary missing
  drift — no separate reporting path.

## 7. Non-goals (v1)

- No new merge-strategy machinery beyond reusing `merge_manifest_data` for
  Stage 1 (combining the overlay's own layers); Stage 2 (applying the
  combined overlay to the target manifest) is a `RegistrationBundle`-level
  union (`manifest.apply_overlay`), not a raw-dict merge — see I1.
- No overlay-of-overlay composition — `config.toml`'s `[overlay]` doesn't
  itself get an `include`; if that's needed, point it at a real manifest
  file instead (the existing `include` mechanism already does this job).
- No wizard support for authoring overlay entries in v1 (I6).
- No inline manifest-content staleness detection here — see Part B.

## 8. Implementation

- `config.py`: `overlay: dict[str, Any] = Field(default_factory=dict)` on
  `GlobalConfig`, `OrgConfig`, and `ScopeConfig` — a permissive raw dict
  (not a typed model), same reasoning `ManifestBundle.skills` already uses:
  real per-entry validation happens once, downstream, via
  `manifest.load_overlay_bundle`. `_expand_overlay_paths` (new)
  `~`-expands a `skill_md_path`/`entry_path` inside any of the three
  `overlay` tables at parse time — the same `~` support `default_manifest`/
  `[[org]].manifest`/`[[scope]].manifest` already have, since an overlay
  entry lives directly in `config.toml`. (Manifest-relative paths never get
  `~` expansion — M5 is relative-to-manifest-dir-or-absolute only — this is
  specific to `config.toml`'s own content.)
- `manifest.py`: `merge_manifest_data` (renamed from `_merge_manifest_data`
  — now used outside this module too) combines the overlay's own two
  layers. Two new public functions: `load_overlay_bundle(overlay,
  config_path, *, cache_dir=None) -> RegistrationBundle` runs the combined
  overlay dict through the same validate/path-resolve pipeline
  `load_manifest` runs on a manifest's own tables (I8's anchor is
  `config_path`, not any manifest's directory); `apply_overlay(bundle,
  overlay) -> RegistrationBundle` unions `overlay` onto an
  already-profile-resolved `bundle`, `bundle` winning a same-keyed
  collision (I4).
- `resolve.py`: `resolve_overlay(config, config_path, *, org_match=None,
  scope_match=None) -> tuple[dict, str]` is Stage 1 — the combined raw
  overlay dict plus the I5 source description. `ResolvedManifest` gains
  `overlay: dict` and `overlay_source: str` fields, populated at each of
  `resolve_zero_arg_manifest`'s four resolving branches (empty for the
  repo-local branch's org/scope component, since neither is consulted
  there — only the global layer can contribute).
- `cli.py`: `_resolve_manifest_arg` returns a `_ManifestResolution`
  (path/profiles/write_scope plus overlay/overlay_source/overlay_config_path)
  instead of a 3-tuple, gains an `overlay: bool` parameter (I7) controlling
  whether config-overlay computation happens at all. `apply_command`/
  `validate_command` both gain a `--overlay`/`--no-overlay` flag (default
  on) and call the new `_apply_overlay_to_bundle` helper — which builds the
  overlay bundle, prints the I5 line, and calls `manifest.apply_overlay` —
  right after `resolve_profile`, inside the existing `try`/`except
  ManifestError` block.

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

### 9.3 Decisions

| # | Question | Decision |
|---|---|---|
| S1 | What gets hashed | Per skill: every file `skill_files()` (`installers.py`) already walks for that skill, folded into one SHA-256 alongside each file's relative path (not just its bytes — two skills whose content is identical but split across files differently must not hash the same) — the exact tree `install_skills` copies, so the hash is precisely "would this copy differ" (`installers.skill_content_hash`). Per plugin hook: the `entry_path` file's own bytes (`installers.hook_content_hash`). Inline manifest content (`mcp_servers`, declarative hooks, `options`) is a separate, already-recorded (if unused) concern — see S6. |
| S2 | Where hashes live | A **new** state file, sibling to the existing prune lock file, written unconditionally on every `apply` — not gated on `--prune`. Reusing `<manifest>.lock.json` directly was considered and rejected: that file's mere existence today signals "this manifest has prune ownership tracking" (its entries are what a later `--prune` is allowed to remove); making a plain `apply` always write it would silently opt every manifest into prune bookkeeping nobody asked for. |
| S3 | Comparison direction | At `validate`/`apply` time, recompute each skill/hook's hash from the manifest's *currently resolved* source — already fetched via the normal `load_manifest()` path, no extra network round trip — and compare against the last-recorded hash. A resolved skill with no recorded hash (first-ever apply, or a platform the manifest wasn't previously applied to) is new, not stale (`prune.stale_names`). |
| S4 | `apply` behavior on staleness | Print a warning (`⚠ N entry/entries changed upstream since last apply: <names>`) before the install step, then proceed with the normal apply. Never blocking — `apply` already re-copies every skill on every run regardless of whether it changed, so the "stale" condition is corrected as a side effect of the very `apply` that reported it. Skipped under `--dry-run`, same as the state write itself (S2). |
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

- **O-STALE-SCOPE** (S6) — *resolved: deferred.* Extending to inline
  manifest content (finally wiring up `manifest_hash` itself) stays a
  separate follow-up; not implemented here.
- **O-STALE-WRITE-COST** — *resolved: always write, no skip-if-unchanged
  optimization.* Writing `<manifest>.applied.json` on every `apply` costs
  one read per skill/hook (to hash current content) plus one write, even
  when nothing changed — negligible for the catalog sizes seen so far. Revisit
  if a large-catalog manifest makes this measurable.
- **O-STALE-REMOTE-COST** — *resolved, as stated:* hashing "current"
  content for a `git+`-sourced skill piggybacks on the shallow clone
  `fetch_remote` already performs on every `load_manifest()` call
  (`fetch.py:20-30`) — no additional network cost. No code change needed;
  this was a documentation note, not a decision.

### 9.7 Non-goals (v1)

- No semantic diff of *what* changed in a skill — changed-or-not only,
  matching the "presence/absence, never content-level detail" posture
  `fetch.py` already documents for the analogous remote-source case.
- No staleness detection for inline manifest content (S6/O-STALE-SCOPE).
- No new re-apply trigger — `apply` already re-copies every skill on every
  run; this only adds visibility into what changed, not a new action.

### 9.8 Implementation

- `installers.py`: `skill_content_hash(skill: SkillSource) -> str` and
  `hook_content_hash(entry_path: Path) -> str`, next to the existing
  `skill_files()`.
- `prune.py`: `AppliedState` (dataclass: `skills`/`hooks` dicts, name/identity
  → hash), `bundle_applied_state(bundle) -> AppliedState`,
  `default_applied_state_path`/`load_applied_state`/`write_applied_state`
  mirroring `PlatformState`/`default_state_path`/`load_state`/`write_state`
  for the narrower S4 schema. `stale_names(current, previous) -> list[str]`
  is the shared comparison both `diff.py` and `cli.py` use (S3).
- `diff.py`: `_diff_skills`/`_diff_hooks` gain a `previous: AppliedState |
  None` parameter; `diff()` too, defaulting to `None` (skip staleness
  checking entirely — presence/absence drift only, unchanged from before
  this feature). Populates the new `Drift.stale_keys` (S5).
- `cli.py`: **corrected from the original sketch**, which said the state
  write happens "unconditionally from `plan.apply()`/`apply_all()`" —
  `plan.py` is deliberately manifest-path-agnostic (shared by other
  consumers, e.g. witan, that have no `<manifest>` file at all), so it has
  no path to name `<manifest>.applied.json` after. The existing prune lock
  file has exactly the same constraint and for the same reason lives
  outside `plan.py`: `write_state` is already called from `cli.py`'s
  `apply_command`, not from `plan.apply()`/`apply_all()`. The applied-state
  write follows the identical pattern: computed and printed (S4's warning)
  before the install step, written (unconditionally, skipped under
  `--dry-run`) after it, merged into whatever `load_applied_state` already
  returned so a single-`--platform` run doesn't erase every other
  platform's recorded state — same read-merge-write contract `write_state`
  already documents. The cross-repo path redirect `_default_prune_state_path`
  already has (O-STATE, spec `agent-config-kit-profiles-composition-spec.md`
  §9) is shared via a new `_redirect_project_scope_state_path` helper, used
  by both `_default_prune_state_path` and the new
  `_default_applied_state_path` — same clobbering risk, same fix, different
  filename (each state kind still gets its own file — S2/§9.5). `validate_command`
  loads `<manifest>.applied.json` and passes each platform's recorded
  `AppliedState` into `diff_bundle`; `_report_drift` gains a "stale" column.
