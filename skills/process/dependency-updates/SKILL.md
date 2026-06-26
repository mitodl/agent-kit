---
name: dependency-updates
description: >
  Triage, evaluate, and apply dependency updates safely — whether from Renovate
  PRs, a proactive audit, or a project that has fallen behind. Use this skill
  when you need to discover what is outdated, decide what is safe to update in
  parallel, sequence updates that have ordering constraints, evaluate changelogs
  and blast radius, adapt code to breaking changes, and verify the result.
  Covers any ecosystem (Python, JavaScript/TypeScript, Go, Ruby, Rust, Java,
  etc.) and any layer of the stack (libraries, frameworks, Helm/Kubernetes
  charts, Docker base images, Apt/system packages, databases, operators,
  Terraform/Pulumi providers).
license: BSD-3-Clause
metadata:
  category: process
---

# Dependency Updates

## Scope

| Layer | Examples |
|-------|---------|
| Language library / framework | Python packages, npm packages, Go modules, Ruby gems, Cargo crates, Maven artifacts |
| Container base image | `FROM python:3.9-slim` → `3.12-slim`, `ubuntu:20.04` → `24.04`, distroless variants |
| System / OS packages | `apt-get install`, Alpine `apk`, Homebrew `brew` in CI |
| Helm / Kubernetes charts | Bitnami charts, operator Helm releases, CRD-carrying chart upgrades |
| Database / storage | PostgreSQL, Redis, Elasticsearch, MongoDB — via Helm, Docker Compose, or operators |
| Infrastructure tooling | Terraform providers, Pulumi SDKs, OpenTofu modules |

---

## Effort sizing

| Bump | Library | Infra / DB | Base image |
|------|---------|-----------|------------|
| Patch (x.y.**z**) | Changelog skim + test run | Changelog skim + test run | Rebuild + smoke |
| Minor (x.**y**.0) | Changelog + usage check + test | Changelog + values diff + test | Rebuild + smoke test |
| Major (**x**.0.0) | Full Phase 1–6 | Full Phase 1–6 + staging | Full Phase 1–6 + rebuild |
| Digest / date pin | Rebuild + smoke | N/A | Rebuild + smoke |

---

## Phase 0 — Discover what is outdated

When you don't have an automated tool handing you a list, generate it yourself. Run the appropriate command for each ecosystem present in the project.

### Python

```bash
uv lock --outdated          # uv projects
pip list --outdated         # pip-managed environments
pip-audit                   # combined outdated + known vulnerabilities
```

### JavaScript / TypeScript

```bash
npm outdated                # shows current / wanted / latest per package
bun outdated                # bun equivalent
yarn outdated               # yarn classic
pnpm outdated               # pnpm
npm audit                   # known vulnerabilities
```

### Go

```bash
go list -m -u all           # shows available updates for every module in go.mod
govulncheck ./...           # known vulnerabilities in the dependency graph
```

### Ruby

```bash
bundle outdated             # shows gems with available updates
bundle audit                # known vulnerabilities (requires bundler-audit)
```

### Rust

```bash
cargo outdated              # requires cargo-outdated: cargo install cargo-outdated
cargo audit                 # known vulnerabilities
```

### Helm / Kubernetes charts

```bash
helm repo update
helm search repo <chart-name> --versions | head -20   # see all available versions
# For each chart in Chart.yaml dependencies:
helm dependency list        # shows current vs latest for each dependency
```

### Docker base images

There is no universal "outdated" command for base images. Check freshness by:
```bash
# Pull the latest version of the pinned tag and compare digests
docker pull <image>:<tag> --quiet
docker inspect <image>:<tag> --format '{{.Id}}'
# Compare against the digest currently in your Dockerfile
```
Or check the image's registry page / GitHub releases for the upstream project.

### Terraform / OpenTofu providers

```bash
terraform providers lock -upgrade    # updates .terraform.lock.hcl with latest versions
tofu providers lock -upgrade         # OpenTofu equivalent
# Then review what changed:
git diff .terraform.lock.hcl
```

### Apt / system packages (in Dockerfile or CI)

```bash
# Run inside the container to see what is upgradable
apt-get update && apt list --upgradable 2>/dev/null
```

---

## Phase 1 — Characterize each update

For each outdated package (from Phase 0, from Renovate, or from a targeted single-package check):

```bash
# If the update came as a PR:
gh pr checkout <PR number>
git diff HEAD~1 -- '*.toml' '*.json' '*.yaml' '*.lock' 'Dockerfile*' '*.tf'

# If you are applying the update manually, record the old version first:
# grep <package> pyproject.toml   # or go.mod, package.json, Chart.yaml, etc.
```

Establish for each package:
- Version range: old → new
- Bump classification: patch / minor / major / digest
- Direct or transitive dependency
- Stack layer (library, chart, base image, system, DB)

---

## Phase 2 — Evaluate the changelog

### Where to find it

1. **GitHub releases** (most authoritative): `gh api repos/<owner>/<repo>/releases --jq '.[].body'`
2. **Package registry pages** — PyPI, npm, crates.io, pkg.go.dev all link to changelogs
3. **`CHANGELOG.md`** in the repo — often more complete than release notes
4. **Helm charts** — diff `values.yaml` between versions, which is where breaking changes hide:
   ```bash
   helm show values <repo>/<chart> --version <old> > old.yaml
   helm show values <repo>/<chart> --version <new> > new.yaml
   diff old.yaml new.yaml
   ```
5. **Docker base images** — check the upstream project release page plus the base OS release notes (e.g., Debian Bookworm migration guide)

### What to flag

Read every entry from the current version to the new version:

- **Breaking change** — API removals, signature changes, renamed config keys, removed CLI flags
- **Deprecation** — items that now emit warnings; not merge blockers, but open a follow-up task
- **Security fix** — CVE IDs and advisory links; these jump to the front of the queue
- **Silent behavior change** — changed defaults, altered retry/timeout/encoding behavior
- **Runtime requirement raised** — minimum Python/Node/Go version bumped above what you run

---

## Phase 3 — Locate usages (blast radius)

Build a list of every file that references the package.

### Language libraries

```bash
# Adapt the pattern and --type flag to the language
rg "<package-name>" --type py           # Python
rg "\"<package-name>\"" go.mod          # Go module declaration
rg "<package-name>" --type ts --type tsx # TypeScript
rg "'<gem-name>'" Gemfile               # Ruby
rg '"<crate>"' Cargo.toml               # Rust
```

Also search config files that reference the package by name: plugin configs, lint configs, CI workflow steps.

### Docker / container base images

```bash
rg "FROM <image-name>" --glob 'Dockerfile*' --glob '*.dockerfile'
rg "apt-get install" --glob 'Dockerfile*'   # apt lines may reference packages affected by base change
```

Multi-stage builds: check whether intermediate `AS build` stages also use the image.

### Helm / Kubernetes charts

```bash
rg "<chart-name>" charts/ -l
# Render the current state as a baseline before upgrading
helm template <release-name> <chart> -f values.yaml > baseline.yaml
```

---

## Phase 4 — Evaluate impact

Cross-reference each usage site against the changelog flags from Phase 2.

| Finding | Decision |
|---------|----------|
| No breaking changes in range | Skip Phase 5, go to Phase 6 |
| Deprecated API in use | Note; do not block; open a follow-up task |
| Removed/changed API in use | Must adapt before applying → Phase 5 |
| Behavior change at a usage site | Write or update a test to assert the new behavior |
| Runtime requirement raised above current | Upgrade runtime first as a separate dependency |

### Layer-specific checks

**Base image (Docker):**
- A base image bump often changes two things simultaneously: the OS version (e.g., Buster → Bookworm) and the bundled runtime (e.g., Python 3.9 → 3.12). Evaluate both axes separately.
- Check `apt-get install` lines for packages that were renamed or removed in the new OS version
- Run `docker build` locally — ABI and missing-library failures surface here, not at changelog review time
- In multi-stage builds, confirm both `FROM` lines change to the same new base to avoid ABI mismatches when copying compiled artifacts between stages
- **Python 3.12 specific removals** (commonly bite at build or import time): `distutils` (removed; use `setuptools`), `cgi` and `cgitb` (removed; no stdlib replacement), `imghdr`, `aifc`, `chunk`, `crypt`, `mailcap`, `msilib`, `nis`, `nntplib`, `ossaudiodev`, `pipes`, `sndhdr`, `spwd`, `sunau`, `telnetlib`, `uu`, `xdrlib` — search your source and dependencies for any of these:
  ```bash
  rg "import (distutils|cgi|cgitb|imghdr|aifc|crypt|mailcap|pipes)" --type py
  pip show setuptools   # must be present if any dep still calls distutils at install time
  ```

**Helm chart major bumps:**
- Diff CRDs: fields may be promoted, removed, or have changed validation
- Verify `apiVersion` in rendered manifests is supported by your target cluster version
- Identify renamed or removed `values.yaml` keys against your override files
- Run `helm template --dry-run` with your existing values file to surface missing keys

**Database / operator major versions:**
- Check for removed SQL functions, changed defaults, wire protocol changes
- Verify the ORM or driver supports the new server version
- Determine if the chart upgrade implies a data migration — PostgreSQL major upgrades require `pg_upgrade` or a dump-restore cycle; this must be planned independently of the chart bump
- Validate against a staging or local instance before applying to production

**Infrastructure tooling:**
- Run `plan` / `preview` before merging — provider majors often rename resources or move arguments
- Check for deprecated resource types that were removed in the new version

---

## Phase 5 — Adapt code (only if Phase 4 found a blocker)

Make the **minimum change** to satisfy the new API. Do not refactor surrounding code.

If the package provides a codemod, migration script, or official upgrade guide, follow it rather than hand-editing.

After adapting:
- Re-run type checkers and linters
- Confirm the changed call sites resolve (`python -c "import <pkg>"`, `go build ./...`, etc.)
- For Docker: rebuild locally and confirm startup behavior

---

## Phase 6 — Verify

Run the full test suite before committing the update.

```bash
# Substitute your project's test command
pytest -x              # Python
go test ./...          # Go
npm test               # JS/TS
cargo test             # Rust
bundle exec rspec      # Ruby
```

Also run linters and type checkers — minor bumps can introduce type errors without breaking tests.

For infrastructure:
```bash
helm template . -f values.yaml | kubectl apply --dry-run=server -f -
pulumi preview --stack <stack>
terraform plan
```

### Interpreting failures

- **Import / link errors** → API changed; return to Phase 5
- **Type errors** → fix call sites; don't suppress without an explanatory comment
- **Failures in unrelated modules** → investigate before assuming pre-existing; transitive behavior changes surface far from the import
- **Docker build failures** → check `apt-get` pins and runtime compatibility with the new base
- **Helm dry-run failures** → a value key was renamed or a CRD version changed; re-run the values diff

---

## Batch strategy — working through a backlog

Whether you discovered the backlog via `uv lock --outdated`, `npm outdated`, `go list -m -u all`, or a pile of Renovate PRs, the same sequencing logic applies.

### Step 1: Build the full inventory

Run Phase 0 for every ecosystem in the project. Produce a flat list: package name, current version, latest version, bump type. Group by ecosystem.

### Step 2: Classify by risk tier

| Tier | Criteria | Approach |
|------|----------|----------|
| 1 — Security | Known CVE or security advisory | Apply immediately, ahead of everything else |
| 2 — Patch | x.y.**z** bumps | Batch-apply per ecosystem; one test run per batch |
| 3 — Minor (clean) | No breaking flags in changelog | Review + test, one ecosystem group at a time |
| 4 — Minor (with deprecations) | Deprecations in use | Review + adapt + test; open follow-up tasks |
| 5 — Major | API or behavior changes | Full Phase 1–6 per package, one at a time |
| 6 — Runtime / OS / DB | Base image, database major, language runtime | Last; staging validation required |

### Step 3: Determine what can be parallelized

Many updates within a tier can be applied simultaneously:

**Safe to batch (apply together, one test run):**
- All patch bumps within a single ecosystem (e.g., every Go module patch in one `go get -u=patch ./...`)
- All minor bumps within an ecosystem that have no breaking flags and no dependency relationships between them
- Updates across different ecosystems at the same tier (Go patches and npm patches can proceed simultaneously on separate branches)

**Must be sequenced (one at a time or in explicit order):**
- **Runtime before libraries** — if a library requires Python 3.12+, upgrade Python first; the library upgrade is blocked until then
- **Leaf before root** — if package A imports package B, upgrade B before A, or their version constraints may conflict
- **DB client before DB server** — the driver or ORM must support the new protocol before the server is upgraded
- **CRDs before operator chart** — Kubernetes applies CRDs separately; an operator upgrade that references a missing CRD field will fail
- **Major bumps within the same module graph** — do not combine multiple major bumps that touch overlapping files; you won't be able to attribute failures

For cross-ecosystem updates (Go service + npm frontend + Helm charts), those ecosystems are independent and their work streams can proceed in parallel even when their internal sequencing differs.

### Step 4: Apply in waves, test between each

```
Wave 1 — Security: apply individually; fast-path merge
Wave 2 — Patches: batch per ecosystem with `uv sync`, `go get -u=patch`, `npm update --save-exact`, etc.
Wave 3 — Clean minors: one ecosystem group at a time; full test suite after each group
Wave 4 — Minors with deprecations: adapt call sites, then test
Wave 5 — Majors: one package at a time, full Phase 1–6
Wave 6 — Runtime / OS / DB: coordinate with deployment; validate in staging
```

Run the full test suite after each wave. If a wave introduces failures, diagnose before proceeding to the next wave — do not accumulate undiagnosed failures across multiple waves.

### Useful batch-apply commands by ecosystem

```bash
# Python — apply all patch-level updates
uv lock --upgrade-package <pkg>    # upgrade a single package
uv sync --upgrade                  # upgrade all to latest allowed by constraints

# Go — apply patch updates only
go get -u=patch ./...
go mod tidy

# npm — update to latest satisfying semver range
npm update
# Update a specific package to a specific version
npm install <pkg>@<version>

# Rust
cargo update                       # update all deps within semver constraints
cargo update -p <crate>            # update a single crate

# Ruby
bundle update --patch              # patch-level updates only
bundle update <gem>                # update a single gem

# Helm — update a chart dependency to a new version
# Edit Chart.yaml version pin, then:
helm dependency update
```

### Step 5: Track progress

Keep a simple checklist in a tracking issue or branch description:

```
[ ] Wave 1 — security: CVE-2024-xxxxx (pkg A), CVE-2024-yyyyy (pkg B)
[x] Wave 2 — patches: 14 Go modules, 8 npm packages — CI green
[ ] Wave 3 — clean minors: Go (in progress), npm (pending), Helm (pending)
```

---

## Decision matrix

| Signal | Action |
|--------|--------|
| Patch bump, tests green | Apply |
| Minor bump, no breaking flags, tests green | Apply |
| Minor bump, deprecation warnings new | Apply + open follow-up task |
| Major bump, no usage of changed APIs, tests green | Apply |
| Major bump, removed API in use | Adapt code (Phase 5), then apply |
| Base image major bump | Build locally, smoke test, then apply |
| DB / operator major bump | Validate in staging; coordinate with deploy |
| Security advisory (any bump size) | Prioritize; apply ahead of the queue |
| Tests fail, root cause unclear | Do not apply; open investigation task |
| Runtime version requirement raised | Upgrade runtime first as its own wave |
| Two updates touch overlapping files | Sequence them; do not combine |
