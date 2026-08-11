# Skills

Reusable skills installed via [`agent-config-kit`](../packages/agent-config-kit/README.md)
(`agent-kit`) — see the repo-level [Quick Start](../README.md#quick-start) for
installing `agent-kit` and applying [`agent-config.toml`](../agent-config.toml).

Skills are organized by **category**. Each skill lives in
`skills/<category>/<skill-name>/SKILL.md` and carries YAML frontmatter with
`name`, `description`, `category`, and `tags` fields.

## Categories

| Category | Description |
|----------|-------------|
| [`python/`](./python/README.md) | Python tooling and dependency management |
| [`dagster/`](./dagster/README.md) | Dagster pipeline development with `dg` |
| [`infrastructure/`](./infrastructure/README.md) | Pulumi IaC and Vault secrets |
| [`containers/`](./containers/README.md) | Docker image builds |
| [`workflow/`](./workflow/README.md) | Cross-cutting process conventions |
| [`process/`](./process/README.md) | GitHub and external service interactions |

## All Skills

| Category | Skill | Description |
|----------|-------|-------------|
| python | [`uv-python-workflow`](./python/uv-python-workflow/SKILL.md) | Use `uv` exclusively for all Python env & dependency management |
| python | [`cyclopts-cli-scripts`](./python/cyclopts-cli-scripts/SKILL.md) | Use `cyclopts` for CLI scripts; place in `bin/` |
| dagster | [`dagster-code-location-structure`](./dagster/dagster-code-location-structure/SKILL.md) | `dg_projects/` layout, asset/sensor placement, one-at-a-time migration |
| infrastructure | [`pulumi-modify-existing`](./infrastructure/pulumi-modify-existing/SKILL.md) | Modify existing stack entrypoint; never create new files; preserve `assumeRole` |
| infrastructure | [`vault-k8s-auth`](./infrastructure/vault-k8s-auth/SKILL.md) | Wire Vault K8s auth via `hvac`; never hardcode role or mount path |
| containers | [`docker-uv-image-builds`](./containers/docker-uv-image-builds/SKILL.md) | `mitodl/<service>` naming, git short-ref tags, relocatable `uv` venvs |
| workflow | [`validate-before-commit`](./workflow/validate-before-commit/SKILL.md) | Run `pre-commit` → `mypy` → `pulumi preview` proactively before declaring done |
| workflow | [`creating-skills`](./workflow/creating-skills/SKILL.md) | Create a new skill: frontmatter, category placement, progressive disclosure, index updates |
| process | [`create-ol-github-issue`](./process/create-ol-github-issue/SKILL.md) | Create mitodl GitHub issues using org standard templates |
| process | [`create-ol-pull-request`](./process/create-ol-pull-request/SKILL.md) | Create mitodl pull requests using the org's standard PR template |
| process | [`create-ol-rfc-discussion`](./process/create-ol-rfc-discussion/SKILL.md) | Write and post a structured RFC as a GitHub Discussion in mitodl/hq under the RFC category |
| process | [`generate-standup`](./process/generate-standup/SKILL.md) | Generate and post a daily standup from GitHub activity to the mitodl/hq Check-ins discussion |
| process | [`screenshot-pr`](./process/screenshot-pr/SKILL.md) | Capture UI changes for a PR with shot-scraper at desktop, tablet, and mobile viewports |
| process | [`dependency-updates`](./process/dependency-updates/SKILL.md) | Triage and apply Renovate dependency updates safely across Python, JS/TS, Helm, Apt, and database ecosystems |
| process | [`dependency-pruning`](./process/dependency-pruning/SKILL.md) | Audit dependencies to find unused ones to remove and underused ones to vendor or rewrite |
| process | [`github-issue-triage`](./process/github-issue-triage/SKILL.md) | Audit open GitHub issues to identify stale, completed, or superseded items using parallel codebase cross-referencing |
| process | [`github-pr-triage`](./process/github-pr-triage/SKILL.md) | Categorize open PRs across an org by required action (needs first-pass review, has feedback, approved & ready to merge) and optionally act on them |
| process | [`address-pr-feedback`](./process/address-pr-feedback/SKILL.md) | Fetch, categorize, address, and resolve GitHub PR review feedback, with pagination for large/long-running PRs |
| process | [`renovate-security-triage`](./process/renovate-security-triage/SKILL.md) | Rank open Renovate PRs in your active repos by security urgency using advisory severity, CVSS, and EPSS (read-only) |

## Authoring a Skill

1. Pick or create a category directory: `skills/<category>/`
2. Create `skills/<category>/<skill-name>/SKILL.md` with frontmatter and body:

   ```markdown
   ---
   name: your-skill-name        # must match directory name exactly
   description: >               # what it does AND when to use it (max 1024 chars)
     Use this skill when...
   license: BSD-3-Clause
   metadata:
     category: <category>
   ---

   # Skill Title

   ...
   ```

3. Add the skill to your category's `README.md` table and to the **All Skills** table above.
4. Register it in [`agent-config.toml`](../agent-config.toml)'s `[skills]` table (and the relevant `[profiles.*]` entry) so `agent-kit apply` picks it up.
5. Open a PR — see the repo-level [CONTRIBUTING](../README.md#contributing) guide.
