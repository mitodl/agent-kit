# Data pitfalls

Things that silently skew a style analysis. Most were hit in the first real run of
this process; the scripts handle the mechanical ones, but the analysis passes still
need to watch for the rest.

## Collection

- **Mirrored or forked repos double-count.** Two repos sharing history (a mirror,
  a CI copy, a security-advisory fork `*-ghsa-*`) produce the same SHAs.
  `extract_commits.py` dedupes by SHA, keeping the repo with more discovered commits.
  Exclude advisory forks with `exclude_repo_patterns`.
- **`--shortstat` on a blobless clone is very slow.** Each commit fetches its blobs
  lazily. The scripts only collect stats and diffs from full clones and local
  checkouts, so blobless repos contribute messages only.
- **GitHub search caps at 1,000 results per query and rate-limits bursts.**
  `fetch_github.py` bisects date ranges above the cap and retries with backoff; it
  raises rather than silently returning an empty page, because a silent failure
  once truncated four years of PRs without any error (the cause was most likely a secondary rate limit, but that was never confirmed).
- **Two writers on one file corrupt it.** Run different kinds in parallel processes
  if you want, never the same kind twice at once. Writes are atomic per kind.
- **`contributionsCollection` is partial.** It lists at most 100 repos per year,
  counts only default-branch commits, and hides private-repo activity the token
  can't see (`restrictedContributionsCount`). Discovery is year-granular. Add known
  repos to `subject.repos` if they're missing.
- **Identity drift.** People commit under several emails and names over a decade.
  Use `activity/identities.md` and check the per-year commit counts against
  `activity/yearly.md` for gaps.
- **Loose author patterns misattribute.** `--author` and the attribution helper do a
  regex search, so `Sam` matches `Samantha`. Anchor patterns with `^`, but never end
  them with `$`: git matches against a line that includes the timestamp.
- **AI tools commit under variant names** (`Jane Doe (aider)`). Include those
  variants in `author_patterns` on purpose. They're dropped from the baseline by the
  AI flag, but they're some of the clearest evidence for choosing `ai_cutoff`.
- **Symlinked checkouts are the user's real working copies.** Never write to them,
  and remove the links before deleting `repos/` (`acquire_repos.py cleanup` does).

## Things that look like style but aren't

- **Templates inflate PR and issue bodies.** A template introduced in year X makes
  "description length" jump that year. Use `own_words_chars` and check the template's
  git history to see who wrote it and when.
- **GitHub email-reply footers** contain an em dash (`—` above "You are receiving
  this because..."). Replies sent by email also carry mail-client signatures.
- **Forge-generated text:** squash-merge suffixes `(#123)`, autofilled PR bodies from
  the single commit's message, "Update <file>" commits from the web editor or
  suggestion blocks, "Apply suggestions from code review", default revert messages.
- **Bots:** renovate/dependabot/pre-commit-ci authors, bot commands posted as
  comments (`@renovate rebase`, `/gemini review`), approvals on bot PRs.
- **Bare `:+1:` comments** can be a third of all comments in some eras. They inflate
  entry counts; use word-based rates for prose metrics and count them separately as
  an approval behavior.
- **Formatter and codemod commits** (Black, Ruff, Prettier, pyupgrade) change quote
  style, union syntax, and imports across thousands of lines in one commit. Attribute
  those shifts to the tool adoption date, not to a change in how the person writes.
- **Edited-after-the-fact content.** Discussion and issue bodies are fetched in their
  current state. A roadmap post edited by others for two years isn't the original
  author's voice; look for references to later dates.
- **Lint suppressions** (`# noqa`, `eslint-disable`) spike when a rule set expands.
  That's a migration artifact, not a preference for suppressions.
- **Repo-wide counts in shared repos** measure conventions the team follows, which
  the subject may have set or merely followed. Check authorship of the files that
  define the convention before crediting it to one person.
- **Role changes change genres.** A lead writes more issues and approvals and fewer
  explanatory review comments. Compare like with like (comments vs comments) before
  calling it a change in voice.

## AI-era markers

Treat these as signals for choosing `ai_cutoff` and for the comparison-era ban list,
never as proof on their own:

- Commit trailers (`Co-Authored-By: Claude`, `Generated with`), `(aider)` authors,
  scoped conventional commits with lowercase descriptions appearing suddenly.
- PR/issue bodies with `## Summary` / `## Root Cause` / `## Key Changes`, bold-label
  bullets (`- **Thing**: ...`), check-mark or warning emoji, "comprehensive",
  "robust", "seamless", "leverage", Title Case titles, link text that repeats the URL.
- Code: docstring style switching (Sphinx to Google), broad `except Exception`
  blocks, repeated near-identical blocks, sudden test-volume jumps.
- Docs: "Executive Summary", "Key Finding", "Phase N completion report", second-person
  "your backend" in a doc written for the author's own team.
