# Where a spec goes

`docs/specs/`, resolved against the right root. That is the whole rule.

This file used to detect which of four directories a repo happened to use.
That produced `docs/plans/` in ol-infrastructure, `docs/specs/` and
`docs/design/` in ol-data-platform, `docs/rfcs/` in mit-learn, and an ADR with
nowhere to live at all — and it put a spec for already-agreed work into a
directory of RFCs, which in this team's taxonomy are proposals needing
agreement from *outside* the team. Different audience, different lifetime,
different route ([`create-ol-rfc-discussion`](../../scope-work/references/complexity-triage.md)),
same folder. Detection cannot tell those apart; only a convention can.

| Directory | Holds |
|---|---|
| `docs/specs/` | The plan for one body of work, plus its `<slug>-deviations.md` |
| `docs/adr/` | One decision per file, immutable once accepted — see [`write-adr`](../../write-adr/SKILL.md) |

Nothing else is a sanctioned destination for a design document. If what you
have is neither, see [Findings are not files](#findings-are-not-files) below —
it probably already has a home.

## Step 1 — Resolve the root, which is not always the repository

**The structure inside `docs/` is fixed. Which `docs/` is not.** In a monorepo
a package owns its own documentation: this repo keeps witan's ADRs at
`mcp/servers/witan/docs/adr`, and a rule that said "repository root" would
scatter them to a top level that knows nothing about them.

**Take the nearest ancestor that already has a `docs/` directory**, walking up
from a file this work changes:

```bash
d=$(dirname "<a file this work touches>")
while [ "$d" != "." ] && [ "$d" != "/" ]; do
  [ -d "$d/docs" ] && { echo "$d"; break; }
  d=$(dirname "$d")
done   # falls through to the repo root, which is the common answer
```

**Owning documentation, not being a package, is the test** — and the
difference is not academic. `mit-learn` carries a `package.json` at the root,
at `frontends/`, *and* at `frontends/main/`, so "nearest package root" would
file a site-wide CSP spec under `frontends/main/docs/specs/` because most of
the diff happened to be frontend. None of those directories owns
documentation; the repository root does, and that is where the spec belongs.
In this repo the same test puts witan's specs beside witan's ADRs and sends
`witan-core`'s — it has no `docs/` — to the root.

Two cases the loop will not decide for you:

- **A package that should own documentation but does not yet.** Create
  `docs/` in it only when the package is **independently released** — witan
  publishes to PyPI on its own cadence, which is why it has its own. A
  directory inside a repo is not a reason; a separate release is.
- **Work spanning several roots.** The spec goes with the one it is mostly
  about, and its header names the others it touches. A spec split across two
  `docs/` directories is one nobody finds.

## Step 2 — Discard generated mirrors

A documentation site usually **copies** these files, so the same spec appears
twice and the copy looks like a second convention. Writing into the copy means
the next site build overwrites it.

```bash
# A generated tree is the one a generator writes. Find the generator first.
grep -rn "docs/" --include='*.py' --include='*.yml' bin/ 2>/dev/null \
  | grep -iE 'mirror|generat|emit'
```

In this repo `bin/gen_docs.py` mirrors into `docs/reference/` and
`docs/explanation/`; the sources are elsewhere. Check for a banner at the top
of a candidate file — a mirrored file nearly always announces itself.

## Step 3 — Create it if it is not there

`docs/specs/` is created on first use. Do not ask, and do not go looking for
somewhere else to put the file: the convention is the answer to that question.

Seed it with a `README.md` saying what belongs there, what does not, and —
**this part matters** — where the older material is:

```markdown
# Specs

The plan for one body of work: what is being built, in what order, and what is
still open. One file per piece of work, retired when the work ships.

Decisions that outlive the work are ADRs, in `../adr/`.

Specs written before <YYYY-MM> are in `../plans/` and were not moved.
```

Without that last line a repo has two live-looking directories and nothing
saying which is current, which is the state this convention exists to end.

## Legacy directories: read, never write

`docs/plans/`, `docs/design/`, `docs/rfcs/` and `docs/internals/design/` hold
real history — roughly fifty documents across the org. They stay exactly where
they are.

- **Read them.** They are the best evidence of how this team writes, and a
  spec that supersedes an older plan should say so and link it.
- **Never write a new document into one**, however many files it already has.
  That is how a directory becomes a grab-bag.
- **Never migrate them.** Live ADRs and issues cite those paths; moving the
  files breaks inbound links for no benefit the convention needs.

What you will find in them, which is also why the single-directory rule is
worth having — `ol-infrastructure/docs/plans/` alone mixes all of these:

| Suffix | Kind |
|---|---|
| `*-spec.md` | A plan for work to be done — the only kind that is a spec |
| `*-analysis.md`, `*-evaluation.md` | Findings, no plan attached |
| `*-spike-results.md`, `*-spike.md` | A spike's answer |
| `*-audit.md`, `*-inventory.md` | An operational snapshot, sometimes with a data file beside it |
| plain (`remove-nginx-sidecar.md`) | Either — read the opener |

## Findings are not files

About a third of what sits in the legacy directories is not a spec and should
not become one. Each kind already has a home:

| What you have | Where it goes |
|---|---|
| A spike's answer | `task_comment` on the spike task, plus `memory_store` for any lesson that outlives this work |
| An evaluation of alternatives | The ADR's `## Options Considered` — that *is* the evaluation, written up |
| An analysis, or a measured baseline | The spec's `## Measured baseline` |
| An audit, inventory or re-measurement | Its own issue. A one-off report is not a design document |

This is what closes the gap in the spike protocol, which requires a spike to
name "the artifact that ends it" and never said where that artifact is
written. The answer is witan, not a file.

## Naming

Kebab-case, subject first, kind last: `gravitino-keycloak-integration-spec.md`.
Subject first is what makes the directory scannable — `spec-gravitino-...`
sorts every spec together and tells a reader nothing.

No numbering. ADRs are numbered because they are referenced by number forever;
specs are referenced by subject and retired when the work ships, and a numbered
sequence of them invites the question of what happened to the gaps.

## Match the house style, not the house location

The location is fixed; how a document reads is not. Before writing, read the
**newest** two or three documents available — in `docs/specs/` if it has any,
otherwise in whichever legacy directory this repo used — and match their
register, section depth and level of detail.

```bash
# Newest by COMMIT time, not mtime: mtime reflects when the file was last
# checked out, which says nothing about authorship order.
#
# Skip deviation logs. One is written DURING implementation, so it is always
# newer than the spec it describes and would crowd out the real examples.
for f in <dir>/*.md; do
  case "$f" in *-deviations.md) continue ;; esac
  printf '%s\t%s\n' "$(git log -1 --format=%cI -- "$f")" "$f"
done | sort -r | head -3
```

The oldest files in any of these directories tend to follow a template their
authors have since abandoned. Read the newest, not the first.

## Repository-specific obligations

Check whether the repo's documentation site indexes the directory before
assuming a new file is complete:

- **agent-kit** — the docs tree is generated and navigated; a new document
  that is not linked builds fine and is findable by nobody. `just docs-gen`
  then `git diff -- docs/` shows whether anything needs regenerating.
- **ol-infrastructure** — `docs/plans/` is not navigated; files are found by
  path and by link from the issue. A new `docs/specs/` inherits that.

Run the repo's docs check before opening the pull request, not after.
