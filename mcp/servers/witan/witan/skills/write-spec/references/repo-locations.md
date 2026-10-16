# Finding a repository's spec directory

How [`write-spec`](../SKILL.md) decides where a spec goes. The same problem
[`write-adr`](../../write-adr/references/repo-flavors.md) solves for ADR
directories, and the same rule: **detect what the repo already does, and follow
it.** Imposing one repo's layout on another is how a project ends up with two
conventions and no convention.

## Step 1 — Enumerate candidates

```bash
git ls-files \
  | grep -iE '(^|/)docs/(plans|specs|design)/|(^|/)docs/internals/design/' \
  | sed 's|/[^/]*$||' | sort -u
```

Widen it if that comes back empty — some repos use `rfcs/`, `proposals/` or a
`design/` directory at the root:

```bash
git ls-files \
  | grep -iE '(^|/)(docs/)?(plans?|specs?|designs?|proposals?|rfcs?)/' \
  | sed 's|/[^/]*$||' | sort -u
```

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

## Step 3 — Read the newest files, not the oldest

```bash
# Newest by COMMIT time, not mtime: mtime reflects when the file was last
# checked out, which says nothing about authorship order.
for f in <dir>/*.md; do
  printf '%s\t%s\n' "$(git log -1 --format=%cI -- "$f")" "$f"
done | sort -r | head -3
```

The oldest files in any of these directories tend to follow a template their
authors have since abandoned. Read the newest two or three and match those.

## The directories in use

### `ol-infrastructure/docs/plans/` — 29 files

The largest and loosest collection. Mixes several document kinds in one
directory, distinguished only by filename and opening section:

| Suffix | Kind |
|---|---|
| `*-spec.md` | A plan for work to be done |
| `*-analysis.md`, `*-evaluation.md` | Findings, no plan attached |
| `*-spike-results.md`, `*-spike.md` | A spike's answer |
| plain (`remove-nginx-sidecar.md`) | Either — read the opener |

Numbered sections are common but not universal. Openers vary widely: `1. Goal`,
`1. Problem`, `TL;DR`, `Short answer`, and in one case `The decision that
reframes everything`. Several lead with a measured headline — one opens
`1. Headline: Rootly alert volume is down 57%` — which is a good habit worth
copying when there is a number that reframes the work.

**Match the suffix to what you are writing.** A spec named `*-analysis.md` is
findable by nobody looking for the plan.

### `ol-data-platform/docs/specs/` and `docs/design/` — 8 files

Split across two directories. Check both before writing; the split is not
strictly by kind, so follow whichever holds the nearest neighbour to your
subject. The count is the review's, not one taken by reading them, so treat
the conventions there as unsurveyed — Step 3 applies with more than usual
force.

### `agent-kit/docs/internals/design/` — 14 files

The most consistent of the three, and the only one that reliably carries the
prose header block (`Status:` / `Project:` / `Basis:`) and the pinned commit
anchor. Discovery/spec pairs are explicit here — `graph-structured-memory.md`
alongside `graph-structured-memory-spec.md`, with the spec's §0 resolving the
discovery's numbered open questions.

Shipped specs **stay** in this directory rather than moving to an archive. It
is `internals/`, which is already the "how this was built" tree.

## No spec directory anywhere

Ask before creating one, the way
[the skill's "Asking the user"](../SKILL.md#asking-the-user) describes. A new
top-level documentation directory is a convention the whole repo then lives
with, and the answer is often "put it in the one next door" — a monorepo may
keep specs per-package rather than at the root.

If the answer is to create one: `docs/design/` in a repo that already keeps
documentation under `docs/`, otherwise `docs/design/` at the root. Flat,
no per-work-item subdirectory. Seed it with a `README.md` saying what belongs
there and what does not, so the second spec arrives into an established
convention rather than next to a stray file.

## Naming

Kebab-case, subject first, kind last: `gravitino-keycloak-integration-spec.md`.
Subject first is what makes the directory scannable — `spec-gravitino-...`
sorts every spec together and tells a reader nothing.

No numbering. ADRs are numbered because they are referenced by number forever;
specs are referenced by subject and retired when the work ships, and a numbered
sequence of them invites the question of what happened to the gaps.

## Repository-specific obligations

Check whether the repo's documentation site indexes the directory before
assuming a new file is complete:

- **agent-kit** — `docs/internals/design/` is listed in `docs/internals/index.md`
  and reachable from the mkdocs nav. A new spec that is not linked builds fine
  and is findable by nobody. `just docs-gen` then `git diff -- docs/` shows
  whether anything needs regenerating.
- **ol-infrastructure** — `docs/plans/` is not navigated; files are found by
  path and by link from the issue. Nothing to update.

Run the repo's docs check before opening the pull request, not after.
