---
name: pulumi-modify-existing
description: >
  Modify existing Pulumi infrastructure stacks safely. Use this skill when making
  any Pulumi IaC changes — always edit the existing stack entrypoint, never create
  new files, preserve assumeRole and cross-account configuration, and validate with
  pulumi preview before finishing.
license: BSD-3-Clause
metadata:
  category: infrastructure
---

# Pulumi: Modify Existing Files

## Read the repo's conventions first

Before editing, read the repository's own `AGENTS.md` and any guides it points
to (in `mitodl/ol-infrastructure`: `docs/context/03-pulumi-workflows.md`,
`05-secrets-management.md`, `06-code-style-conventions.md`). They own stack
naming, component rules, helpers, and secrets layout, and they change; don't
restate them from memory.

Regardless of repo, these hold:

- **Environment values come from stack config**, not literals in `__main__.py`.
  A value that differs between CI, QA, and Production belongs in
  `Pulumi.<project>.<Env>.yaml`, read with `Config(...).require()` when it's
  required.
- **Secrets never land in plaintext**: use the repo's SOPS files, Vault, or
  `pulumi config set --secret` (`secure:` values).
- **Pinned versions live where the repo centralizes them** (ol-infrastructure:
  `src/bridge/lib/versions.py`, which Renovate updates), not inline.
- **Reuse existing components and helpers** (`parse_stack()`, `OL*` component
  resources, stack outputs such as shared security groups) instead of building
  raw resources that duplicate them.

## Always modify, never create

Infrastructure changes belong in the **existing** entrypoint for the stack
(typically `__main__.py`). Do not create additional Pulumi files unless the stack
already follows a multi-file pattern that makes a new file the right fit.

## Preserve existing functionality

When editing a Pulumi stack entrypoint, the following must **never** be silently
removed:

- **`assumeRole` / cross-account role configuration** — removing this breaks
  production deployments that depend on cross-account access.
- Existing resource exports (`pulumi.export(...)`).
- Stack references to other stacks.

Before finalizing changes, scan the diff to confirm nothing was deleted
unintentionally.

## Validate changes before declaring done

After modifying any Pulumi code, run a preview to confirm the plan is correct:

```bash
pulumi preview --stack <stack-name>
```

Review the output carefully:
- Unexpected resource **replacements** or **deletions** are bugs, not acceptable
  side effects. The common cause is a changed logical resource name, Helm release
  name, or component name on something that already exists. Keep the original
  name, or add `opts=ResourceOptions(aliases=[...])` so Pulumi treats it as the
  same resource.
- "0 changes" is only correct if you genuinely expected no changes.
