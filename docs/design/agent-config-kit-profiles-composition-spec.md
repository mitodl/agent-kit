# agent-config-kit — profiles, composition & scoped provisioning — Spec

Project: `wp-agent-config-kit-profiles-composition-scoped-pro-96a2a2`
Phase: discovery → spec
Status: design only — nothing here is implemented yet.

Builds on the shipped manifest/CLI layer described in
[`agent-config-kit-cli-spec.md`](./agent-config-kit-cli-spec.md) (the `ac-kit`
console script, `load_manifest()`, `apply`/`apply_all`, `fetch.py`'s
`https://`/`git+` resolution, `prune.py`'s state file). That project
(`wp-cross-agent-coding-agent-config-management-libra-5593b0`) is complete;
this spec is the next body of work on top of it.

## 1. Goal

Five behavioral extensions, in dependency order:

1. **Profiles** — named, stackable bundles (`universal`, `frontend`,
   `application-dev`, `platform-eng`, `manager`, …) so a team declares its
   skills/MCP servers/hooks once and slices them into role-targeted sets,
   selected at apply time.
2. **Composition / includes** — a manifest can `include` other manifests
   (local paths *and* remote `https://`/`git+` URIs), so org-wide bundles are
   declared once and reused without re-declaration.
3. **Directory-prefix & project scoping** — provision different config under
   `~/code/mit` vs `~/code/personal` (and per-project targets), driven by a
   global config that routes a working directory to a manifest + profile.
4. **GitHub org-scoped apply** — `ac-kit apply` in a freshly cloned
   `github.com/mitodl/*` repo detects the org from the git remote and applies
   that org's manifest + default profile automatically.
5. **Global config file** — a user-level `config.toml` that holds the default
   manifests/profiles per scope (global, per-org, per-directory-prefix), so
   the ergonomic zero-argument `ac-kit apply` "just works".

A running example of the end state (feature 4): clone a mitodl repo, run
`ac-kit apply` with no arguments, and the mitodl org manifest's
`platform-eng` profile is installed — because the global config maps the
`mitodl` org (and/or the `~/code/mit` prefix) to that manifest+profile.

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| P1 | Profile model | **Named subsets in a manifest** (chosen over one-file-per-profile and "both"). All entries are defined once in the manifest's top-level tables; `[profiles.<name>]` tables select entries **by key** and compose via `inherits`. Single source of truth, cheap set-union stacking, trivial "does this profile reference a real entry" validation. File-level reuse is served by composition (feature 2 / §5), not by making every profile its own file. |
| F1 | Manifest format: skills | Skills become a **name→value table** (`[skills]` with `key = value`), replacing the `[[skills]]` array-of-tables. The key is the skill name (already constrained to `^[a-z0-9]+(-[a-z0-9]+)*$`, always a valid TOML bare key). The value is **either a string** (shorthand for `skill_md_path`) **or an inline table** (for future per-skill fields). Mirrors the existing `[mcp_servers.*]` name-keying and makes profile references (`skills = ["commit"]`) read against the same keys. |
| F2 | Manifest format: hooks | Hooks have no natural key, so they **stay a list** but may be written as an **inline array-of-tables** (`hooks = [ {..}, {..} ]`) to drop the repeated `[[hooks]]` headers. No semantic change. |
| F3 | Backwards compatibility | Per team convention (no BC shims), the loader adopts the F1/F2 forms outright. The old `[[skills]]` array form was only ever in the prior spec's examples + tests, not in any depended-on real manifest, so it is dropped rather than dual-supported. |
| C1 | Include mechanism | A top-level `include = ["...", ...]` list of manifest references, each a local path (resolved per M5, relative to the *including* manifest's dir) or a remote `https://`/`git+` URI (reusing `fetch.py`). A `[profiles.<name>]` may also carry its own `include` (§5.2). |
| C2 | Include merge & precedence | Includes are merged **depth-first, left-to-right**, then the including manifest's own top-level entries are merged last (**local wins**). Same-keyed entries: **last writer wins** (later include, then local). Cycles are detected (a manifest transitively including itself) and raise `ManifestError`. |
| S1 | Global config location | `${XDG_CONFIG_HOME:-~/.config}/agent-config-kit/config.toml` (overridable with `--config` / `AC_KIT_CONFIG`). Holds default manifests/profiles keyed by scope: a global default, a `[[org]]` list, and a `[[scope]]` prefix list. |
| S2 | Directory-prefix scoping | **ac-kit-side routing** — the global config's `[[scope]]` entries map a directory prefix to a manifest + profile; zero-arg `ac-kit apply` picks the **longest-matching** prefix for the CWD. Native per-agent hierarchy loading does **not** cover this case (see D-INV / §6.1), so ac-kit materializes into per-repo project-scoped targets rather than relying on the agent to walk ancestors. |
| S3 | Native vs. ac-kit division of labor | **Native-first, ac-kit fills gaps.** For the *within-repo / monorepo* case, defer to native discovery where it exists (do not fight it). For the *cross-repo directory-prefix* case (`~/code/mit` spanning many independent git repos), no agent supports it — ac-kit routing is mandatory. Established by the D-INV investigation (§6.1). |
| O1 | Org detection | **Git-remote-URL only** — parse the org/owner from `git remote get-url origin` (and other remotes as fallback). Offline, no auth, no new dependency. It intentionally does **not** verify GitHub *membership* via API; anyone who cloned the repo and has a matching `[[org]]` entry gets that config. (Membership verification is a deferred enhancement, §8, open question O-MEM.) |
| O2 | Zero-arg `apply` resolution order | `ac-kit apply` with no `MANIFEST` resolves its manifest+profile from the global config by, in order: **(1) explicit CLI flags** → **(2) a repo-local `agent-config.toml`** if present → **(3) org match** from the git remote → **(4) longest directory-prefix match** → **(5) the global default**. First hit wins for *which manifest*; profile is taken from the same source, overridable by `--profile`. |
| S4 | Project-scope targets | The registry today populates only each platform's `global` `ScopeTarget`; `project` is `None` everywhere. This work **populates `project` targets** (`.claude/skills`, `.mcp.json`, Pi/OpenCode/Copilot project paths) so `--scope project` and per-repo materialization (S2) actually write somewhere. |

## 3. Manifest format changes

### 3.1 Compact schema (F1/F2)

```toml
# agent-config.toml
instructions = "See AGENTS.md"          # optional scalar, keep before any table

include = [                             # feature 2 (§5)
  "https://cfg.mitodl.org/base.toml",
  "./team-shared.toml",
]

[options]
scope = "global"                        # "global" | "project"
platforms = ["claude", "pi"]            # optional allow-list

[mcp_servers.witan]                     # unchanged: table keyed by name
kind = "stdio"
command = "uvx"
args = ["witan", "serve"]

[skills]                                # NEW: table keyed by name (was [[skills]])
commit          = "./skills/commit/SKILL.md"                 # string  = skill_md_path
webapp-testing  = "https://cfg.mitodl.org/skills/webapp-testing/SKILL.md"
frontend-design = { skill_md_path = "./skills/frontend-design/SKILL.md" }  # table form

hooks = [                               # NEW inline array-of-tables (was [[hooks]])
  { kind = "declarative", event = "user_prompt_submit", command = "witan inject-context" },
  { kind = "plugin", entry_path = "extensions/pi/witan.ts" },
]

[profiles.universal]                    # feature 1 (§4)
skills      = ["commit"]
mcp_servers = ["witan"]

[profiles.frontend]
inherits    = ["universal"]
skills      = ["webapp-testing", "frontend-design"]
```

### 3.2 Loader changes

- `ManifestBundle` gains a `skills` field typed to accept the **name→(str |
  table)** mapping and normalize each entry to a `SkillSource(name=<key>,
  skill_md_path=<str | table["skill_md_path"]>)`. The `[[skills]]` list path
  is removed. `_resolve_relative_paths` walks the new mapping's values (and
  inline-table `skill_md_path`s) instead of the old list.
- `hooks` accepts the inline-array-of-tables form natively (already a list of
  discriminated `Hook`s — TOML inline tables parse straight into it; no model
  change beyond dropping the `[[hooks]]` assumption in any test fixtures).
- `extra="forbid"` stays; new top-level keys (`include`, `[profiles]`) are
  added to the model explicitly so a typo still errors.

## 4. Profiles (feature 1 — P1)

### 4.1 Schema

A `[profiles.<name>]` table has four optional keys, each a **list of entry
keys** referencing top-level table keys of the same manifest (post-include
merge, §5), plus `inherits`:

```toml
[profiles.<name>]
inherits    = ["<other-profile>", ...]  # union with these first
skills      = ["<skill-key>", ...]
mcp_servers = ["<mcp-key>", ...]
hooks       = ["<hook-id>", ...]         # by hook identity (see §4.3)
lsp_servers = ["<lsp-key>", ...]
```

### 4.2 Resolution

`resolve_profile(manifest, names: list[str]) -> RegistrationBundle`:

1. Expand `inherits` transitively (detect inheritance cycles → `ManifestError`).
2. Union the selected keys across all named + inherited profiles.
3. Project the manifest's fully-defined entries down to exactly the selected
   keys, producing a `RegistrationBundle`. A selected key with no matching
   top-level definition is a `ManifestError` (fail fast on a typo'd
   reference) — validated at load time against the merged entry set.
4. `instructions` is profile-independent (a scalar) — carried through as-is;
   a profile does not scope instructions in v1 (open question O-INSTR, §9).

Selecting **multiple** profiles at apply time is the union of their resolved
bundles (`--profile universal --profile frontend`). No profile selected + a
manifest that *has* profiles → open question O-DEFAULT (§9): current lean is
"apply the whole manifest (all entries), i.e. profiles are opt-in filters,
not gates" — but a manifest may set `[options] default_profiles = [...]`.

### 4.3 Hook references

Hooks are keyed for profile-selection by the same identity string `prune.py`
already uses: `f"{kind}:{event}:{command}"` for declarative,
`f"plugin:{entry_path.name}"` for plugin. A profile's `hooks = [...]` lists
those identities. (Most manifests will have few hooks; if listing identities
proves clumsy, a follow-up may allow naming hooks — deferred.)

### 4.4 CLI

```
ac-kit apply [MANIFEST] --profile NAME...      # repeatable; union
ac-kit validate [MANIFEST] --profile NAME...
ac-kit profiles [MANIFEST]                     # list a manifest's profiles + resolved entry counts
```

`--profile` intersects nothing — it *selects*. With profiles present but no
`--profile` and no `default_profiles`, see O-DEFAULT.

## 5. Composition / includes (feature 2 — C1/C2)

### 5.1 Top-level `include`

`include = [ref, ...]` where each `ref` is a local path (M5 resolution,
relative to the including manifest) or a remote URI (`fetch.py`). Loading:

1. Parse the including manifest's raw TOML.
2. For each `include` ref (depth-first, left-to-right): fetch/read, recurse
   (its own `include` first), merge its resolved entry tables into an
   accumulator.
3. Merge the including manifest's own top-level tables last (**local wins**,
   C2). `[profiles]` tables merge by profile name (local profile of the same
   name overrides an included one wholesale — not a deep merge, to keep
   "which profile am I actually running" legible).
4. Detect cycles via a visited-set of canonicalized refs (absolute path /
   normalized URI) → `ManifestError` naming the cycle.

Remote include fetch reuses `fetch_remote` (a `.toml` is just a single-file
HTTP GET; `git+#subdirectory=` for a manifest living in a repo subdir), with
the same conditional-GET / offline-fallback semantics `fetch.py` already has.

### 5.2 Per-profile `include`

A `[profiles.<name>]` may carry `include = [ref, ...]`: the referenced
manifests are merged into the entry pool, and **all their entries** are
selected into that profile (an included manifest used *as* a profile). This
is the one concession toward the "one-file-per-profile" model — a remote,
independently-versioned role bundle can be dropped into a profile by URL
without re-listing its entries. Interaction with `inherits`/explicit lists:
union of (inherited) ∪ (this profile's `include` entries) ∪ (explicit key
lists).

### 5.3 Precedence summary (most-wins-last)

```
included manifests (depth-first, left→right)
  → including manifest's own entries
    → selected profile(s)
      → CLI flags (--platform, --scope, --profile)
```

## 6. Directory-prefix & project scoping (feature 3 — S2/S3/S4)

### 6.1 Native support investigation (D-INV — findings)

Established by research (stored as witan memory
`pf-native-per-agent-skill-config-directory-hierarch-40bff8`). **No surveyed agent loads config from
a mid-tree ancestor above the git repo root** — every agent that walks up the
tree bounds the walk at the enclosing `.git`:

| Agent | Within-repo hierarchy | Mid-tree ancestor (`~/code/mit/.<agent>`) above independent repos |
|-------|----------------------|-------------------------------------------------------------------|
| Claude Code | **Yes** — `.claude/skills` walked cwd→repo-root + nested on-demand | **No** (walk stops at repo root); MCP: no walk at all |
| Codex CLI | Partial — skills cwd→repo-root; AGENTS.md root→cwd | **No** |
| OpenCode | Partial — config/skills cwd→git worktree root (inclusive) | **No** |
| Pi | Only `.agents/skills` walks (stops at git root); `.pi/skills` cwd-only | **No** |
| GitHub Copilot | Opt-in parent walk, stops at first `.git` | **No** (default off; still no when repo is its own git root) |

**Consequence:** the user's actual scenario — one shared config for many
independent repos under `~/code/mit` — is **not** natively expressible in any
agent. So:

- **Within-repo case:** defer to native (S3). ac-kit's `--scope project`
  writes to the repo-local dirs the agent already reads; nested/monorepo
  skill loading is the agent's job, not ac-kit's.
- **Cross-repo prefix case:** ac-kit routing is mandatory (S2). ac-kit
  materializes the prefix's manifest+profile into each repo's project-scoped
  targets when `ac-kit apply` runs there (or, opt-in, into the agent's global
  location). The portable lever the agents *do* expose — injecting absolute
  skill paths into a settings allowlist (OpenCode `skills.paths`, Pi
  `skills[]`, Copilot `*Locations`) — can't be prefix-scoped (global settings
  apply everywhere), so per-repo materialization is the reliable mechanism.

### 6.2 Project-scope targets (S4)

Populate each platform's `project` `ScopeTarget` in the registry (relative to
a repo root): `claude` → `.claude/skills`, `.mcp.json`, `.claude/settings.json`;
`pi` → `.pi/skills`, `.pi/extensions`, `.pi/settings.json`; `opencode` →
`.opencode/...`; `copilot` → `.github/skills`, `.vscode/mcp.json`. Enables
`ac-kit apply --scope project` and S2 materialization. Verify each path per
D-INV notes.

### 6.3 Prefix routing (S2)

Global config `[[scope]]` entries (§7) map a directory prefix to a
manifest + profile. Zero-arg `ac-kit apply` (§7.2) expands `~`, canonicalizes
the CWD, and picks the **longest** matching `match_prefix`. Default write
scope for prefix-routed applies is `project` (materialize into the current
repo); `scope = "global"` in the `[[scope]]` entry opts into writing the
prefix's config to the agent's global location instead.

## 7. Global config file (feature 5 — S1)

### 7.1 Schema

```toml
# ${XDG_CONFIG_HOME:-~/.config}/agent-config-kit/config.toml

default_manifest = "~/dotfiles/agent-config.toml"   # scope (5): fallback
default_profiles = ["universal"]

[[org]]                                              # feature 4 (§8)
name     = "mitodl"                                  # matches github.com/<name>/*
manifest = "https://cfg.mitodl.org/agent-config.toml"
profiles = ["platform-eng"]

[[org]]
name     = "my-personal-gh"
manifest = "~/dotfiles/personal-agent-config.toml"

[[scope]]                                            # feature 3 (§6.3)
match_prefix = "~/code/mit"
manifest     = "https://cfg.mitodl.org/agent-config.toml"
profiles     = ["platform-eng"]

[[scope]]
match_prefix = "~/code/personal"
manifest     = "~/dotfiles/personal-agent-config.toml"
profiles     = ["universal"]
scope        = "project"                             # write target scope (§6.3)
```

Loaded by a new `config.py` (`load_global_config()`), XDG-aware, absent-file =
empty config (never an error — zero-arg apply just falls through to "no
manifest resolved" with a clear message). Wrapped errors like `ManifestError`.

### 7.2 Zero-arg `apply` (O2)

`ac-kit apply` with no `MANIFEST` resolves per O2's order: explicit flags →
repo-local `agent-config.toml` at the repo root → `[[org]]` match (git remote,
§8) → longest `[[scope]]` prefix match → `default_manifest`. The chosen
source supplies both the manifest and its default profiles; `--profile`
overrides the profile, `--scope` overrides the write scope. `ac-kit apply`
prints *which* source resolved the manifest (so the "magic" is legible), e.g.
`resolved manifest from org 'mitodl' → profile platform-eng`.

## 8. GitHub org-scoped apply (feature 4 — O1)

`detect_org(repo_root) -> str | None`: read `git remote get-url origin` (then
other remotes), parse the owner from `github.com[:/]<owner>/<repo>` for both
SSH and HTTPS forms; return `<owner>`. No network, no `gh` dependency (O1).
Zero-arg apply (§7.2 step 3) looks the owner up in `[[org]]`; a hit supplies
that org's manifest + profiles. No match → fall through to prefix/default.

Non-goals (deferred, O-MEM §9): verifying the user is actually a *member* of
the org (would need `gh api user/memberships` + auth); private-manifest auth
beyond what `fetch.py`/`git` credential helpers already provide.

## 9. Open questions

- **O-DEFAULT** — profiles present, none selected: apply-all vs. require a
  selection vs. honor `[options] default_profiles`. Lean: `default_profiles`
  if set, else apply-all (profiles are opt-in filters).
- **O-INSTR** — should a profile be able to scope `instructions`? v1: no.
- **O-MEM** — org *membership* verification (vs. URL-only detection). Deferred
  per O1; add behind an opt-in `[[org]] verify_membership = true` if a real
  need appears.
- **O-STATE** — prune state file (cli spec §5) is per-manifest; zero-arg /
  org / prefix routing means one CWD may be applied from different manifests
  over time. Define state-file identity for resolved-manifest applies (key by
  resolved manifest hash + platform) before wiring `--prune` into zero-arg.
- **O-PRIORITY** — sequencing (see §10) assumes format→profiles/includes→
  config→scoping→org. Confirm before implementation.

## 10. Sequencing (tasks linked to the project)

Foundational (parallelizable): **manifest format v2** (§3), **project-scope
targets** (§6.2), **global config loader** (§7), **native-support
investigation** (§6.1, largely done — close with the stored memory).

Then: **profiles** (§4, needs format v2), **composition/includes** (§5, needs
format v2).

Then: **directory-prefix routing + zero-arg apply** (§6.3/§7.2, needs global
config + project-scope targets + profiles), **org-scoped apply** (§8, needs
global config + zero-arg resolution).

Finally: **precedence/layering docs + README + integration tests** (needs the
feature set landed).
