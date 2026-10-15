# Chunking the work into pull requests

How [`plan-technical-approach`](../SKILL.md) decides whether approved work ships as
one pull request or several, and where the boundaries fall when it is several.

**The default is one PR.** Most bugs and most small features are one PR, and
saying so takes one line. Splitting has real costs — rebase churn, review
latency, a trunk that passes through intermediate states — so it has to be
bought with a reason. What follows is how to decide, not a mandate to split.

## When to split

Split when at least one of these is true. If none is, it is one PR.

- **The work crosses a deployable boundary.** Separate repos, or separately
  deployed services, cannot merge atomically anyway. The split already exists;
  the plan just has to acknowledge it and order it.
- **It busts the size budget.** 500 changed lines is the soft limit, 1,000 the
  hard one — see [Size budget](#size-budget). Size is what forces the question;
  it never answers it. A chunk over budget still has to be cut along one of the
  axes below, not at the line where the counter crossed 500.
- **A reviewer cannot hold it in their head.** Independently of the count,
  reviewing the whole diff may require holding two unrelated models at once
  ("is the migration right" *and* "is the serializer right"). A 300-line diff
  can fail this while an 800-line mechanical rename passes it.
- **Part of it is independently valuable.** If chunk 1 is worth merging even if
  chunk 2 is abandoned, it should not wait for chunk 2.
- **Part of it carries materially different risk.** A data migration and a
  template change do not deserve the same review attention or the same revert
  story; bundling them means the cautious one drags the trivial one.
- **A shared contract has to land before its consumer.** Schema columns, new
  env vars, a published package export, a new endpoint — the producer deploys
  first, then the consumer. This is a sequencing fact, not a preference.

## Size budget

| Threshold | Meaning |
|-----------|---------|
| **≤ 500 changed lines** | The target. No justification needed. |
| **500–1,000** | Soft limit exceeded. Either split it, or state in the plan why this chunk is coherent at this size. Being mechanical, or being one migration that cannot be partial, are good reasons; "it's all related" is not. |
| **> 1,000** | Hard limit. Split it, or get an explicit bypass from the engineer. Never bypass silently. |

**What counts.** Added plus deleted lines in the chunk's own diff, excluding
lockfiles and generated paths — a regenerated lockfile or a compiled bundle is
not review surface. Use the repo's own exclusions, adding any other generated
paths it has (`top` anchors each pattern at the repo root; without it they
resolve against the current directory and silently match nothing):

```bash
git diff --shortstat origin/<base>...HEAD -- ':/' \
  ':(top,exclude)*.lock' ':(top,exclude)*-lock.*'
```

**At planning time this is an estimate, and estimates are wrong.** Chunks are
sized from the touch-points measured while grounding — files to change, call
sites to update, tests to add. Say it is an estimate. Then re-measure during
implementation with the command above: a chunk that was estimated at 400 and is
passing 900 is a trigger to stop and re-chunk, not a number to quietly exceed.
That re-chunk is a plan change, so it updates the plan comment like any other.

### Bypassing the hard limit

Sometimes 1,000 lines genuinely cannot be cut: a generated-file update that is
meaningless in halves, a framework upgrade whose call-site changes must land
atomically, a single migration touching many models at once. The bypass exists
for those, and only those.

Asking for it requires naming three things, because "it's unavoidable" is a
claim the engineer is being asked to accept:

1. **Every axis you tried**, and the specific reason each one produced chunks
   that fail one of [the three tests](#the-three-tests-every-chunk-must-pass) —
   usually independent correctness.
2. **The estimated size**, and how much of it is mechanical or generated versus
   hand-written. A 1,400-line chunk that is 1,200 lines of codemod output is a
   very different review than one that is not.
3. **What makes it reviewable anyway** — a commit sequence a reviewer can walk,
   the command that generated the mechanical part, or the single invariant that
   makes the whole thing one idea.

The engineer approves or refuses. An approved bypass is a tradeoff, so it gets
a ledger row like any other: giving up reviewability at the margin, to get
atomicity, revisit when the thing being upgraded next moves. **Never assume the
bypass.** If the engineer has not said yes, the answer is to keep looking for
an axis.

## Valid split axes

A split must follow a line that already exists in the system. These are the
ones that do.

### Service or deployable boundary

Each separately deployed thing gets its own PR. Backend and frontend are the
common case, but so are: a Django service and the Pulumi stack that deploys it,
a library and the application consuming it, two repos in a cross-repo change.

Order follows deploy order, which is usually producer before consumer.

### Functional layer within one service

A backend feature large enough to split usually splits along its own layers,
in dependency order:

1. **Data model** — migrations, model definitions, constraints. Lands and
   deploys first because everything else reads it.
2. **Domain or service logic** — the behavior, with its unit tests.
3. **API surface** — serializers, viewsets, routes, permissions.
4. **Consumers** — tasks, management commands, the frontend call site.

Not every feature has all four. Use the ones it actually has, and say which
ones were merged into a single chunk and why.

### Contract-first (expand / migrate / contract)

When a change alters something two sides agree on, the sequence is forced:

1. **Expand** — add the new column, field, env var, or endpoint alongside the
   old one. Nothing reads it yet, so this is safe to deploy alone.
2. **Migrate** — move readers and writers over, backfill if needed.
3. **Contract** — delete the old one, once nothing references it.

Collapsing these into one PR is the classic cause of a deploy-ordering
incident: the consumer ships before the producer and errors until the second
deploy lands.

### Mechanical separated from semantic

A large rename, move, or codemod goes in its own PR, with **no behavior
change**, so the behavior diff is reviewable instead of buried in 2,000 lines
of churn. Say in the PR description that it is mechanical and how it was
generated, so a reviewer can check the method rather than the output.

### Risk isolation

A migration, a backfill, a dependency bump, or a feature-flag introduction
separates from the code that uses it, so each can be reverted without taking
the other down.

## Not valid axes

These produce chunks that are extra process with no reviewability gained:

- **Diff size, as a *boundary*.** The budget decides *whether* to split; it
  never decides *where*. Cutting at the line where the counter crossed 500
  slices through the middle of a change as readily as along its seams. An
  over-budget chunk is cut on one of the axes above, and if no axis yields
  chunks under budget, that is the bypass case below — not a licence to cut
  anywhere.
- **Directory, for its own sake.** A directory is only a valid boundary when it
  happens to coincide with a service or layer boundary. Often it does not.
- **"Part 1 / Part 2."** If a chunk needs an ordinal to describe it, it has no
  independent meaning, and the split is arbitrary.
- **Tests split from the code they test.** A PR that adds untested code and a
  later PR that tests it means the first one was never reviewable.
- **One PR per file, or per commit.** These are authoring artifacts, not
  functional lines.

## The three tests every chunk must pass

A proposed chunk is only a chunk if all three hold. If one fails, merge it back
into its neighbor.

1. **Independently reviewable.** A reviewer can judge it without reading the
   next chunk. If the answer to "is this correct?" is "depends what chunk 3
   does", the boundary is in the wrong place.
2. **Independently correct.** Trunk is deployable after it merges. Dead code
   behind a flag is fine; a half-migrated read path is not.
3. **Independently revertable** — or explicitly declared not to be. Some chunks
   genuinely cannot be reverted alone once data has moved. That is allowed, but
   it has to be stated in the plan rather than discovered during an incident.

## Stacked or independent

Say which, because they are worked differently:

- **Stacked** — each PR targets the previous one's branch (`gh pr create --base
  <previous-branch>`), and they merge in order. Use when chunk N genuinely
  needs chunk N-1's code. Keep stacks short: every PR in a stack rebases when
  one below it changes, so a four-deep stack costs more than it saves unless
  the chunks are large.
- **Independent** — each targets the default branch and they can land in any
  order, or in parallel. Prefer this whenever the chunks do not actually
  depend on each other; it is strictly cheaper.

A chunk that depends on another *deploying* (not merging) is a third case:
independent PRs with a stated deploy order. Say that explicitly — a reviewer
cannot infer it from the diff.

## Recording it

**Where the plan lives**: a Delivery section in `TECHNICAL.md` for spec'd work,
or in the plan comment for fast-path work — see
[`plan-comment-format.md`](plan-comment-format.md). One or the other, never
both; two chunk tables drift.

**In witan**, when there is more than one chunk, mirror the shape:

- `task_create(type="epic", ...)` for the whole piece of work, carrying the
  issue URL as `external_uri`.
- One child per chunk: `task_create(..., parent="<epic slug>")`.
- For a stack, record the order: `task_link(from_slug="<chunk-1>",
  to_slug="<chunk-2>", kind="blocks")`. Closing a blocker automatically makes
  its dependents eligible in `task_ready`, so the stack order stays visible to
  anyone looking for work rather than living only in the plan comment.

A single chunk needs no epic — one task is the whole thing.

## Worked example — one PR

> Fix a `None` crash in `parse_window`.

One chunk, ~30 lines. One function, one caller, one regression test. Well
under budget, no deployable boundary, no contract change, nothing independently
valuable to separate out, and a reviewer holds the whole thing at once. Say
"single PR, ~30 lines" and move on — the Delivery section is one line.

## Worked example — split along service lines

> Add saved-search notifications: a new Django model and API, plus the frontend
> UI that consumes it.

Three chunks, independent PRs with a stated deploy order:

| # | Chunk | Repo | Axis | Est. LOC | Depends on |
|---|-------|------|------|----------|------------|
| 1 | Model, migration, and the notification-building service | `mit-learn` | Functional layer — data model | ~350 | — |
| 2 | API: serializer, viewset, route, permissions | `mit-learn` | Functional layer — API surface | ~250 | 1 merged |
| 3 | Saved-search UI calling the new endpoint | `mit-learn` frontend | Service boundary | ~400 | 2 **deployed** |

Chunk 3's dependency is on a deploy, not a merge — the frontend 404s against a
backend that has not shipped the route yet. That is exactly the fact a reviewer
cannot see in the diff, so it is written down.

Chunks 1 and 2 are separate because the migration carries different risk from
the serializer, and because 1 is independently valuable: the notification
service is usable by a management command even if the API slips. Note that
together they are ~600 lines, over the soft limit — which is what prompted the
question — but the boundary between them is the layer line, not the 500th
line. Each chunk lands comfortably under budget on its own.

## Worked example — contract-first

> Rename `Course.runs` to `Course.course_runs` across a shared package and its
> three consumers.

Four chunks, stacked within the package and independent across consumers:

1. **Expand** — add `course_runs` as the canonical field, keep `runs` as a
   deprecated alias that reads through to it. Deploy alone, safely.
2. **Migrate consumers** — three independent PRs, one per consuming repo, each
   moving to `course_runs`. They can land in any order and in parallel.
3. **Contract** — remove the `runs` alias, once all three consumers have
   **deployed**, not merged.

The mechanical rename inside each consumer is its own commit within that
consumer's PR rather than its own PR, because each consumer's diff is small
enough to review whole. Splitting further would be an ordinal split.
