# The spec template

The shape [`write-spec`](../SKILL.md) writes. Derived by reading the 43
documents in `ol-infrastructure/docs/plans/` (29) and
`agent-kit/docs/internals/design/` (14) — not invented. Where the directory you
are writing into disagrees with this file, **the directory wins**; read two or
three of its newest files first.

`ol-data-platform/docs/specs/` and `docs/design/` hold 8 more, per the review
that prompted this skill; they are not counted below.

## What is actually consistent

Counted across those 43 files, so you know which parts are convention and which
are this document's preference. The counts cover every document in those two
directories, analyses and spike results included — 11 of the 29 and 11 of the
14 are named `*-spec.md`:

| Element | Prevalence |
|---|---|
| `tk-` task slugs referenced inline | 30 of 43 |
| A `Goal` / `Problem` / `TL;DR` opener | near-universal, under many names |
| Numbered top-level sections (`## 1. Goal`) | 21 of 43 — follow the directory |
| A `— Spec` / `— spec` title suffix | 10 of 43, plus 3 as `— implementation spec` |
| Sequencing / Order of work / Phasing | near-universal |
| Open questions | common, and the ones that lack it tend to regret it |
| YAML frontmatter | **none** — the header is prose |

Two things vary enough that you should copy the neighbours rather than this
file: section numbering, and whether decisions live in one `## Decisions`
section or get one `## Dn.` section each.

## The skeleton

```markdown
# <subject> — Spec

Status: **spec** (design/track only — nothing here is implemented yet)
Project: `wp-<slug>`
Basis: [`<discovery-doc>.md`](./<discovery-doc>.md) (findings A1–C5), and
<the originating issue>

All source anchors below are against `origin/main` @ `<short sha>`.

<One paragraph: what this document turns into what. "This spec turns the
evaluation's what/why into concrete how: shared modules, exact signatures,
and the migration.">

## 1. Goal

What is true when this is done, in a few lines. Not a restatement of the
issue — the issue says what hurts, the goal says what the end state is.

## 2. Measured baseline

The numbers the design rests on, from scoping: caller counts, touch points,
latencies, row counts. A spec whose design rests on an adjective is a guess
with headings.

## D1. <the decision> (decided)
## D2. <the decision> (open — needs <who>)
## D3. <the decision> (deferred to P2)

One section per decision, numbered so it can be argued with individually.
Each carries: the decision, the alternative that lost, and the condition
that would have flipped it.

## <N>. The design

The body. Per area or per component, naming exact paths and symbols so an
implementer can diff against the tree at the pinned sha. This is where a
spec earns its length; everything else is scaffolding.

## Sequencing

1. **<phase>** — `tk-<slug>`
   Done when: <a condition that can be checked, not asserted>.
2. **<phase>** — `tk-<slug>` (blocked by 1)

Referencing the tasks plan-technical-approach already created, not forking
a second DAG.

## Verification

How anyone knows each phase worked. Commands and observable conditions.

## Non-goals

What a reader will otherwise assume is coming. Prevents more scope creep
than any other section.

## Open questions

Each with who can answer it. An open question with no owner is a wish.

## Risks

Where this fails in a way the sequencing cannot absorb, and what is done
about it.
```

## What each section owes the reader

**Goal** — the end state, not the complaint. If it could be pasted into the
issue without anyone noticing, it is not a goal yet.

**Measured baseline** — carried from `scope-work`, not re-derived. It is also
what makes a later reader able to tell whether the design still applies: the
numbers changed, so the conclusion might have.

**The `Dn.` sections** — see [the skill](../SKILL.md#step-3--number-the-decisions).
The state marker in the heading is not decoration; it is how a reviewer knows
which sections they are being asked to settle rather than check.

**Sequencing** — phases with checkable done-conditions, each linked to its
task. "Tests pass" is not a done-condition; "every view reads through the
shared layer and no view queries the store" is.

**Non-goals** — write this even when it feels obvious. The specs in the corpus
that have it are noticeably the ones that stayed the size they started.

**Open questions** — this section shrinking to empty is what moves the status
from "spec" to "implementation-ready". Track it, rather than quietly deleting
entries as they resolve: an answered question belongs in the `Dn.` section it
settles.

## The commit anchor

```markdown
All source anchors below are against `origin/main` @ `8af6c62`.
```

The most-skipped line and the most-missed one. A spec quotes paths and line
numbers; without a sha they rot silently, and an implementer six weeks later
cannot tell whether the spec is wrong or the code moved. Get it from:

```bash
git rev-parse --short origin/main
```

Re-anchor when the spec is substantially revised, and say which sha in the
revision. Do not re-anchor silently: the anchor is a claim about which tree the
line numbers were true for.

## Companion documents

Specs in these directories routinely come in pairs — a discovery or spike
document that establishes the facts, then the spec that turns them into a
design. `graph-structured-memory.md` / `graph-structured-memory-spec.md` is the
pattern, and the spec's §0 resolves the discovery's open questions one by one.

Where a discovery document exists, **link it as `Basis:` and resolve its
questions explicitly**, by its own item ids. A spec that silently re-decides
what discovery already settled wastes the discovery.

Do not fold the two into one document. The discovery is evidence and stays
true; the spec is a plan and stops being true the moment the work ships.
