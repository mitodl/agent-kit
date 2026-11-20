# Suppression syntax by tool

Look the spelling up here instead of reaching for a config change because the
inline form would not come to mind. These are the tools mitodl repositories
actually run; a finding from something else is covered by the last section.

Every form below was run against a deliberately-violating fixture with a second,
unsuppressed violation as a probe - the method [the skill](../SKILL.md) asks for.
Each tool ran at the version agent-kit pins for it - the hooks from `prek.toml`,
biome and tsc from `mcp/servers/witan/ui/package.json` - except gitleaks, which
ran from the prek hook cache. Renovate moves those pins, so read a version out
of the file rather than from a number written here. mypy, eslint and yamllint,
which agent-kit does not use, ran on current releases.

The five things to find in any tool, including one not listed here: the **rule
identifier**, the **inline escape**, the **file-level escape**, the **config
key**, and the **unused-suppression check**. Once you have those five, the ladder
in the skill applies unchanged.

## Python

### ruff

| | |
| --- | --- |
| Line | `# noqa: S603, S607` - trailing prose after the codes is allowed and is the house style: `# noqa: FLY002 - one alternative per line` |
| File | `# ruff: noqa: S603` at the top of the file |
| Config | `[tool.ruff.lint.per-file-ignores]`, a glob to a list of codes |
| Unused | `RUF100` - enable it |
| Rule docs | `ruff rule S603` |

Multiple codes go in one directive, comma-separated. A bare `# noqa` silences
every rule on the line, present and future; never write one.

### mypy

| | |
| --- | --- |
| Line | `# type: ignore[arg-type]  # reason` |
| File | `# mypy: disable-error-code="arg-type"` at the top of the file |
| Config | `[[tool.mypy.overrides]]` with `module = "..."` and `disable_error_code = [...]` |
| Unused | `warn_unused_ignores` - reports `[unused-ignore]` |

**The reason must be its own comment.** Prose written directly after the bracket
makes the whole directive invalid, and the error it was meant to suppress fires
anyway:

```python
a = takes_int("no")  # type: ignore[arg-type] upstream stub is wrong
# error: Invalid "type: ignore" comment  [syntax]
# error: Argument 1 to "takes_int" has incompatible type "str"; expected "int"
```

The habit carried over from ruff, where trailing prose is fine, produces a
suppression that silently is not one.

## JavaScript / TypeScript

agent-kit's UI lints with biome (`npm run lint` in
`mcp/servers/witan/ui`) and typechecks with `tsc --noEmit`. The org's React and
Next repos run eslint. All three appear below because a finding in a `.ts` file
can come from any of them, and each has a different escape.

### biome

| | |
| --- | --- |
| Line | `// biome-ignore lint/suspicious/noExplicitAny: reason` |
| File | `// biome-ignore-all lint/suspicious/noExplicitAny: reason` |
| Config | `overrides` in `biome.json`, keyed by `includes` globs |
| Unused | `suppressions/unused` - on by default, no configuration needed |
| Rule docs | `biome explain noExplicitAny` |

**The reason is mandatory.** Omitting `: reason` is not a reason-less
suppression, it is a parse error - and the finding is reported as well:

```text
suppressions/parse  × Unexpected token, expected one of ':', '(' or whitespace
```

The rule identifier is the full path (`lint/<group>/<rule>`), not the bare rule
name that appears in the diagnostic's right-hand column.

### eslint

| | |
| --- | --- |
| Line | `// eslint-disable-next-line no-console -- reason` |
| Block | `/* eslint-disable no-console */` ... `/* eslint-enable no-console */` |
| File | `/* eslint-disable no-console */` with no matching enable |
| Config | flat config: an entry with `files` and `rules` |
| Unused | `linterOptions: { reportUnusedDisableDirectives: "error" }` |

The reason goes after ` -- `; the separator is two dashes surrounded by spaces,
and everything after it is free text. Prefer `-next-line` over the block pair:
code added inside a region later is silently covered too.

### TypeScript (tsc)

| | |
| --- | --- |
| Line | `// @ts-expect-error reason` - prose goes straight after the directive |
| File | `// @ts-nocheck` |
| Config | `exclude` in `tsconfig.json` - rung 5, it stops checking those files entirely |
| Unused | built in to `@ts-expect-error`: `TS2578: Unused '@ts-expect-error' directive` |

**Use `@ts-expect-error`, not `@ts-ignore`.** They suppress the same way, but the
first fails the build once the underlying error is gone, and the second sits
there silently forever. There is no rule identifier for either, so both are
broad: they cover every error on the next line. Keep the line they guard small.

## Shell

### shellcheck

| | |
| --- | --- |
| Line | `# shellcheck disable=SC2086` - applies to the **next command only** |
| File | the same directive placed before the first command |
| Config | `.shellcheckrc` with `disable=SC2086` - rung 5 |
| Unused | none; a directive that suppresses nothing is reported nowhere |

Because there is no unused-directive check, a shellcheck disable left behind
after a refactor is invisible. Re-run with it removed when touching the code
around it.

## Workflows and CI config

### zizmor

| | |
| --- | --- |
| Line | `# zizmor: ignore[template-injection]`, **on the same line as the finding** |
| Config | `.github/zizmor.yml`: `rules:` → the rule name → `ignore:` → a list of filenames |
| Unused | none. The run summary counts ignored findings in aggregate, which will not tell you that *your* directive stopped matching |

Placement is the trap. The comment on the line *above* the flagged line parses
fine, suppresses nothing, and looks exactly like a working suppression:

```yaml
      # zizmor: ignore[template-injection]
      - run: echo "${{ github.event.comment.body }}"   # still reported
      - run: echo "${{ github.event.comment.body }}" # zizmor: ignore[template-injection]
```

Confirm it took: the summary line reads `N findings (1 ignored, ...)`.

### actionlint

| | |
| --- | --- |
| Line | **none** - there is no inline escape |
| Config | `.github/actionlint.yaml`, `paths:` mapping a glob to `ignore:` regexes matched against the message text |
| CLI | a repeatable `-ignore <regex>`, which applies to every file the run covers - broader than the config, so prefer `paths:` |
| Unused | none |

With no inline form, the smallest available suppression is a config entry keyed
to one path and one message. That is still narrow if the regex is specific:

```yaml
paths:
  .github/workflows/probe.yml:
    ignore:
      - 'input "nosuchinput" is not defined'
```

Other findings in the same file keep firing, including other occurrences of the
same rule with a different message. A regex like `'is not defined'` would not -
write the longest pattern that matches the one message.

actionlint resolves the config relative to the repository root, so it is read
only when the tool runs inside the checkout.

### gitleaks

| | |
| --- | --- |
| Line | `gitleaks:allow` anywhere in a comment on the line |
| Config | `.gitleaksignore`, which takes fingerprints from the report - see gitleaks' own docs for its shape |

A test fixture that must contain a credential-shaped string is the legitimate
case; `mcp/servers/witan/tests/test_detectors.py` is the worked example, and it
carries the pragma for both scanners that look at it. Never silence a secret
scanner by path for anything but fixtures, and say which paths a new exclusion
skips.

### prek / pre-commit hooks

| | |
| --- | --- |
| Line | whatever the underlying tool provides; the framework adds none |
| Config | `exclude` (a regex) or `files` on the individual hook |

`detect-private-key` is the case worth knowing: it has **no** inline pragma, so
the only lever is a per-hook `exclude`. agent-kit's `prek.toml` shows the
shape - a one-file regex with a comment above it saying why that file is
exempt and why the other scanners still cover it.

Exclude the one hook, never the whole file list, and never a parent directory
when a filename will do.

## Markup

### markdownlint

| | |
| --- | --- |
| Line | `<!-- markdownlint-disable-next-line MD034 -->` |
| Block | `<!-- markdownlint-disable MD034 -->` ... `<!-- markdownlint-enable MD034 -->` |
| File | `<!-- markdownlint-disable-file MD034 -->` |
| Config | `.markdownlint.yaml` |

agent-kit's `.markdownlint.yaml` is the model for a rung-5 entry: every
disabled rule carries a comment saying what about that repository's Markdown
makes the rule wrong, not that it was noisy.

### yamllint

| | |
| --- | --- |
| Line | `# yamllint disable-line rule:line-length` |
| Block | `# yamllint disable rule:line-length` ... `# yamllint enable rule:line-length` |
| File | `# yamllint disable-file`, **on the first line of the file** |
| Config | `.yamllint` |

Placement is the trap, as it is for zizmor. `disable-file` is honoured only as
the very first line, so it has to go *above* the `---` that opens most workflow
and config YAML. One line lower it is inert - no error, no warning, and the
finding still reported:

```yaml
---
# yamllint disable-file   # silently does nothing here
one: <a line over the limit>   # still reported
```

## A tool that is not listed

Find the five parts - rule identifier, inline escape, file escape, config key,
unused-suppression check - in the tool's own documentation before deciding the
scope. Two questions settle most of it:

- Does the inline escape take a rule identifier? If it does, always name one. If
  it does not (like `@ts-nocheck`), the escape is broader than it looks and
  belongs on the smallest possible region.
- Does the tool report suppressions that no longer suppress anything? If not, the
  only way to know is to remove it and re-run - so do that before extending one.
