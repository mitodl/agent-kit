---
name: extract-style-profile
description: >
  Build an evidence-backed, longitudinal profile of how a person, a team, or a
  repository writes code and communicates, from git history and GitHub activity
  (commits, PRs, reviews, issues, discussions, code diffs), separating pre-AI
  baseline behavior from AI-assisted drift. Produces a report plus paste-ready
  CLAUDE.md / AGENTS.md instructions and skill proposals. Use this skill when asked
  to "analyze my coding style", "extract my writing voice", "generate a CLAUDE.md
  from my history", "what are our team's code review / commit / PR conventions",
  "derive a style guide from this repo", "profile how our team communicates", or to
  audit existing agent instructions against what someone actually does. Needs `gh`
  (authenticated), `git`, and `uv`.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Extract a style profile

Turns years of git and GitHub history into rules an agent can follow, with the
evidence attached. It works in three modes:

| Mode | Subject | Output |
|---|---|---|
| `person` | One human's GitHub login(s) and git identities | Their personal coding and communication style |
| `team` | Several members | Shared conventions with agreement levels, enforced vs emergent. No per-person grading |
| `repo` | One or more repositories, all human authors | The codebase's conventions |

The process is: collect data with the bundled scripts, cut it into per-era text
corpora, run one evidence-bound analysis pass per dimension, spot-check, then
synthesize a report. The scripts do the mechanical work; the analysis passes and
the synthesis need judgment.

References (read when you reach the step that uses them):

- [references/subject-schema.md](references/subject-schema.md): `subject.json` fields, examples per mode, choosing eras and the AI cutoff
- [references/analysis-brief.md](references/analysis-brief.md): the brief every analysis pass reads
- [references/dimensions.md](references/dimensions.md): the checklist for each analysis pass
- [references/pitfalls.md](references/pitfalls.md): data artifacts that look like style but aren't
- [references/report-template.md](references/report-template.md): report structure and synthesis rules

## Before starting

**Consent and scope.** Profiling a person means reading everything they wrote in
public and private repos the token can see. Run `person` mode on yourself, or on
someone who asked for it. For `team` mode, confirm the team knows, and keep the
output about conventions: no rankings, no per-person scorecards, no quotes that
single someone out.

**Cost.** A decade-long profile of one active engineer took ~25 minutes of data
collection and six parallel analysis passes of ~150-250k tokens each. A single repo
over two years is much smaller. Tell the user the rough size (from
`activity/yearly.md`) before launching the analysis passes, and offer to narrow
`since`, `orgs`, or the dimension list.

**Ask only what you can't infer.** You need: mode, logins or repos, date range,
output location, and whether the subject used AI coding tools (and roughly when).
Everything else has defaults.

## Step 1. Set up the workdir

Pick a workdir outside any repository checkout (a scratch directory, or somewhere
like `~/Documents/style-profile/<name>` if the user wants to keep the data). Write
`<workdir>/subject.json` following [subject-schema.md](references/subject-schema.md).
Start with provisional eras and `ai_cutoff`; you'll revise both after Step 3.

Scripts live in this skill's `scripts/` directory. Run each with
`uv run <skill-dir>/scripts/<script>.py <workdir>`. They share `common.py`, so run
them from their own directory or by full path, not copied elsewhere.

## Step 2. Discover and acquire

```bash
uv run scripts/discover.py <workdir>        # person/team only
```

Writes `activity/yearly.md` (contributions per member per year, including hidden
private counts), `activity/repos.tsv` (repos by commit count), and
`activity/identities.md` (git name/email pairs GitHub linked to each login).

- Put the real identities into `members[].author_patterns`. Missing an old work
  email silently drops years of commits.
- Show the user the yearly table and the top repos. Confirm the scope (`orgs`,
  exclusions) before cloning.

```bash
uv run scripts/acquire_repos.py <workdir>
```

Links existing checkouts found under `local_roots` (read-only) and clones the rest.
Repos under `full_clone_min_commits` are cloned blobless: their commit messages are
used, but not their diffs.

## Step 3. Extract, measure, pick the boundaries

```bash
uv run scripts/extract_commits.py <workdir>
uv run scripts/fetch_github.py <workdir>     # slowest step; prints progress to stderr
uv run scripts/profile_stats.py <workdir>
```

`fetch_github.py` accepts `--kinds prs issues comments reviews discussions`. Running
different kinds in separate background processes is safe and faster. Running the
same kind twice at once corrupts its output file. In repo mode, `prs`, `reviews`
and `comments` all run the PR query, so treat them as one kind there.

Then read `stats/commit_stats.md` and `stats/ai_markers.md`, and:

1. **Fix `ai_cutoff`.** Pick the first month where AI markers become sustained
   relative to activity. Confirm with the user. See
   [subject-schema.md](references/subject-schema.md#choosing-ai_cutoff).
2. **Fix the eras.** Put boundaries at step changes in the yearly tables and at role
   or stack shifts visible in `repos.tsv`. Put `ai_cutoff` on an era boundary.
3. If either changed, re-run `extract_commits.py` and `profile_stats.py` (both run
   in seconds).

## Step 4. Build corpora

```bash
uv run scripts/build_corpora.py <workdir>
```

Writes per-era `commit_msgs_`, `prs_`, `reviews_`, `conversation_`, and `diffs_`
files plus `corpus/INDEX.md`. Diffs are a stratified sample by dominant language,
capped per repo and per author (`--per-language`, `--per-repo`, `--per-author`).
Check the index: an era with a handful of entries in a file can't support a trend
claim. Merge eras or widen the sample if needed.

## Step 5. Analysis passes

1. Copy [analysis-brief.md](references/analysis-brief.md) to `<workdir>/BRIEF.md` and
   fill in the placeholders: subject, known history, cutoff, eras, and mode rules.
   Add what you learned in Steps 2-4 (role changes, stack migrations, templates).
2. Choose dimensions from [dimensions.md](references/dimensions.md) based on what
   the corpus has: `commits`, `pull-requests`, `reviews`, `communication`, one
   `code-<language>` per language with enough diff samples, `infra-tooling`, and
   `conventions-enforcement` for teams.
3. Run one pass per dimension. Where the agent platform supports subagents, run
   them in parallel in the background, each prompted with: *"Working directory:
   `<workdir>`. Read BRIEF.md and follow it exactly. Your dimension: `<name>`. Write
   `findings/<file>.md`."* followed by that dimension's checklist from
   dimensions.md. Without subagents, run the passes one at a time yourself, and
   clear working notes between passes.
4. The passes read the corpora; you don't need to. Keep your own context for the
   synthesis.

## Step 6. Verify

Before synthesizing, check at least one headline claim per findings file against
the source:

- a dated transition: `git -C repos/<repo> show --stat <sha>` for the trigger commit
- a count: re-grep the corpus (for example, em dashes outside email footers)
- an attribution: who authored the config or template credited to the subject

Fix or drop claims that don't hold. Where two findings files disagree, check the
data and settle it. Tell the user what you checked.

## Step 7. Synthesize and deliver

Write `<workdir>/REPORT.md` following
[report-template.md](references/report-template.md). Apply the synthesis rules
there, especially:

- Only traits seen across media or baseline eras go in the stable core.
- Comparison-era-only traits go to the drift table, never to paste-ready rules.
- Habits that would make agent output worse go to "observed but not worth encoding".
- If the subject has existing agent instructions (`~/.claude/CLAUDE.md`, `AGENTS.md`,
  skills), audit each instruction against the baseline: corroborated, stricter than
  baseline, not in baseline, or contradicted.

Keep `findings/`, `corpus/`, `stats/`, and `data/` next to the report so every
citation resolves. Then remove the clones:

```bash
uv run scripts/acquire_repos.py cleanup <workdir>
```

This unlinks symlinked local checkouts before deleting `repos/`. Don't clean up by
hand: `rm -rf repos/<name>/` with a trailing slash follows the symlink and deletes
the contents of the user's real checkout.

Don't edit the subject's CLAUDE.md, AGENTS.md, or skills as part of this skill.
Hand over the report with its paste-ready section and let the user decide what to
adopt.

## Final message

Keep it short: where the report is, the 3-5 most useful findings, the conflicts
found with existing instructions, and what was spot-checked. The report holds the
detail.
