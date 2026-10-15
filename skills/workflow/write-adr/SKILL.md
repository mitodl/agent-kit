---
name: write-adr
description: >
  Record a significant engineering decision as an Architecture Decision Record
  in the repository's ADR directory, following the convention that repository
  already uses. Use this skill when scoping has found a decision worth
  recording — more than one mechanism worth weighing, a schema or API contract
  change, a migration, a frontend/backend split, anything touching auth or
  payments — or when asked to "write an ADR", "record this decision",
  "document why we chose", or "ADR this". Detects the repo's existing ADR
  flavor rather than imposing one, allocates the next number from the
  filesystem, scopes the record to the files it governs, and leaves it
  Proposed for a human to accept.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Write an ADR

An ADR exists so that a decision made once stays legible to someone who was
not in the room. **One ADR per decision** — not per ticket. A piece of work
produces zero, one, or several, and zero is a normal outcome.

| File | Purpose |
|------|---------|
| [`references/adr-template.md`](references/adr-template.md) | The template and a full worked example |
| [`references/repo-flavors.md`](references/repo-flavors.md) | Detecting which convention a repo uses, and its extra obligations |
| [`references/revision-protocol.md`](references/revision-protocol.md) | Addendum vs supersede, and the status lifecycle |
| `scripts/next-adr-number.sh` | Next number from the filesystem, plus a consistency check |
| `scripts/adr-index.sh` | Derive the index table; verify it against README |

## Entry conditions

Entered from
[`plan-technical-approach`](../plan-technical-approach/SKILL.md), **after** its
alternatives have been generated and scored. That skill supplies the chosen
mechanism, the rejected ones with the condition that would flip each, and the
measured baseline that `scope-work` established.

Invoked without those, go there first. `## Options Considered` is the output of
scoring, so an ADR written before it records a preference rather than a
decision — and an ADR written before the code has been read records neither.

---

## Step 1 — Does this decision deserve an ADR?

Write one when **all** of these hold:

- It is architecturally significant: it affects structure, an external
  interface, a quality attribute, or is expensive to reverse.
- Someone who was not in the room will need to understand *why*.
- There was a real trade-off. If one option was obviously right, there was no
  decision to record.

Skip it when the choice is already settled by a linter, a style guide, or an
existing ADR; when it is local to one function; or when it is a temporary
workaround with no architectural consequence. ol-infrastructure's own bar is
the useful one: *"If future you (6 months later) would ask 'Why did we do it
this way?', write an ADR."*

**Zero ADRs is a normal outcome.** A trivial change writes none and proceeds
straight to implementation. Writing one anyway teaches the next reader that
ADRs here are ceremonial, which is how a convention dies.

Several decisions in one piece of work get several ADRs, each standing alone.
Do not bundle unrelated decisions into one record to save effort — the next
reader needs the one that applies to them, not a digest.

---

## Step 2 — Find the directory and detect the flavor

Full procedure in [`references/repo-flavors.md`](references/repo-flavors.md).
In short: enumerate candidate directories, discard generated mirrors, and
fingerprint the convention from the **newest** files, because the oldest ones
often follow a template their authors have since abandoned.

```bash
git ls-files | grep -iE '(^|/)(adr|adrs|decisions?)/' | sed 's|/[^/]*$||' | sort -u
```

**No ADR directory anywhere:** create the standard one — `docs/adr/` in a repo
that keeps documentation under `docs/`, otherwise `adr/` at the root. Flat,
numbered files in one directory; no per-work-item subdirectory. Seed it in the
same change with `template.md` and a `README.md` carrying the index table, so
the first ADR arrives as an established convention rather than a stray file.

**Follow the flavor you find.** Imposing one repo's format on another is how a
directory ends up with two conventions and no convention.

---

## Step 3 — Allocate the number

```bash
./skills/workflow/write-adr/scripts/next-adr-number.sh <adr-dir>
```

From the filesystem, never from the index — every hand-maintained pointer in
these repos has drifted, including the index itself. The script also reports
duplicate numbers and filename/H1 mismatches; both already exist in
ol-infrastructure, so expect warnings about files you did not touch and leave
them alone.

Numbers are permanent. Never renumber to close a gap.

---

## Step 4 — Write it

Template and worked example in
[`references/adr-template.md`](references/adr-template.md).

**Scope it with `applies_to`.** Frontmatter carries the file globs this
decision governs, which is how implementation finds the ADRs relevant to a
change without reading all of them:

```yaml
---
applies_to:
  - "src/ol_infrastructure/applications/apisix/**"
---
```

Two rules, both load-bearing:

- **`:(glob)` semantics, always.** Git pathspecs answer differently depending
  on form — `git ls-files -- 'mcp/*.py'` returns 211 files because `*` crosses
  `/`, while `:(glob)mcp/*.py` returns 0. Write subtree scope as `dir/**`,
  never `dir/*`.
- **Every entry must match at least one tracked file, checked before writing:**

  ```bash
  git ls-files -- ":(glob)$entry" | grep -q . || echo "glob matches nothing: $entry"
  ```

  A glob matching nothing fails silently and in the dangerous direction — the
  ADR never surfaces and implementation proceeds as if no decision governed the
  files. Do not write an ADR with an unverified glob.

**Record the alternatives, with the condition that would flip each.** "Rejected
because X" is a verdict; "would have won if Y" is reviewable. The second is
what makes an ADR useful when the situation changes.

**`**Deciders:**` names a human**, from the issue assignee or `git config
user.name`. An ADR whose decider is a model name has no decider.

### Enforcement, in order of preference

1. **If the repo has a test suite that can assert the invariant, write the
   test**, put it in the suite, and name it in `## Decision`. ol-infrastructure
   ADR 0013 does this and its test back-links to the ADR — that runs in CI and
   fails the pull request.
2. **Only if no suite can carry it**, add a `## Verify` section with a shell
   check, and say in that same section that nothing runs it automatically.
3. **If the decision has no checkable invariant, omit the section.** Most
   decisions decide the shape of a system and have none. A section that is
   filler more often than not teaches readers to skip it.

A `## Verify` command that ships must **assert the violating state, not the
keyword**, and must exclude the ADR directory or it matches its own prose:

```bash
# Wrong: `bearer_only: false` IS compliance, and this reports FAIL on it.
grep -rnE 'bearer_only' src/

# Right: asserts the violation, and cannot match the ADR's own text.
grep -rnE 'bearer_only:[[:space:]]*true' src/ ':(exclude)docs/adr' \
  && echo "FAIL: gateway-side bearer validation enabled" || echo "OK"
```

Run it before writing the ADR and require it to pass on compliant code. A check
that cries wolf gets ignored, and so does the real violation it later catches.

---

## Step 5 — Status, and who may accept

**Write `**Status:** Proposed`.** Never `Accepted` — that is a human's call,
and an agent writing "Accepted" next to "human validation pending" is a
contradiction that has already shipped twice in ol-infrastructure.

Add the pending Review History row:

```markdown
| 2026-10-14 | _Pending_ | _Pending_ | Created by AI agent, pending human approval |
```

Acceptance happens later, mechanically:
[`implement-plan`](../implement-plan/SKILL.md) flips it when the implementation
pull request merges with a human approving review. See
[`references/revision-protocol.md`](references/revision-protocol.md).

---

## Step 6 — Update the index, then commit with the code

```bash
./skills/workflow/write-adr/scripts/adr-index.sh --verify <adr-dir>
```

Add the new row. If the script reports drift in rows you did not touch, **leave
them and say so** — fixing pre-existing drift inside an unrelated pull request
reads as unexplained noise. That is its own small change.

The ADR commits **with the first implementation pull request**, not as a
separate gating one. Some repos have further obligations — agent-kit needs a
regenerated docs mirror and a nav entry, and skipping either fails CI. Those
are listed in [`references/repo-flavors.md`](references/repo-flavors.md).

---

## Keeping it true

A decision that changed and an ADR that does not say so is worse than no ADR:
it is confidently wrong and it is believed. When implementation diverges from a
recorded decision, [`implement-plan`](../implement-plan/SKILL.md) stops, gets
confirmation, and appends a dated addendum — it does not quietly rewrite the
decision. The protocol, including when to supersede instead, is in
[`references/revision-protocol.md`](references/revision-protocol.md).
