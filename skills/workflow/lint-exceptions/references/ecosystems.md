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
| File | `# ruff: noqa: S603` - honoured anywhere in the file, not only at the top |
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

**The reason must be its own comment, and for the file-level form its own
line.** Prose written directly after the bracket makes the line directive
invalid, and the error it was meant to suppress fires anyway:

```python
a = takes_int("no")  # type: ignore[arg-type] upstream stub is wrong
# error: Invalid "type: ignore" comment  [syntax]
# error: Argument 1 to "takes_int" has incompatible type "str"; expected "int"
```

A second comment on the same line is the fix there, as in the Line row above.
The file-level form takes no trailing comment at all, though: mypy parses the
rest of the line as part of the code list.

```python
# mypy: disable-error-code="arg-type"  # upstream stubs wrong
# error: disable_error_code: Invalid error code(s): arg-type  # upstream stubs wrong
# error: Argument 1 to "takes_int" has incompatible type "str"; expected "int"
```

Put that reason on the line below. The habit carried over from ruff, where
trailing prose is fine, produces a suppression that silently is not one.

## JavaScript / TypeScript

agent-kit's UI lints with biome (`npm run lint` in
`mcp/servers/witan/ui`) and typechecks with `tsc --noEmit`. The org's React and
Next repos run eslint. All three appear below because a finding in a `.ts` file
can come from any of them, and each has a different escape.

### biome

| | |
| --- | --- |
| Line | `// biome-ignore lint/suspicious/noExplicitAny: reason` |
| File | `// biome-ignore-all lint/suspicious/noExplicitAny: reason`, which has to be the first thing in the file |
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

`biome-ignore-all` placed after any code is reported as `suppressions/incorrect`
and suppresses nothing - including the findings below it, which keep firing.

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
| Config | `exclude` in `tsconfig.json` - it filters `include`, and is not a suppression at all for a file something else imports |
| Unused | built in to `@ts-expect-error`: `TS2578: Unused '@ts-expect-error' directive` |

**Use `@ts-expect-error`, not `@ts-ignore`.** They suppress the same way, but the
first fails the build once the underlying error is gone, and the second sits
there silently forever. There is no rule identifier for either, so both are
broad: they cover every error on the next line. Keep the line they guard small.

**`exclude` does not take a file out of typechecking.** It only narrows the set
of files `include` seeds the program with. A file that an included file imports
is pulled back in and its errors are still reported, named against the excluded
path:

```text
src/bad.ts(1,14): error TS2322: Type 'string' is not assignable to type 'number'.
```

Only an orphan - a file nothing imports - actually goes quiet, which is the case
where excluding it changes nothing worth having. Reach for `@ts-expect-error` on
the line instead.

## Shell

### shellcheck

| | |
| --- | --- |
| Line | `# shellcheck disable=SC2086` - applies to the **next complete command** |
| File | the same directive, placed before the first command |
| Config | `.shellcheckrc` with `disable=SC2086` - rung 5 |
| Unused | none; a directive that suppresses nothing is reported nowhere |

**One directive, two scopes, decided by where it sits.** Before the first
command - right under the shebang, where it reads like a file header - it
covers the whole script. Anywhere else it covers the next complete command,
and for a `for`, `while`, `if` or function that means the entire body, not the
first line of it:

```bash
# shellcheck disable=SC2086
for f in $files; do
    ls $f       # suppressed
    cp $f /tmp  # also suppressed - same command
done
ls $files       # reported: the loop was the command
```

So a disable written for one expansion, moved to the top of the script while
tidying, silently becomes a file-wide exemption with no diff to show for it.

Because there is no unused-directive check, a shellcheck disable left behind
after a refactor is invisible. Re-run with it removed when touching the code
around it.

## Workflows and CI config

### zizmor

| | |
| --- | --- |
| Line | `# zizmor: ignore[template-injection]`, **inside the span the finding points at** |
| Config | `.github/zizmor.yml`: `rules:` → the rule name → `ignore:` → entries of `file.yml`, `file.yml:line`, or `file.yml:line:col` (base filename, not a path) |
| Unused | none. The run summary counts ignored findings in aggregate, which will not tell you that *your* directive stopped matching |

Placement is the trap, and it has two halves. The comment goes on any line
within the finding's span - not only the one the diagnostic underlines - and a
comment *above* the span parses fine, suppresses nothing, and looks exactly like
a working suppression:

```yaml
name: label
on: [pull_request_target] # zizmor: ignore[dangerous-triggers] - labels only, never checks out the head
```

One line higher - above `on:` - and the finding is still reported.

The second half is block scalars, where the reported line is the wrong place by
construction. For a `template-injection` finding inside a `run: |`, a comment on
the flagged line becomes part of the shell script, and zizmor never sees a
directive at all:

```yaml
      - run: |  # zizmor: ignore[template-injection]
          echo "${{ github.event.comment.body }}"
```

The directive has to sit outside the scalar - on the `run: |` line, as above.
On the `echo` line the run reports the finding unchanged, with `0 ignored`.
Either way, confirm it took: the summary line reads `N findings (1 ignored, ...)`.

The rule in that example is one a suppression can be right about: a
`pull_request_target` workflow that only labels a PR, and never checks out or
runs the contributor's code, does not have the privilege-escalation shape
`dangerous-triggers` warns about. **`template-injection` is not that rule.**
When zizmor reports `${{ github.event.* }}` reaching a `run:` block, the finding
is real and the fix is rung 0 - pass the value through `env:` so the shell reads
it as data instead of the runner pasting it into the script:

```yaml
      - run: echo "$BODY"
        env:
          BODY: ${{ github.event.comment.body }}
```

A suppression there leaves a runner that executes whatever a commenter writes,
and leaves the lint green while it does.

### actionlint

| | |
| --- | --- |
| Line | **none for actionlint's own rules.** A `[shellcheck]` finding takes shellcheck's own `# shellcheck disable=SCxxxx` inside the `run:` script |
| Config | `.github/actionlint.yaml`, `paths:` mapping a glob to `ignore:` regexes matched against the message text (v1.7.4 and later) |
| CLI | a repeatable `-ignore <regex>`, which applies to every file the run covers - broader than the config, so prefer `paths:` |
| Unused | none |

Findings tagged `[shellcheck]` are shellcheck's, forwarded, so they take
shellcheck's inline escape and the next-complete-command scope that comes with
it. That is a rung 1 or 2 suppression where `paths:` would have been rung 3:

```yaml
      - run: |
          files="a b"
          # shellcheck disable=SC2086
          ls $files    # suppressed
          cp $files /tmp  # still reported
```

For actionlint's own rules there is no inline form, so the smallest available
suppression is a config entry keyed to one path and one message. That is still
narrow if the regex is specific:

```yaml
paths:
  .github/workflows/probe.yml:
    ignore:
      - 'property "nosuchinput" is not defined'
```

Other findings in the same file keep firing, including other occurrences of the
same rule with a different message. A regex like `'is not defined'` would not -
write the longest pattern that matches the one message.

actionlint resolves `paths:` keys relative to the repository root and matches
them against the path it was handed, so the config applies only when the tool
runs from the root. The same file linted from a subdirectory reports every
finding the config was meant to silence.

### gitleaks

| | |
| --- | --- |
| Line | `gitleaks:allow` anywhere on the line - it is matched as text, so it does not have to be inside a comment |
| Config | `.gitleaksignore`, which takes fingerprints from the report - see gitleaks' own docs for its shape |

**A confirmed real credential is not a suppression case at all**, at any rung -
see the hard rule in [the skill](../SKILL.md#hard-rules). The fingerprint form
is where that goes wrong, because it *is* the narrowest thing gitleaks offers:
after deleting the key in a new commit fails to clear a scan over a commit
range, a `.gitleaksignore` entry is the next step the ladder seems to point at,
and it turns a live leak green.

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

**No form has a reason slot.** Everything after the directive is parsed as more
rule names, aliases and tags, so prose silently widens the suppression instead
of annotating it. `<!-- markdownlint-disable-next-line MD034 - code links stay
bare -->` also disables every rule tagged `links`, which takes MD042 with it;
the same directive without the prose reports MD042 as normal. Nothing warns.

Put the reason in its own comment on the line above:

```markdown
<!-- Code references in this file are deliberately bare. -->
<!-- markdownlint-disable-next-line MD034 -->
```

agent-kit's `.markdownlint.yaml` is the model for a rung-5 entry: every
disabled rule carries a comment saying what about that repository's Markdown
makes the rule wrong, not that it was noisy.

### yamllint

| | |
| --- | --- |
| Line | `# yamllint disable-line rule:line-length` |
| Block | `# yamllint disable rule:line-length` ... `# yamllint enable rule:line-length` |
| File | `# yamllint disable-file`, **alone on the first line of the file** |
| Config | `.yamllint` |

**No yamllint directive can carry a reason**, at any scope. The parser anchors
each one at end-of-line, so trailing text - including a second `# comment` -
stops it matching, silently: no error, no warning, and the finding still
reported. That holds for `disable-line`, for the `disable`/`enable` pair, and
for `disable-file`.

`disable-file` has a second rule on top of it: it is honoured only as the very
first line, so it goes *above* the `---` that opens most workflow and config
YAML. One line lower and it does nothing.

Put the reason on the line below the directive:

```yaml
# yamllint disable-file
# Generated by <tool>; reflowing it would lose the upstream diff.
---
one: <a line over the limit>
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
