# Keeping a security floor where it actually holds

A floor stops a vulnerable version being resolved again. Setting one is easy;
keeping it correct as the dependency graph moves underneath you is not, and it
fails quietly. This file covers the two ways a floor silently stops working and
the tool that catches both.

Read [release-age-cooldowns.md](release-age-cooldowns.md) first for *why* you
are setting a floor and how it interacts with a resolver cooldown. This file is
about whether the floor you set is the floor your consumers get.

---

## A constraint protects your lockfile; a dependency protects your consumers

This distinction decides whether a floor is worth anything, and the two look
identical in a diff.

| | `[tool.uv] constraint-dependencies` | `[project] dependencies` |
|---|---|---|
| Steers this repo's resolution | yes | yes |
| Appears in a built wheel's metadata | **never** | yes |
| Reaches someone who installs your package | **no** | yes |
| Adds the package if nothing else pulls it in | no | yes |

`constraint-dependencies` is a resolver directive. It is not part of
`requires-dist`, so it is absent from the sdist and the wheel. A library that
carries only a constraint publishes a package whose consumer can resolve the
vulnerable version straight back in — while the library's own CI stays green,
because its lockfile is correct.

**Which one is right depends on whether the repo publishes anything.**

- **An application** — a service, a site, a deployed job — publishes no
  metadata, so it has nothing to propagate. A constraint is the correct and
  complete answer. Adding a direct dependency on a package the application
  never imports just creates dead weight the day it stops being transitive.
- **A library, or a monorepo of them** — the floor has to be declared in
  `[project] dependencies` of the packages that need it, *and those packages
  have to be released*. A declared floor in an unreleased package reaches
  nobody.

Both at once is fine and often right: the constraint guards your own
re-resolution, the declaration guards everyone else's.

> Worked example: an org-wide certifi remediation found 15 of 20 repos had
> expressed the floor as `constraint-dependencies` alone. Every application
> among them was correct. Every library among them had published packages that
> propagated nothing.

---

## The second failure: a narrow floor going stale

Declaring the floor only where it is needed is right — a floor in a package
that never resolves the dependency forces it on consumers who have no use for
it. But "where it is needed" is a fact about the dependency graph, and the
graph moves:

- A package **gains** the floored dependency indirectly, because something it
  already depends on added it. It now needs a floor it never had.
- A package **loses** it, and carries a dependency it no longer justifies.
- A package reaches it only through a sibling in the same monorepo, and
  declares that sibling **unpinned** — so an older release of the sibling,
  published before the floor existed, satisfies the requirement and the floor
  does not hold at all.

None of these are visible to a vulnerability scanner. Once the floor and the
cooldown are in place the dependency always resolves *safe* in your lockfile,
so every scanner stays green while the published metadata is wrong. The
lockfile is a dev artifact; what ships is `requires-dist`.

---

## `mitol-dependency-floors`

Checks that a repo's manifests and lockfile enforce the floors it declares.
It is not a vulnerability scanner and does not replace one — use osv-scanner,
pip-audit or `uv pip list --outdated` to learn *that* you need a floor, and
this to make sure the floor sticks.

```bash
# exit 0 clean, 1 violations, 2 bad config
uvx mitol-dependency-floors check
uvx mitol-dependency-floors explain certifi
```

Installed, the command is also available as `dependency-floors`; use the short
form in CI and hooks.

`check` exits non-zero on violations, so it works as a CI step or a pre-commit
hook on any platform. `explain` prints each package's classification and the
dependency path behind it, which is how you answer "why does this one need a
floor and that one doesn't" without reconstructing the graph by hand.

### Configuration

All of it lives in `[tool.dependency-floors]` in the repo's own
`pyproject.toml`.

```toml
[tool.dependency-floors.options]
lock = "uv.lock"                 # default
members = "src/*"                # omit for an application; see below
exclude = ["src/testonly"]

[tool.dependency-floors.floors.certifi]
specifier = ">=2026.10.19"
reason = "eMudhra CA compromise"
advisory = "https://github.com/advisories/GHSA-6q48-6c7w-9gw2"
```

`specifier` has to place a lower bound; a floor that bounds nothing is a
configuration error rather than a rule that always passes.

### What it checks

**Any repo** — no workspace, nothing published:

| Check | |
|---|---|
| `lock-below-floor` | the lockfile resolved below a floor |
| `constraint-below-floor` | a resolver constraint drifted weaker than the floor it guards |

`lock-below-floor` is the guard against the cooldown problem the other
reference documents: a resolver cooldown applies to every package with no
carve-out for security releases, so an upgrade run can walk the lockfile back
under a floor. With this check a silent downgrade becomes a red build.

Note what is deliberately *not* checked here: it never demands a declared
dependency in an application, because an application publishes nothing.

**A repo that publishes packages** — set `members` to a glob of package
directories, or `"."` for a single-package repo. For each package, and for the
base install and each extra separately:

| Check | |
|---|---|
| `floor-not-declared` | the package reaches the floored dependency through its own non-workspace dependencies, so it must declare the floor itself |
| `inherited-floor-unpinned` | it reaches it only through a sibling, so it must require that sibling at a version carrying the floor |
| `floor-declared-redundantly` | it declares a floor it already inherits |
| `floor-not-needed` | nothing in its graph reaches the dependency, so the declaration should go |

```toml
[tool.dependency-floors.floors.certifi.workspace]
policy = "entry-point"
providers = { my-core-package = "2026.10.28" }
```

`providers` records the first released version of a sibling that declares the
floor. Recording it is what makes inheritance sound — an unpinned sibling
dependency can be satisfied by a release that predates the floor.

Set `policy = "closure"` to drop the distinction entirely: every package whose
graph reaches the dependency declares the floor directly, and `providers` is
unused. Simpler to explain, at the cost of a declaration in packages that would
have inherited one.

### The one escape hatch worth knowing

A requirement on the floored package *itself* is excluded when deciding
reachability, so that declaring a floor is not what makes the floor look
reachable. A package that genuinely **imports** the dependency therefore
reaches it through nothing else, and `floor-not-needed` would tell it to drop a
declaration it needs. List those:

```toml
[tool.dependency-floors.floors.certifi]
specifier = ">=2026.10.19"
direct = ["my-tls-helper"]
```

They are then held to the floor without the reachability test.

### How reachability is decided, and where it is wrong

The lockfile is the source of truth. `dependencies` and every
`optional-dependencies` group are edges; `dev-dependencies` are not, since they
describe what the repo needs to test itself rather than what a consumer
resolves.

Three decisions deliberately **over**-report, because a floor declared where it
was not needed is cheaper than one silently missing: environment markers are
ignored, repeated lock entries union their edges, and a requirement absent from
the lock reads as unreachable.

Run it where the lock is known current — behind `uv lock --check` or
equivalent — or it describes a resolution that no longer exists. `uv.lock` is
the only lockfile format read today.

---

## Where this fits in a security bump

1. Bump the lockfile, overriding the cooldown if the gate in
   [release-age-cooldowns.md](release-age-cooldowns.md) is met.
2. Set the floor. Constraint for an application; declared dependency in the
   packages that need it for a library.
3. **Release the packages carrying a newly declared floor.** This step is easy
   to skip and makes the previous one worthless.
4. Add the floor to `[tool.dependency-floors]` and wire `check` into CI, so the
   next graph change that invalidates the decision fails loudly.
