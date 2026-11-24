---
name: lint-exceptions
description: >
  Satisfy a lint finding instead of silencing it, and when a suppression really is
  the right answer, add the narrowest one the tool offers with a reason that says
  why. Use this skill in any language whenever a linter, formatter-adjacent check,
  or type checker fires and the response under consideration is a `noqa`,
  `type: ignore`, `biome-ignore`, `eslint-disable`, `@ts-expect-error`,
  `@ts-ignore`, `shellcheck disable`, `zizmor: ignore`, `gitleaks:allow`, or an
  edit to `ignore` / `exclude` / `disable` / `per-file-ignores` / `overrides` in a
  lint config. Also use it on "make the linter pass", "just ignore it", "add an
  ignore for", "suppress this warning", "exclude this file from the check", "the
  hook is complaining", and when reviewing a diff that adds a suppression.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Lint exceptions

A linter fired. The fastest route to green is to silence it, and the broadest
silence is the fastest of all - which is why it is the usual mistake. A broad
exemption also covers code nobody has written yet, so the next violation in that
tree arrives pre-silenced.

**Default: satisfy the rule. A suppression is a last resort that has to be argued
for, at the narrowest scope the tool offers, in writing, next to the code.**

Nothing below is specific to one language. Every linter worth using has the same
five parts: a diagnostic, a rule identifier, an inline escape, a file-or-glob
scope, and a config. Find those five in whatever tool just fired and the rules
here apply unchanged. For the exact spelling in a given tool, see
[references/ecosystems.md](references/ecosystems.md) - look the syntax up there
rather than reaching for a config change because the inline form would not come
to mind.

## 1. Satisfy the rule

**A suppression is a claim that the linter is wrong about this code.** Before
making that claim, establish that the code cannot be written so the rule does not
fire.

### Read the rule's own documentation first

Every mainstream linter publishes a page per rule, and that page names the fix it
wants. Open it: `ruff rule S603`, `biome explain noExplicitAny`, the wiki link
shellcheck prints under each finding, the rule page an eslint message names. This
step is thirty seconds and it is the step that gets skipped.

### Then land on one of three outcomes

These are outcomes, not scopes. Two of them send you to section 2, where the
scope gets chosen.

| Outcome | What it means | What to do |
| --- | --- | --- |
| **The rule is right** | The code has the defect the rule describes | Fix the code. No suppression |
| **The rule is right; the fix is not available here** | A real constraint - an external API's shape, a platform quirk, a deliberate trade-off | Suppress at the narrowest scope. The reason states the constraint |
| **The rule is wrong here** | The rule's premise does not hold for this code | Suppress at the narrowest scope. The reason states why the premise fails |

There is a fourth thing that happens, and it is not an outcome: **the fix looked
like more work than the suppression.** If that is the real reason, the honest
move is to do the work, or to say out loud that the fix is being deferred and
why. It never justifies an exemption on its own.

A finding that is real but outside the change in hand - a pre-existing violation
a new rule or a moved file has just surfaced - is the one case where deferring is
right. Defer it visibly: say which findings you are not fixing and why, and leave
them failing or open an issue. Do not convert "not mine to fix today" into a
permanent exemption, which is what a config entry is.

### Turning the check off is not one of the outcomes

A suppression at least leaves a record at the site, with a rule identifier and a
reason a reviewer can argue with. These leave nothing, and none of them is on
the table above:

| Bypass | What it actually does |
| --- | --- |
| `SKIP=<hook>`, `--no-verify` | Commits without running the hook. Either CI fails instead, or CI does not run that hook either and nothing ever checks it |
| `--exit-zero`, or swallowing a hook's exit code | The tool keeps printing findings and stops failing on them, so the check is green and nobody reads the output |
| `continue-on-error: true` on a workflow step | The same, one layer up: the job passes with the step red |
| Deleting the test, the file, or the hook | Not a scope question |

If a check genuinely has to be bypassed to land something - a hook broken
upstream, a service outage, a release that cannot wait - say so where the person
reviewing it will see it, and name what is going unchecked and when it gets
checked. A bypass nobody mentioned is indistinguishable from a check that
passed.

### Fixes that get skipped

Each of these is a finding an agent routinely silences when the rule was simply
right. The shapes are language-independent; the parenthetical names one rule that
produces it.

| Finding | The fix that was available |
| --- | --- |
| Shelling out with interpolated arguments (`S603`/`S607`, `SC2086`) | Make the arguments literal, quote the expansion, resolve the binary explicitly |
| A naive timestamp (`DTZ005`) | Pass the timezone the call already accepts |
| An escape-hatch type (`noExplicitAny`, `no-explicit-any`) | Use the unknown-equivalent and narrow, or write the type the value actually has |
| An unawaited async call (`no-floating-promises`) | Await it, or mark it discarded in the way the rule documents |
| An unused parameter or variable (`ARG001`, `no-unused-vars`) | Use the underscore prefix the rule already exempts, or delete it |
| A broad catch (`BLE001`, `E722`) | Catch the exception type you can actually handle |
| A mutable default or shared state (`B006`) | Construct it inside the function |
| An assertion in shipped code (`S101`) | Raise the error the caller should see |

If the finding is in code being added right now, the fix is nearly always
cheapest here, before anything depends on the current shape.

## 2. If it must be suppressed

**Suppress at the narrowest scope the tool offers, and say why in the same
place.**

### The ladder

Each rung requires the one above it to be genuinely unavailable.

| Rung | Scope | When it is right |
| --- | --- | --- |
| 0 | **Fix the code** | Section 1. The default |
| 1 | **The line** | One violation at one site |
| 2 | **The block** - a scoped disable/enable pair | A handful of adjacent sites, one shared reason |
| 3 | **The file**, named exactly - not a glob that happens to match it | The whole file has the property (a test module where shelling out is the point) |
| 4 | **A glob** | The property is true of the *tree*, and stays true of files not yet written |
| 5 | **Off for the project** | The rule's premise is wrong here in general |

Rung 4 is the one that gets reached for too early. The test for it is not "does
this glob match my file" - it is **"will the next file added under this glob also
deserve the exemption?"** If the answer is no, or unknown, the glob is wrong and
a line, a block, or a named file is right.

### A directive at the top of a file is rung 3, whatever it looks like

Most tools have a form that exempts the **whole file**, and it is usually one
word away from the line form. Writing it because it was the first spelling that
came to mind, or because it was easier than finding the right line, opts every
present and future violation in that file out of the check - including the ones
nobody has written yet.

| Tool | The form that takes the whole file |
| --- | --- |
| ruff | `# ruff: noqa` or `# ruff: noqa: CODE` anywhere in the file |
| mypy | `# mypy: disable-error-code="..."` at the top |
| biome | `// biome-ignore-all lint/group/rule: reason` |
| TypeScript | `// @ts-nocheck` |
| eslint | `/* eslint-disable rule */` with no matching `enable` |
| shellcheck | a `# shellcheck disable=SCxxxx` placed **before the first command** |
| yamllint | `# yamllint disable-file` on line 1 |
| markdownlint | `<!-- markdownlint-disable-file RULE -->` |

shellcheck's is the one that bites, because the same directive means two
different things depending on where it sits: before the first command it covers
the file, and anywhere else it covers the next complete command - which for a
loop or a function is its whole body, not one line. A directive written for one
expansion and parked at the top of the script silences every expansion in it.

Before writing any of these, answer the rung-3 question out loud: **does the
whole file have the property that makes the rule wrong?** A test module whose
entire point is shelling out does. A 300-line module with one unusual call does
not - that is a line. If the file is long enough that you cannot say, it is a
line.

A file-level directive earns the same reason as any other suppression, and it
is the one most likely to be read by someone who has no idea why it is there.

### Hard rules

- **Never a bare suppression where the syntax takes a rule identifier.** An
  escape with no identifier silences everything at that site, including the next
  bug, and it survives the rename of the rule it was meant for. A few escapes
  take no identifier at all - `@ts-expect-error`, `gitleaks:allow`,
  `# yamllint disable-file`. Those are *broad by construction*, not an exception
  to the rule: name the diagnostic you mean in the reason, and keep what they
  cover as small as the tool allows.
- **A confirmed real credential is never suppressed, at any rung.** When a
  secret scanner fires on a live key, the scope ladder does not apply - none of
  its rungs are available. Deleting the key in a later commit does not clear the
  finding when the scan covers a commit range, which puts the next step one
  click from a `.gitleaksignore` fingerprint or a `gitleaks:allow` that makes
  the check green and leaves the key in history and live. Rotate it, and keep
  the credential out of every commit; see `create-ol-pull-request` for the
  procedure. Only a string that is credential-*shaped* and established not to be
  one - a test fixture, a vendor's documented example key - is a suppression
  candidate at all, and the reason says which.
- **Always a reason, inline.** Some tools take a structured one and some do not;
  where the syntax has no slot for it, put a comment on the line above - or
  below, for the rare directive that has to be the whole first line of a file
  and so has no line above it. Check the reason did not break the directive:
  a few tools treat trailing text as part of the directive and silently stop
  suppressing. A suppression with no reason is indistinguishable from an
  accident, and nobody who finds it later can tell whether it is still needed.
- **Prefer the self-expiring form.** Where a tool offers a directive that errors
  once the underlying finding disappears, use it over the one that rots silently.
  Where it offers a check for suppressions that no longer suppress anything,
  recommend turning it on.
- **Prefer "next line" forms over region pairs.** A region someone later writes
  code inside silently covers the new code too.
- **A rewrite that only takes the code out of the rule's range is a
  suppression,** and meets the same bar. `shutil.which("git")` satisfies `S607`
  while reading exactly the same `PATH` the rule objected to - and trades it for
  an `S603`, because the executable is now a variable. A cast silences a type
  error without making the type true. Ask what the rule was checking and whether
  the rewrite changed it; if it did not, the honest version is the inline escape
  with a reason, which at least says so.
- **Never widen an existing exemption to cover a new case** without re-checking
  that the exemption still fits. A new violation inside a tree that already has a
  glob is the moment to narrow the glob, not to file under it.
- **Removing the rule from the enabled set is rung 5, not a shortcut.** It needs
  a comment in the config saying what is wrong with the rule here.
- **Check the repository's convention before inventing one.** A project with no
  config-level exemptions at all and a scattering of inline ones has told you
  which rung it uses. In mitodl/agent-kit, the repository these skills ship from,
  the convention is written into the configs themselves: the `extend-select`
  comment in `pyproject.toml`, the per-rule reasons in `.markdownlint.yaml`, and
  the per-hook `exclude` reasons in `prek.toml`.

### What a reason has to do

It must say **what makes the rule wrong, or the fix unavailable, here** - in
terms a reader can check against the code next to it, without hunting for context
that is not in the diff.

| Not a reason | Why it fails |
| --- | --- |
| `# noqa: S603 - false positive` | Asserts the conclusion. Which premise fails? |
| `# noqa: S603 - linter noise` | Says nothing checkable |
| `# noqa: S603 - needed for tests` | Describes who wants it, not why the rule is wrong |
| `# noqa: S603 - see PR #123` | The next reader is already reading the code, not the PR |
| `# noqa` (bare) | Silences every present and future rule on the line |

A reason that works, from agent-kit itself:

```python
re.compile(
    r"|".join(  # noqa: FLY002 - one alternative per line
```

The `ol-github-workflows` miss is the longer worked example, and the first thing
to notice about it is that most of it was never a suppression problem at all.
The file shelled out through a wrapper:

```python
def git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], capture_output=True, check=False)
```

`S603` and `S607` both fire on that call, and a `# noqa: S603, S607` on it makes
them both go away. It also silences every future caller of `git()`, whatever
they pass - the "covers code nobody has written yet" failure this skill opens
with, written at rung 1 so that it does not look like one.

**`S603` had a code fix.** The rule fires because the argument list holds a
variable, not because the arguments are in a list at all:
`subprocess.run(["/usr/bin/git", x])` is reported and
`subprocess.run(["/usr/bin/git", "status"])` is clean. (Passing a list instead
of a string is the fix for `S602`, the `shell=True` rule - a different finding,
and the one this gets confused with.) That wrapper had a single caller, passing
literal subcommands, so inlining it makes the list literal and `S603` stops
firing with nothing suppressed:

```python
head = subprocess.run(
    # The runner image decides where git lives, so there is no absolute path to
    # pin that stays correct across images.
    ["git", "rev-parse", "HEAD"],  # noqa: S607
    capture_output=True,
    check=False,
)
```

One code, on one line, for the one reason that survives checking. `S607`'s
premise holds - the executable really is resolved from `PATH` - so the reason
says why the fix the rule wants is unavailable rather than pretending the
finding is wrong. That is the second outcome from section 1, and it is the only
one of the two findings that ever reached section 2.

Note what the reason does **not** do: it does not say "fixed arguments" and
leave a reader staring at `*args`. A claim a reader cannot check from the code
in front of them is the same as no reason at all - and once the arguments really
are literal, that half of the reason has no finding left to justify.

## 3. Prove the narrowing

Do not assert that a suppression is correctly scoped. Three checks, all cheap.
The first two use the same technique to answer different questions, which is why
one probe is not enough.

**Does it reach past the scope you wrote?**

> Add a second, deliberately-violating line **just outside the scope you meant
> to exempt**. Re-run the linter. If the probe is **not** flagged, the
> suppression reaches further than its text says. Narrow it. Remove the probe.

"Just outside" is different per rung:

| Rung | Where the outside probe goes |
| --- | --- |
| 1, the line | The next line |
| 2, the block | After the `enable`, or outside the disabled region |
| 3, the file | A second file in the same directory |
| 4, a glob | A path the glob should not match - a sibling directory, or a file one level up |
| 5, off for the project | Nowhere; instead confirm a *different* enabled rule still fires, so you know the config edit did not take the whole rule set with it |

This check catches a scope that is wider than it reads. It cannot catch a scope
that is exactly as wide as written and still the wrong rung: an `"actions/**"`
glob probed from a sibling directory passes it cleanly.

**Is the scope bigger than the violation?** (rungs 3 and up)

> Add a second, deliberately-violating line **inside** the scope, at a site the
> suppression was not written for - a second call in the same file, a second
> file under the same glob. Re-run. If the probe is **silent**, the scope covers
> more than the violation you set out to exempt and the rung is too high.

This is section 2's "will the next file deserve this too" question made
executable, and it is the one the outside probe cannot answer. Expect the probe
to be silent: that silence is the finding, not a pass.

In the `ol-github-workflows` case the inside probe was a second
`subprocess.run` in the same file. Under `"actions/**"` it was silent - the glob
covered the file, not the call - and under the line-level `noqa` it was flagged
immediately. Same probe, same rule, and only one of the two scopes was the size
of the violation.

**Is it needed at all?**

> Remove the suppression. Re-run. Confirm the finding comes back.

A suppression that changes nothing is a suppression that was copied, and it will
outlive whatever once justified it.

All three apply to a config-level change as well. After editing `exclude`,
`ignore`, or a glob, the inside probe is a violating file under the new pattern
and the outside probe is one just beyond it. Run both: a pattern can be too
broad for the violation and no broader than it reads at the same time.

## 4. Editing a lint config

Config changes are rungs 3 to 5, so they carry the heaviest burden of proof.
Before adding or widening a pattern:

1. **Count the sites.** One site is a line. Two or three adjacent ones are a
   block. A tree-wide property is a glob. Match the rung to the count, not to the
   nearest existing entry.
2. **Say it in the config.** Every exemption gets a comment above it naming the
   property that makes it correct, in the same style as the entries already
   there.
3. **State what the pattern now skips.** When a change makes a checker stop
   looking at paths it used to cover - especially a secret scanner or a security
   rule - say which paths those are and why, where the person reviewing it will
   see it. A silently broadened `exclude` is how a scanner ends up covering
   nothing.

## 5. Reviewing someone else's suppression

A suppression added in a diff gets the same scrutiny as the code it silences.
Four questions, in order:

1. Was the fix considered? If the rule is plainly right, say so and ask for the
   fix.
2. Is there a reason, and is it checkable against the code in the diff?
3. Is the scope the narrowest one available, or is a glob doing work a line would
   do? Ask the "will the next file deserve this too" question out loud.
4. If the suppression widens an existing exemption, does that exemption still fit
   its original purpose?

This applies when working through review feedback as well: a reviewer asking to
make a check pass is not asking for the check to be turned off.
