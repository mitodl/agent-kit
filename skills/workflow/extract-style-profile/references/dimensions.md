# Analysis dimensions

Each dimension is one analysis pass. Its prompt is: "Read `BRIEF.md` and follow it
exactly. Your dimension: <name>. Write `findings/<file>.md`." plus the checklist
below. Drop dimensions with no data (no reviews, no discussions) and split a code
dimension per language when the diff index shows enough samples.

Every checklist item is a question to answer with numbers and quotes, not a
heading to fill in. Skip items the data can't answer, and say so.

## commits → `findings/commits.md`

Sources: `corpus/commit_msgs_*`, `stats/commit_stats.md`, repo history.

- Subject structure per era: tense/mood (past, gerund, imperative), capitalization,
  trailing period, length distribution, share over 72 chars.
- Find the **transition points** (month-level) for any mood or format change and
  look for the trigger: a commit template, commitlint/commitizen config, a
  `CONTRIBUTING` change, a repo migration. Use `git log -S` on config files.
- Type-prefix vocabulary (conventional or custom): each type's meaning as actually
  used, with examples. Combined types, scopes, casing after the colon.
- Body usage: rate by diff size, what goes in a body (why vs what, prose vs
  bullets, links), wrapping column.
- Issue/ticket references: format, frequency, closing keywords, squash suffixes
  added by the forge (don't count those as the author's).
- Granularity: median lines/files per commit, same-day follow-up bursts, fixups,
  reverts and whether reverts get an explanation.
- Merge strategy: direct pushes vs PR merges (`git log --first-parent`, committer
  field), squash vs merge commits.
- Differences by repo ownership: personal vs work, and repos with a different
  house style (does the subject adapt?).
- Team mode: how uniform the format is across members; which rules are enforced by
  tooling vs habit.

## pull-requests → `findings/pull_requests.md`

Sources: `corpus/prs_*`, `data/prs.jsonl`, repo `.github/` history.

- Title style per era vs commit subjects. Do single-commit PRs reuse the subject?
- Description length in **own words** (template stripped) per era and vs diff size.
- Templates: who wrote them and when (git history of `.github/pull_request_template*`
  and org `.github` repo). How each section is filled: deleted, blank, "N/A", one
  line, detailed.
- Structure of the prose: problem-first vs change-first, motivation vs mechanics,
  bullets, headers the author chose (not the template's).
- Testing notes: unit-test claims vs operational verification steps.
- Ticket linking, dependency notes ("Depends on"), screenshots.
- Tone: courtesy, hedging, enthusiasm, emoji.
- Internal vs external/upstream PRs: does register change for outsiders?
- PR size and commits per PR over time.
- Comparison era: generated-looking bodies (`## Summary`, `## Root Cause`, bold-label
  bullets, check-mark emoji, tool attribution).

## reviews → `findings/reviews.md`

Sources: `corpus/reviews_*`, `data/reviews.jsonl`, PR-conversation entries in
`corpus/conversation_*` (approvals as `:+1:` comments live there).

- State distribution per era (approved / commented / changes requested), share with
  no text, inline comments per review, words per comment.
- Approval style: silent, emoji, one-liner with caveat. Did it change (for example
  conversation `:+1:` to formal approvals) and why?
- What comments target: correctness, security, ops safety, naming, simplicity,
  duplication, tests, docs, formatting. Rank by frequency.
- Comment shape: opener patterns ("This should...", questions), where the why sits,
  suggestion blocks, "nit", politeness markers, humor.
- Hedging: on facts vs on judgment calls.
- Who gets reviewed and how that changes the register (mentoring vs peers vs bots).
- Replies to feedback on the subject's own PRs: concede/fix/push back patterns.
- **Recurring technical preferences** stated in reviews. These are the most direct
  source of CLAUDE.md rules, so list each with 2+ dated quotes.
- Team mode: shared review norms (what the team consistently asks for) and which
  requests reliably lead to changes.

## communication → `findings/communication.md`

Sources: `corpus/conversation_*` (exclude review entries).

- Register and tone, sentence length, paragraphing, markdown usage per era (headers,
  bullets, tables, code blocks, links).
- Hedging rate per 1k words (I think, maybe, probably, might, perhaps, should); split
  directive "should" from uncertain "should".
- Punctuation habits: em/en dashes (exclude email-reply footers and quoted text),
  exclamation marks, parentheticals, emphasis style (bold, underscores, caps), emoji.
- Openers and closers: greetings, sign-offs, @mentions, thanks.
- Pronouns: we vs I vs you, and what each is used for.
- Genres, each with structure and exemplars: design docs/RFCs, bug reports (internal vs
  upstream), task issues, answers to questions, disagreement and pushback,
  status/check-in posts.
- Vocabulary: jargon level, acronym handling, recurring phrases, words that never
  appear (useful as a ban list), words that appear only in the comparison era.
- Check any existing writing-style instructions the subject already has against the
  baseline (for example a ban on a word they naturally use).

## code-<language> → `findings/code_<language>.md`

Run once per language with enough samples (see `corpus/INDEX.md`). Sources:
`[<language>]` sections of `corpus/diffs_*` plus direct inspection of the files the
subject authored most (`git log --author --name-only`, then `git show <sha>:<path>`
for snapshots at different dates).

- Naming, typing (adoption timeline), docstring/comment style and density, what
  comments say.
- Function and module shape: procedural vs OO, size, where classes are used.
- Error handling: exceptions caught, validation location, defensive checks, fail-loud
  vs default values.
- Data modelling and config patterns (dataclasses, pydantic, structs, env vars).
- Idioms: string formatting, paths, comprehensions/functional helpers, logging.
- Abstraction level: duplication tolerance, when things get extracted.
- Testing habits: whether tests are written, framework, style, what gets tested.
- Tooling timeline from config history: formatter, linter, type checker, package
  manager, language version targets. Date each adoption and who made it.
- Snapshot exemplars from different eras (short, real).
- Known defects found while reading (useful as review-skill test cases, not style).

## infra-tooling → `findings/infra_tooling.md`

Sources: non-application sections of `corpus/diffs_*` (yaml_ci, iac, config_mgmt,
container_build, shell, docs) and repo config history.

- Config layering (defaults, environment overrides, secrets) and how it's expressed
  in each tool generation.
- Secrets handling: where secrets live, how they're referenced, scanners.
- Naming of stacks, resources, states, jobs.
- YAML/HCL/Dockerfile/shell style, with counts (strict mode, functions, length).
- CI/CD design, task runners (or their absence).
- Reuse mechanisms: templates, generators, shared components, org-level presets.
- Docs and READMEs written in repos: structure, length, wrapping.
- Tooling adoption timeline (pre-commit hooks, linters, dependency bots) with commit
  dates and authors.
- Architecture tendencies visible in code: declarative vs imperative, generation vs
  copying, environment promotion order.

## Team-only: conventions-enforcement → `findings/enforcement.md`

Sources: linter/formatter configs, `.pre-commit-config*`, `.editorconfig`, CI
workflows, templates, `CONTRIBUTING*`, `AGENTS.md`/`CLAUDE.md` if present (note their
dates), and review comments that request a change.

- Inventory what is mechanically enforced today and when each rule arrived.
- For each enforced rule: is it actually followed (sample violations, suppressions
  like `noqa`/`eslint-disable` counts)?
- Conventions reviewers ask for repeatedly that no tool enforces (candidates for
  automation or for agent instructions).
- Conflicts between written guidance and observed practice.
