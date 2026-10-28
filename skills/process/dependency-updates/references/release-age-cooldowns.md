# Release-age cooldowns and emergency bumps

A **release-age cooldown** refuses to install any package version published more recently
than some window. It is a supply-chain control: most malicious releases are caught and
yanked within hours, so a few days of delay filters out smash-and-grab compromises.

It also blocks the one update you most need to apply fast. When a zero-day lands, the fix
is by definition a brand-new release, and the cooldown treats it exactly like an attack.
This file covers recognizing that situation, deciding whether you are allowed to override,
and overriding in the narrowest way that survives the next maintenance run.

---

## Recognizing a cooldown

Whether a cooldown announces itself depends on whether anything *forces* the new version.

**Nothing forcing it — silent.** The resolver reports the new version as *missing*, not as
*withheld*, and the command succeeds with no change at all. uv's documentation describes this
case:

> unsatisfiable resolutions will not mention that distributions were excluded due to the
> `--exclude-newer` flag — newer distributions will be treated as if they do not exist.

**Something forcing it — loud, and current uv is better than that doc text suggests.** Give
uv a floor it cannot satisfy and it fails with an explicit hint:

```text
hint: `certifi` was filtered by `exclude-newer` to only include packages uploaded before
2026-10-16T15:11:16Z. The latest version satisfying the requirement is v2026.10.23, published
at 2026-10-23T11:36:14Z. Consider using `exclude-newer-package` to override the cutoff.
```

That is a strong argument for declaring a floor (below): it converts the silent mode into a
self-explaining one. Yarn gives no comparable hint — a blocked resolution there fails without
naming the age gate.

So in the silent case the symptom is never "cooldown". It is one of these:

- `uv lock --upgrade-package X` reports no change, though the registry has a newer version
- resolution fails with "no solution found" naming a version you can see on PyPI
- a Renovate security PR is open, but its lockfile step produced no version change
- CI resolves a different version than your laptop did yesterday

When you see any of those, check for a window **before** debugging anything else.

```bash
# Python
grep -n 'exclude-newer' pyproject.toml uv.lock

# JavaScript — config first, then the bundled binary's own default,
# because Yarn's gate can be active with no config present at all
grep -nE 'npmMinimalAgeGate|npmPreapprovedPackages|min-release-age' .yarnrc.yml .npmrc 2>/dev/null
grep -o 'npmMinimalAgeGate:{[^}]*' .yarn/releases/yarn-*.cjs

# PR-level (Renovate) — check the preset it extends, not just the local file
grep -nE 'minimumReleaseAge|lockFileMaintenance' renovate.json*
```

---

## The gate — when you may override

Overriding reintroduces exactly the exposure the window exists to prevent. Do it only when
one of these holds, and **name it in the PR body**:

1. A GHSA/CVE/OSV ID affects the version **currently in the lockfile**, and the fix is in
   the new release.
2. A documented compromise of the existing version's trust chain — a CA key leak, a
   maintainer account takeover, a yanked release shipping malware.

"It is newer", "CI is red", "Renovate opened a PR for it", and "the pin looks stale" are
not grounds. Wait out the window.

If the trigger is a published advisory, the sibling `renovate-security-triage` skill ranks
advisories by real severity, CVSS and EPSS. Its tier 0 (`critical`) is precisely the case
that will hit a cooldown on the way in.

---

## uv

### Settings

| Setting | Scope | Value |
|---------|-------|-------|
| `[tool.uv] exclude-newer` | every package | RFC 3339 timestamp, or a duration such as `7 days` / `P7D` |
| `[tool.uv.exclude-newer-package]` | one package | same value forms, or `false` to exempt entirely |

Package-specific values take precedence over global ones. uv also accepts a compact `7d` /
`0d` spelling, which is what the mitodl repos use, though the documented examples spell it
out; either parses.

The resolved timestamp is written into `uv.lock` under `[options]` and
`[options.exclude-newer-package]`, and is only recomputed when a new resolution runs — a
passing clock does not invalidate a lockfile.

### The two override mechanisms

**Scoped CLI flag — no `pyproject.toml` diff:**

```bash
uv lock --upgrade-package <pkg> --exclude-newer-package <pkg>=0d
```

`--exclude-newer-package` exists on `uv lock`, `uv sync`, `uv add`, and `uv pip compile`.
Unlike `--exclude-newer` (which binds `UV_EXCLUDE_NEWER`), it has **no environment variable
on any subcommand** — it cannot be set ambiently in CI.

The resulting lockfile still passes `uv lock --check` with nothing added to
`pyproject.toml`: uv records the override in the lock, and a recorded value *looser* than
what `pyproject.toml` demands does not invalidate it.

**Declarative entry — survives a full re-resolution:**

```toml
[tool.uv.exclude-newer-package]
# CVE-2026-XXXXX: 2.1.4 is the fix and is inside the 7-day window.
# Remove this line after 2026-11-05.
somepkg = "0d"
```

### Choosing between them

The CLI-only form lives **only in the lockfile**, so the next full re-resolution drops it
and silently downgrades the package:

```text
$ uv lock --upgrade
Updated certifi v2026.10.23 -> v2026.7.22
```

Renovate's `lockFileMaintenance` runs exactly `uv lock --upgrade` for uv projects, on a
schedule. So:

```text
revert risk = (next full `uv lock --upgrade`) < (release upload date + global cooldown)
```

- **No revert risk** — the release ages out of the window before the next maintenance run.
  Use the CLI flag; keep `pyproject.toml` clean.
- **Revert risk** — use the declarative entry with a dated removal comment.

Under the common mitodl configuration (uv `7d`, `lockFileMaintenance` weekly) a weekly run
nearly always lands inside a 7-day window, so **the declarative entry is usually the
correct answer**. Treat the CLI-only form as the narrow case, not the default.

### Command disambiguation

Three similar-looking checks mean different things:

| Command | Meaning |
|---------|---------|
| `uv lock --check` | the lockfile is up to date (hidden alias `--locked`, which is what carries `UV_LOCKED`) |
| `uv lock --check-exists` | a `uv.lock` merely exists (alias `--frozen`, `UV_FROZEN`) |
| `uv sync --check` | the **environment** matches the lockfile — not a lockfile check at all |

---

## JavaScript

Every major JS package manager now has a cooldown, but **the scoped escape hatch is not
universally reachable from the command line**. This is where the uv pattern stops
transferring.

| Manager | Cooldown setting | Per-package escape | Scoped override with no file diff |
|---------|------------------|--------------------|-----------------------------------|
| npm | `min-release-age` (days); also `before` (date) | `min-release-age-exclude` — names or minimatch globs | **Yes** — CLI flag, or `npm_config_min_release_age_exclude` |
| Yarn Berry 4.10+ | `npmMinimalAgeGate` — type `DURATION`, unit minutes | `npmPreapprovedPackages` — descriptors or globs | **No** |
| pnpm 10.16+ | `minimumReleaseAge` — minutes, default `1440` from v11 | `minimumReleaseAgeExclude` | via config only |

**The three managers do not share a unit.** npm counts **days** (`hint: '<days>'`; its
flatten computes `Date.now() - 86400000 * age`), while Yarn and pnpm count **minutes**. A
value copied between them is wrong by three orders of magnitude in whichever direction
hurts — `min-release-age=7` is a week of soak, `minimumReleaseAge: 7` is seven minutes.
Read the unit out of the manager you are configuring before reusing a number.

### npm

The exclusion is an ordinary npm config, so it works as a flag and the uv pattern carries
over directly:

```bash
npm install <pkg>@<fixed> --min-release-age-exclude=<pkg>
```

**Name the version.** The flag only makes the fresh release *eligible*; it does not request
an upgrade. An argument-free `npm install` reifies the existing `package-lock.json` and
keeps any locked version that still satisfies the manifest — so the command succeeds with
the vulnerable version still installed. For a transitive dependency, declare the
`overrides` floor first (see [Pin the floor](#pin-the-floor-not-just-the-lockfile)) and
then install with the flag attached.

`min-release-age-exclude` exempts a package from **both** `min-release-age` and `before`.
npm's own config definition for `before` says so explicitly: "Packages whose names match
`min-release-age-exclude` are exempt from this filter."

### Yarn — no safe CLI-only form

Yarn settings map to `YARN_<UPPER_SNAKE_CASE>` environment variables, but the documentation
states that "arrays and objects aren't supported yet" — and `npmPreapprovedPackages` is an
array. The only lever reachable without editing a file is:

```bash
YARN_NPM_MINIMAL_AGE_GATE=0 yarn install   # DO NOT DO THIS
```

That is **global**. It drops the gate for every package in the install, which is the same
mistake as a global `--exclude-newer`, and it fast-tracks whatever else happens to be fresh
in the tree at that moment. On Yarn, edit `.yarnrc.yml` instead — the declarative route is
the only scoped one:

```yaml
npmPreapprovedPackages:
  # CVE-2026-XXXXX: 2.1.4 is the fix and is inside the gate window.
  # Remove this entry after 2026-11-05.
  - "somepkg@2.1.4"
```

**Use a versioned descriptor, not a bare name.** Yarn parses each entry with
`tryParseDescriptor` and, when the range comes back `unknown` — which is what a bare
`somepkg` yields — the entry matches *every* version of that package, forever. A range
(`somepkg@2.1.4`, `somepkg@^2.1.4`) is tested against the candidate version, so the
exemption expires on its own once the tree moves past it. Verified by reading
`.yarn/releases/yarn-4.12.0.cjs`; the bare-name case is the `range === "unknown"` early
`return true`.

### Read the Yarn default from the binary, not the docs

The gate can be active with no configuration present, because its default varies by Yarn
version — and the published docs have been wrong about it. Check the bundled release:

```bash
grep -o 'npmMinimalAgeGate:{[^}]*' .yarn/releases/yarn-*.cjs
```

Observed defaults: `0m` in 4.12.0 and 4.13.0 (off), `1d` in 4.17.0 (on), and the setting
does not exist at all in Yarn 3.x. Yarn additionally applies a compatibility selector keyed
on the yarnrc version (`< 10` forces the gate to `0`), so the binary default is not the
final word — verify against the project rather than assuming.

The practical consequence: **a routine Yarn upgrade can switch a cooldown on**, with no
config change for anyone to review, and the first symptom is an install that silently
cannot find a version.

---

## PR-level cooldowns: Renovate and Dependabot

These are a separate layer. They delay the *pull request*, not the resolution, and they
stack with the package-manager cooldown above.

**Renovate** — `minimumReleaseAge`. Its `vulnerabilityAlerts` configuration defaults to
`minimumReleaseAge: null`, `schedule: []`, and `prCreation: 'immediate'`, so security PRs
skip Renovate's own cooldown and "skip the line" past concurrency and schedule limits.

**That exemption stops at Renovate's boundary.** The artifact-update step still shells out
to the package manager, which enforces its own window unconditionally — uv has no concept
of a vulnerability. A Renovate security PR for a two-day-old fix will open, run `uv lock`,
and come back with no version change. **This gap is the main reason this file exists.**

Two Renovate details worth having right:

- `lockFileMaintenance` runs `uv lock --upgrade` for uv projects; a targeted update runs
  `uv lock --upgrade-package <dep>`. Renovate's generic documentation says lockFileMaintenance
  deletes the lock file first — that is **not** what the uv processor does; it updates in place.
- The default schedule is weekly; a shared org preset may override it.
- `osvVulnerabilityAlerts` queries OSV directly rather than going through GitHub, but its
  documentation is explicit that it only covers **direct** dependencies. Since most CVEs land
  in transitive ones, it will not fire for the common case; the Dependabot-backed
  `vulnerabilityAlerts` is what covers transitive, and only where Dependabot alerts are
  enabled on the repo.

**Dependabot** — `cooldown`, with an implicit 3-day default and `include` / `exclude` lists.
Documented as applying to **version updates only, not security updates**, so Dependabot's
security path is exempt by design.

---

## Pin the floor, not just the lockfile

A lockfile bump says "today we resolved to the fixed version". It does not say "the
vulnerable version is forbidden". Any future re-resolution can undo it, and for a *transitive*
dependency — which is what most CVEs land in — nothing in the manifest objects.

So alongside the bump, declare a minimum. The vulnerable version then becomes unreachable
rather than merely unselected, and a regression becomes a failed resolution instead of a quiet
downgrade nobody reviews.

| Ecosystem | Direct dependency | Transitive dependency | Semantics |
|-----------|-------------------|------------------------|-----------|
| uv / Python | raise the range in `[project.dependencies]` | `[tool.uv] constraint-dependencies` | **constrains** — narrows a package already in the graph, never adds one |
| npm | raise the range in `dependencies` | `overrides` | **forces** — accepts any npm specifier, including a `>=` range |
| Yarn Berry | raise the range in `dependencies` | `resolutions` | **forces** — documented with exact versions; supports `parent/child` scoping |
| pnpm | raise the range in `dependencies` | `overrides` | **forces** |

```toml
# uv — certifi is transitive, so constrain rather than adding a dependency on it
[tool.uv]
constraint-dependencies = ["certifi>=2026.10.19"]
```

```json
{ "overrides": { "<pkg>": ">=<fixed>" } }
```

npm accepts a range there, so express a floor rather than pinning an exact version; Yarn's
`resolutions` is documented with exact versions, which is a pin you will have to revisit.

Know which of the two semantics you are getting. uv's `constraint-dependencies` is a genuine
floor: resolution stays free above it and **fails loudly** if nothing satisfies it. npm
`overrides`, Yarn `resolutions`, and pnpm `overrides` are *forcing* — closer to uv's
`override-dependencies` than to its constraints. They will happily install a version a parent
package declares incompatible, so they can mask a real conflict and they go stale. Prefer a
range over an exact pin where the tool allows one, and revisit them.

Do not reach for uv's `override-dependencies` to raise a floor. It replaces what other
packages declare regardless of validity; `constraint-dependencies` expresses "at least this"
without overriding anyone's legitimate pin.

**The transitive column above is complete only for a repo that publishes nothing.** None of
those four mechanisms appear in a built artifact's metadata: `constraint-dependencies` is
absent from `requires-dist`, and npm `overrides` / Yarn `resolutions` / pnpm `overrides` are
consumer-side directives a dependent never sees. They protect your resolution and nobody
else's. If the repo publishes packages, the floor also has to be declared in
`[project] dependencies` (or `dependencies` in `package.json`) of the packages that need it,
and those packages released. See
[dependency-floors.md](dependency-floors.md), which also covers keeping a narrowly-declared
floor correct as the graph changes.

### How the floor interacts with the cooldown

During the overlap you need **both**, because the floor demands a version the window hides:

```toml
[tool.uv]
constraint-dependencies = ["certifi>=2026.10.19"]   # permanent

[tool.uv.exclude-newer-package]
# Remove after 2026-10-26; the constraint above stays.
certifi = "0d"                                      # temporary
```

Remove the dated cooldown entry once the window clears, and **keep the constraint**. Removing
the constraint instead is the mistake: it is the part that prevents the vulnerable version
from coming back.

Verified behavior with a floor in place and the cooldown entry removed before the window
cleared: `uv lock --upgrade` **fails** with the `exclude-newer` hint rather than downgrading.
Without the floor, the same command silently reported
`Updated certifi v2026.10.23 -> v2026.7.22` and exited successfully.

The constraint is recorded in `uv.lock` under `[manifest] constraints`, and `uv lock --check`
passes with it present:

```toml
constraints = [{ name = "certifi", specifier = ">=2026.10.19" }]
```

### When to retire a floor

A floor is cheap to keep and costly to drop early, so the default is to leave it. Retire it
only when the whole supported range is above it anyway — for example once the lowest version
any dependency resolves to already exceeds the floor — and never merely because the cooldown
override next to it expired.

---

## Verify the bump survives

An override that the next maintenance run undoes is worse than no fix, because nothing
reports the regression. Prove it:

```bash
uv lock --upgrade --dry-run >relock.log 2>&1; rc=$?
cat relock.log
[ "$rc" -eq 0 ] || echo 'RESOLUTION FAILED — not a pass; read the log above'
grep -iE '^Updated? <pkg> ' relock.log    # only meaningful when rc was 0
```

**Check uv's exit status before you read the grep.** Piping straight into `grep` conflates
two opposite outcomes: with the floor in place but no effective exemption, uv *errors* and
prints no `Update` line, so a naive `uv lock ... | grep` produces the same empty output and
the same non-zero pipeline status as a clean unchanged resolution. The bump only survived if
uv exited `0` **and** the grep printed nothing.

`--dry-run` writes no lockfile. Match both tenses: `--dry-run` prints `Update pkg vA -> vB`,
while a real run prints `Updated`.

Then confirm the version that is actually installed, not just locked:

```bash
# importlib.metadata works for any distribution; `__version__` is not universal
uv run python -c "import importlib.metadata as m; print(m.version('<pkg>'))"
uv lock --check
```

`uv lock --check` is not a substitute for the re-resolution above — it only asserts that the
lockfile on disk is consistent with `pyproject.toml`, which an exemption-bearing lockfile
satisfies happily.

---

## Audit the release you fast-tracked

Bypassing the window means skipping the soak period that would normally have caught a
malicious release. Replace it with direct inspection:

- the upstream tag or GitHub release exists and matches the published artifact
- the diff does what the advisory says, and nothing else
- publisher attestations / provenance are present where the registry supports them
- the maintainer set did not change immediately before this release
- the release is not the project's first in a long dormancy — a classic takeover signature

---

## Sweep expired overrides

A dated override is debt, and it does not remove itself. One real example: an
`ol-infrastructure` entry commented "remove this line after 2026-10-13" was still in
`pyproject.toml` ten days later.

Whenever you touch a repo's dependencies, check for overrides that have outlived their
reason:

```bash
# uv — print the whole table, through to the next TOML section
awk '/^\[tool\.uv\.exclude-newer-package\]/{f=1} f&&/^\[/&&!/exclude-newer-package/{f=0} f' pyproject.toml

# Yarn — every preapproved descriptor, with its dated comment
awk '/^npmPreapprovedPackages:/{f=1;print;next} f&&/^[^[:space:]#-]/{f=0} f' .yarnrc.yml

# npm / pnpm
grep -n 'min-release-age-exclude\|minimumReleaseAgeExclude' .npmrc package.json pnpm-workspace.yaml 2>/dev/null
```

A fixed-count `grep -A2` is not enough: the entries above carry a two-line dated comment,
so the package line itself falls outside the window and any second entry is invisible. Read
the whole table.

**Deleting the line is not enough.** uv also records the override in `uv.lock` under
`[options.exclude-newer-package]`, and a plain `uv lock` will not clear it — the resolution
is already valid, so uv does nothing and `uv lock --check` keeps passing with the exemption
still live. Force a re-resolution of that one package:

```bash
uv lock --upgrade-package <pkg>
grep '<pkg> = {' uv.lock          # must print nothing
```

Then confirm the package holds its version on its own, and that the lockfile's `[[package]]`
section is otherwise unchanged. Use the same uv version the lockfile was written with — an
older uv silently rewrites `revision` in the lock header and turns a one-line cleanup into a
large spurious diff.

The same applies on the JS side, and the removal is not uv-specific: a stale
`npmPreapprovedPackages` or `minimumReleaseAgeExclude` entry keeps the soak gate off for
that package indefinitely. Delete the entry **and** regenerate the lockfile, or the
fast-tracked version simply stays resolved:

```bash
yarn install --mode=update-lockfile    # Yarn Berry
npm install --package-lock-only        # npm
pnpm install --lockfile-only           # pnpm
```

Leave permanent first-party exemptions alone — entries covering an
organization's own packages (`mitol-django-*`, `ol-openedx-*`) are deliberate and carry no
removal date, because a cooldown on a package you just published serves no purpose.

---

## Resolve the real configuration, not a stale copy

A repo's `renovate.json` usually just extends a shared org preset, and a local checkout of
that preset can be out of date. Fetch the live version:

```bash
gh api repos/<org>/.github/contents/renovate-config.json --jq '.content' | base64 -d
```

A local checkout of one shared preset was missing `minimumReleaseAge`,
`vulnerabilityAlerts`, and `osvVulnerabilityAlerts` that the live copy sets — reading it
would have given exactly the wrong answer about whether a cooldown applies.

One investigation caveat, since every question here is answered by searching for a config
that may not exist: **do not suppress stderr while proving absence.** A command such as
`fd ... 2>/dev/null` prints exactly nothing whether it found no matches or was never
installed, and "no cooldown is configured" is the wrong conclusion to reach from a missing
binary. Drop the redirect, or confirm with a second tool, before reporting that a setting
is absent.
