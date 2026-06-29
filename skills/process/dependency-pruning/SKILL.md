---
name: dependency-pruning
description: >
  Audit a repository's dependencies to identify those that are unused (safe to
  remove), underused (few enough features used to vendor or rewrite), imported
  inefficiently (blocking tree-shaking), or deprecated/sunset (need migration).
  Use this skill when the user wants to slim down their dependency tree, reduce
  supply chain risk, remove dead weight from package manifests, identify
  libraries that could be replaced with a few lines of code, or do a general
  dependency health check. Covers Python, JavaScript/TypeScript, Go, Rust, and
  other ecosystems. Invoke whenever the user mentions "unused dependencies",
  "dependency audit", "remove packages", "we don't need this library", "too
  many dependencies", "could we vendor this", "could we just write this
  ourselves", "bundle size", "tree shaking", or any variation of slimming down
  or cleaning up external dependencies.
license: BSD-3-Clause
metadata:
  category: process
---

# Dependency Pruning

## Goal

Produce an actionable report categorizing dependencies into: **remove**,
**optimize import style**, **vendor/rewrite**, **migrate away from**, and
**keep**. Then offer to execute the safe changes.

## Configurable thresholds (defaults)

- **Max imported symbols for vendor candidate**: 3 unique symbols from the package
- **Max package LOC proxy for vendor candidate**: 500 total source lines

Mention these defaults briefly at the start and ask if the user wants to
override them — but don't block; proceed with defaults if they don't respond.

---

## Phase 1 — Detect ecosystems

Scan the repo root and subdirectories for manifest files:

| Ecosystem | Indicator files |
|-----------|----------------|
| Python | `pyproject.toml`, `requirements*.txt`, `setup.py`, `setup.cfg`, `Pipfile` |
| JS/TS | `package.json` |
| Go | `go.mod` |
| Rust | `Cargo.toml` |

For other ecosystems, look for any manifest that lists dependencies (Gemfile,
pom.xml, build.gradle, pubspec.yaml, mix.exs, etc.) and adapt the approach
below to that format.

Announce which ecosystems were found before proceeding. In monorepos, enumerate
all manifest files.

---

## Phase 2 — Find unused dependencies

See `references/unused-detection.md` for the full per-ecosystem commands.

**Python** — run `uvx deptry .` (no install needed). Focus on `DEP001`
(declared but never imported). If deptry is unavailable, fall back to grepping
for each dependency name across `*.py` files.

**JS/TS** — run `npx --yes depcheck --json`. Unused runtime deps appear under
`dependencies`, unused dev deps under `devDependencies`. `knip` is a good
alternative for monorepos: `npx --yes knip --reporter json`.

**Go** — run `go mod tidy -v` in a temporary copy of go.mod/go.sum; diff the
result to see what disappears. Do not mutate the real files yet.

**Rust** — run `cargo machete` (installs itself if needed via `cargo install
cargo-machete --quiet`).

**Other** — enumerate declared deps from the manifest, then grep the source
tree for each package name to find those never referenced.

### Go beyond the tool output

Automated tools miss things. After the tool run, scan the full declared dep
list yourself and verify packages the tool didn't flag — especially:
- Packages whose PyPI/npm name differs from the import name
- Packages that are very old or rarely heard of
- Packages that duplicate stdlib functionality

### Known tool blind spots

**Django / Python projects**: deptry's **DEP001** false-positive rate can be
very high (sometimes 30+ flags for a single project) because PyPI package names
rarely match their Python module names:
- `djangorestframework` → `rest_framework`
- `beautifulsoup4` → `bs4`
- `pyyaml` → `yaml`
- `pygithub` → `github`
- `psycopg2-binary` → `psycopg2`

When deptry can't find `import djangorestframework` anywhere, it raises DEP001
("declared but never imported") — but the package IS in use, just as
`from rest_framework import ...`. The paired DEP002 ("import found but not
declared") may also fire for the Python module name that is imported, since the
manifest lists the PyPI name. Both errors stem from the same root cause.
Verify each DEP001 manually before treating any as a removal candidate. After
the audit, suggest adding a `[tool.deptry.package_module_name_map]` section to
`pyproject.toml` so future runs are accurate.

**Django INSTALLED_APPS**: packages registered as Django apps (django-anymail,
django-storages, django-guardian, etc.) are loaded by the framework from
strings in `INSTALLED_APPS`, not via Python `import` statements. Static
analysis will always miss these. Always check `settings.py` before flagging
a `django-*` package as unused.

**Server runtime packages**: WSGI/ASGI servers (gunicorn, uwsgi, granian,
hypercorn, uvicorn) are invoked via CLI in Dockerfile or Kubernetes manifests,
not imported in Python. Before flagging any of these for removal:
1. Check deployment configs (`Dockerfile`, `docker-compose.yml`, Helm charts, `Procfile`)
2. Check git history for in-progress migrations (e.g., uwsgi→granian): if a pending
   PR or recent commit is switching servers, both the old and new runtime belong in
   the manifest until the migration lands. Flag as "keep — migration in progress"
   rather than a removal candidate.

**Developer tooling (CLI-invoked)**: Debuggers, REPLs, and profilers — such as
`ipdb`, `pdbpp`, `bpython`, `ptpython`, `debugpy`, `pudb`, `py-spy`,
`memory-profiler` — are invoked from the terminal, not imported in application
code. Static analysis will always flag them as unused. These belong in
dev dependencies (not main), but should generally be kept rather than removed
if they're clearly intended as team-wide developer conveniences. Flag as
"dev tooling — classify as dev dep if in main" rather than "remove".

**Webpack/babel loaders and plugins**: These are referenced in
`webpack.config.js`, not imported in source code. Don't flag webpack plugins
as unused based on source-code search alone.

### Handling test-only usage

If a package is only imported in test files (`tests/`, `spec/`, `*_test.go`,
`*_spec.rs`, etc.), classify it as a dev-only dep, not as fully unused — the
fix is moving it to dev dependencies, not removing it outright.

### Dynamic and conditional imports

Flag packages that are loaded dynamically (`importlib`, `require()` with a
variable, `dlopen`) or conditionally (`try: import X except ImportError:`) as
"possibly used — verify manually" rather than unused.

---

## Phase 3 — Analyze API surface for vendoring candidates

For each dependency that IS used, check how much of it is actually called.

```bash
# Python: unique symbols imported from a package
PKG="humanize"
rg "from ${PKG}(?:\.\w+)? import (\w+)" -g "*.py" -o -r '$1' --no-filename | sort -u

# JS/TS: named imports + member accesses
PKG="lodash"
rg "import \{([^}]+)\} from ['\"]${PKG}['\"]" --no-filename --include="*.{ts,tsx,js,jsx}"
rg "${PKG}\.\w+" --no-filename --include="*.{ts,tsx,js,jsx}" -o | sort -u

# Go: symbols accessed after package alias
PKG="github.com/pkg/errors"
rg "\"${PKG}\"" --include="*.go" -l

# Rust: use paths from the crate
CRATE="serde"
rg "use ${CRATE}::" --include="*.rs" -o | sort -u
```

Count unique symbols. If the count is at or below the threshold (default: 3),
flag the package as a vendoring candidate. Include a one-sentence sketch of the
replacement (e.g., "The 2 functions could be replaced with ~25 lines of Python").

Also estimate package size:
```bash
# Python
python -c "
import importlib.util, pathlib
spec = importlib.util.find_spec('${PKG}')
if spec and spec.origin:
    origin = pathlib.Path(spec.origin)
    if origin.name == '__init__.py':
        root = origin.parent
        lines = sum(len(f.read_text(errors='ignore').splitlines()) for f in root.rglob('*.py'))
    else:
        lines = len(origin.read_text(errors='ignore').splitlines())
    print(lines)
"
# JS/TS
du -sk node_modules/${PKG} 2>/dev/null
```

---

## Phase 3b — Import style and bundling analysis (JS/TS only)

For each JS/TS package that passes the "keep" threshold (too many symbols to
vendor), check whether its import style is preventing tree-shaking:

```bash
PKG="lodash"
# Default/namespace imports that load the whole package
rg "import _ from ['\"]${PKG}['\"]" --include="*.{ts,tsx,js,jsx}"
rg "import \* as .+ from ['\"]${PKG}['\"]" --include="*.{ts,tsx,js,jsx}"
rg "const .+ = require\(['\"]${PKG}['\"]" --include="*.{ts,tsx,js,jsx}"

# Named imports (tree-shakeable IF the package ships ESM)
rg "import \{" --include="*.{ts,tsx,js,jsx}" | grep "${PKG}"

# Check if the package ships an ESM build
ls node_modules/${PKG}/esm 2>/dev/null || cat node_modules/${PKG}/package.json | grep -E '"module"|"exports"' | head -5
```

If the package is imported via default/namespace import but ships an ESM
alternative (e.g., `lodash-es`, per-function path imports like
`lodash/debounce`), flag it as an **import style optimization** opportunity —
not a removal candidate, but potentially a large bundle-size win.

---

## Phase 4 — Check for deprecated or sunset packages

After the unused and vendoring analysis, scan for packages that are still
in active use but should be migrated away from:

- **Deprecated by maintainer**: check npm/PyPI for deprecation notices
- **Abandoned**: check for packages with no commits in 2+ years and open issues
- **Sunset by platform**: e.g., Google Analytics UA (react-ga, analytics.js)
  was sunset in July 2023; older React component libraries with known
  React 18 incompatibilities (react-hot-loader, etc.)
- **Superseded by stdlib**: e.g., `more-itertools.batched` → `itertools.batched`
  (Python 3.12+), `moment` → `Temporal` or `date-fns`, `request` → `fetch`

For each, note what the migration target is and roughly how large the change
would be. Do not conflate "deprecated" with "remove" — flag these as
"should migrate" rather than immediately removable.

---

## Phase 5 — Report

Present a structured report:

```
# Dependency Audit: <repo-name>
Thresholds: API surface <= N symbols, package LOC proxy <= N

## Summary
- Ecosystems: <list>
- Total direct dependencies: N
  - Remove (unused): N
  - Optimize import style: N
  - Vendor/rewrite candidates: N
  - Migrate away from (deprecated/sunset): N
  - Dev-only misclassified (in main, should be dev): N
  - CLI-invoked dev tooling (keep, move to dev if needed): N
  - Well-used: N

## Remove — Unused Dependencies
| Package | Ecosystem | Evidence of non-use |
|---------|-----------|---------------------|
| ddt     | Python    | No `import ddt` or `from ddt` in any test file |

## Optimize Import Style (JS/TS)
| Package | Current import | Issue | Fix |
|---------|----------------|-------|-----|
| lodash  | `import _ from 'lodash'` | Prevents tree-shaking; full ~72KB ships | Switch to `lodash-es` or per-function imports |

## Vendor/Rewrite Candidates
| Package | Used symbols | Package LOC | Replacement sketch |
|---------|-------------|-------------|-------------------|
| waait   | default (1)  | 1 LOC       | `const wait = (ms=0) => new Promise(r => setTimeout(r, ms))` |

## Migrate Away From
| Package | Status | Migration target |
|---------|--------|-----------------|
| react-ga | GA3 sunset Jul 2023 | PostHog (already wired), or GA4 via gtag |

## Dev-only Misclassifications
| Package | Currently | Should be |
|---------|-----------|-----------|
| ipython | dependencies | dev dependencies |

## Well-used (checked, nothing to do)
<simple list — just confirms these were inspected>
```

---

## Phase 6 — Offer to apply changes

After the report, present the user with options:

1. **Report only** — done; they act on it manually.
2. **Remove unused deps** — show dry-run list and confirm, then execute.
3. **Remove unused + optimize imports** — remove unused, then apply the
   import-style fixes (e.g., `yarn add lodash-es && yarn remove lodash`).
4. **Remove unused + draft vendor stubs** — remove unused, then for each
   vendoring candidate create a stub comment showing which symbols to implement.

For any execution option, show a dry-run list of what will change and confirm
before executing. If the user has the `create-ol-pull-request` skill available,
offer to create a PR after applying changes.

### Removal commands

| Ecosystem | Command |
|-----------|---------|
| Python (uv) | `uv remove <pkg>` |
| Python (pyproject.toml) | Edit `[project.dependencies]`, then `uv sync` |
| Python (requirements.txt) | Remove line; `pip install -r requirements.txt` |
| JS (npm) | `npm uninstall <pkg>` |
| JS (bun) | `bun remove <pkg>` |
| JS (yarn) | `yarn remove <pkg>` |
| JS (pnpm) | `pnpm remove <pkg>` |
| Go | Edit `go.mod`, then `go mod tidy` |
| Rust | Edit `Cargo.toml`, then `cargo build` |

After removal, run the project's test suite (or at minimum a build/typecheck).

---

## Additional caveats

- **Wildcard imports** (`from pkg import *`, `import * from 'pkg'`) make
  static analysis unreliable — note these and recommend converting to explicit
  imports as a follow-up.
- **Transitive deps used directly** — a dep used in code but not declared in
  the manifest (deptry's `DEP003`) is a separate problem; note it but don't
  flag for removal. These should be added to the manifest instead.
- **Optional feature extras** — packages installed as `pkg[extra]` may have
  conditional sub-imports; check the extras explicitly.
