---
name: validate-before-commit
description: >
  Run the full validation sequence before declaring any task done. Use this skill
  proactively after any code or infrastructure change — run the repository's
  hooks with prek, then mypy and pulumi preview (where applicable), without
  waiting to be asked. Never declare success without passing checks.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Validate Before Commit

**Do not declare a task done until the full validation sequence has passed.**
Run these checks proactively — do not wait for the human to ask.

## Standard sequence

```bash
# 1. Linting, formatting, and basic static checks
uv run prek run --all-files

# 2. Type checking
uv run mypy <package_or_src_dir>

# 3. Infrastructure plan (if Pulumi files were changed)
pulumi preview --stack <stack-name>
```

mitodl repositories run their hooks with [prek](https://prek.j178.dev), which
reads the same `.pre-commit-config.yaml` (or `prek.toml`) that pre-commit
did. Use the command for the repository's prek pin:

| Repository | Hook command |
|------------|--------------|
| Python with `uv` (prek in the `prek` dependency group) | `uv run prek run --all-files` |
| Node (`@j178/prek` in `devDependencies`) | `npx prek run --all-files` |
| Neither (prek installed as a tool) | `prek run --all-files` |
| Not yet migrated (still declares `pre-commit`) | `uv run pre-commit run --all-files` |

In a repository with `.github/workflows/autofix.yml`, the same hooks run in CI
as the `prek` check, and autofix.ci commits any fixes they make to the PR.
Running them locally first saves that round trip.

## Rules

- Run the hooks **before** mypy; they may auto-fix formatting that would
  otherwise produce mypy noise.
- If the hooks auto-fix files, stage the changes and re-run them to confirm
  every hook passes cleanly.
- mypy errors are blocking — do not leave type errors for the human to clean up.
- `pulumi preview` output must be reviewed: unexpected replacements or deletions
  are bugs, not acceptable side effects.

## For Dagster / dg changes

After modifying assets, sensors, or code location structure, also confirm the
workspace loads cleanly:

```bash
dg check
```

## Interpreting failures

Read the full error output before proposing a fix. Do not apply a speculative
patch without understanding the root cause — iterating blindly on the same error
multiple times is worse than pausing to diagnose.
