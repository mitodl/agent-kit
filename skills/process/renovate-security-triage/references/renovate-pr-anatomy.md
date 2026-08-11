# Renovate PR Anatomy

What Renovate actually puts in a PR, and which parts `enrich-renovate-prs.sh`
parses. All examples are real bodies from `mitodl` PRs.

## Title

Renovate titles follow the repo's `commitMessagePrefix` config, so they are
**inconsistent across an org**. All of these are real:

```text
Update dependency cryptography to v50 [SECURITY]
fix(deps): update dependency mitol-django-oauth-toolkit-extensions to v2026
chore(deps): update dependency webpack-bundle-analyzer to v5.3.1
chore(deps): update actions/setup-python action to v7
Pin dependencies
chore(deps): lock file maintenance
```

`[SECURITY]` is appended when Renovate resolves a vulnerability alert — but
**only when the repo's config produces it**. Do not rely on it:

> `mitodl/ol-keycloakify#174` is titled `chore(deps): update dependency
> @vitest/browser to v4.1.10` with no marker, and its body carries
> `GHSA-p63j-vcc4-9vmv` — a **CVSS 9.4** advisory.

This is why detection keys off body GHSA ids as well as the title marker.

## Labels

**Empty on every Renovate PR sampled in mitodl.** Renovate only applies labels
when `labels` / `vulnerabilityAlerts.labels` is configured, and these repos do
not. Never classify on labels.

## Body: the update table

Standard form, one row per package:

```markdown
This PR contains the following updates:

| Package | Change | [Age](...) | [Confidence](...) |
|---|---|---|---|
| [cryptography](https://redirect.github.com/pyca/cryptography) ([changelog](...)) | `>=49,<50` → `>=50,<51` | ![age](...) | ![confidence](...) |
```

Parsing notes:

- The package cell may be a markdown link, a link plus a `([changelog](...))`
  suffix, or bare text. The parser takes the first `[...]` label, falling back to
  the trimmed cell.
- **The version cell is not necessarily a semver.** It holds whatever the
  manifest expresses — `>=49,<50` (a constraint range), `1.2.3` (a pin), or a
  digest. The parser extracts the backticked pair and derives `bump` by
  comparing the first differing integer, which handles all three.
- Age and Confidence are Mend Merge Confidence badge images, not data. They are
  ignored — the badge URL encodes the versions, nothing about severity.

### Variant: lock file maintenance

```markdown
| Update | Change |
|---|---|
| lockFileMaintenance | All locks refreshed |
```

Different columns, no `→`. Yields zero `updates`, tagged
`kind: "lockfile_maintenance"`. Nine of 175 PRs in the reference run were these.

### Variant: pin dependencies

Titled `Pin dependencies`; adds exact pins without changing resolved versions.
Tagged `kind: "pin"`. Frequently produces `bump: "other"`.

## Body: the advisory block

When the update closes a vulnerability, Renovate inserts a section **before** the
release notes:

```markdown
### cryptography: PKCS#7 EnvelopedData decryption exposes a Bleichenbacher oracle
[CVE-2026-69247](https://nvd.nist.gov/vuln/detail/CVE-2026-69247) / [GHSA-g6cj-pr64-35w5](https://redirect.github.com/advisories/GHSA-g6cj-pr64-35w5)

<details>
<summary>More information</summary>

#### Details
…
#### Severity
…
#### References
- https://github.com/advisories/GHSA-g6cj-pr64-35w5
…
```

There is **no** stable `## GitHub Vulnerability Alerts` heading to anchor on —
the heading is the advisory's own summary text. So GHSA ids are harvested by
regex across the whole body (`GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}`),
deduplicated.

The same id repeats many times per advisory (heading link, References list, OSV
attribution, GitHub Advisory Database attribution) and a grouped PR repeats the
whole block per member — hence the `unique`.

**Why whole-body harvesting is safe enough here:** the `### Release Notes`
section that follows can mention unrelated advisories in changelog text. In
practice that is rare and errs toward over-reporting, which is the right
direction for security triage. If it ever produces a visible false positive,
scope the regex to the body text *before* the `### Release Notes` marker.

## Body: release notes and configuration

`### Release Notes` (collapsed `<details>`) then `### Configuration` describing
the schedule, rebase policy, and the Dependency Dashboard checkbox. Neither is
parsed. The Configuration section is where you learn whether the PR is
automerge-eligible, if the user asks.

## Ecosystem inference

The advisory database is keyed by ecosystem, which Renovate does not state, so it
is inferred from the changed manifest paths:

| Path pattern | Ecosystem |
|--------------|-----------|
| `pyproject.toml`, `uv.lock`, `poetry.lock`, `Pipfile`, `setup.py/cfg`, `requirements*.txt` | `PIP` |
| `package.json`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `bun.lock` | `NPM` |
| `go.mod`, `go.sum` | `GO` |
| `Gemfile`, `*.gemspec` | `RUBYGEMS` |
| `Cargo.toml`, `Cargo.lock` | `RUST` |
| `pom.xml`, `build.gradle` | `MAVEN` |
| `composer.json` | `COMPOSER` |
| `*.csproj`, `packages.config` | `NUGET` |
| `pubspec.yaml` | `PUB` |
| `.github/workflows/*` | `ACTIONS` |
| `Chart.yaml`, `values.yaml`, `Dockerfile`, `docker-compose.yml` | **none** |

The full enum is `ACTIONS COMPOSER ERLANG GO MAVEN NPM NUGET PIP PUB RUBYGEMS
RUST SWIFT` (`SecurityAdvisoryEcosystem`). Helm charts and container images have
no entry — those PRs come back `ecosystems: []` and are unscorable from advisory
data alone. Report them as unscored, never as safe.

## Dependency Dashboard

Renovate maintains a per-repo `Dependency Dashboard` issue listing everything
pending, including updates with no PR yet. This skill does not read it — it
triages open PRs. It is worth mentioning to the user as the place to see
suppressed or rate-limited updates.

Its checkboxes are **write** controls. Ticking one makes Renovate act. This skill
never touches them.
