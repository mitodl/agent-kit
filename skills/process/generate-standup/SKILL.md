---
name: generate-standup
description: >
  Generates a daily standup post from GitHub activity and agent session
  history, and posts it to the mitodl/hq Check-ins discussion. Use when asked
  to write, generate, or post a daily standup — fetches PR, issue, discussion,
  and code-review activity via the gh CLI, queries recent agent sessions, asks
  clarifying questions about timing and off-GitHub work, renders the standup
  in the team's standard format, and posts it as a discussion comment with
  user confirmation.
license: BSD-3-Clause
metadata:
  category: process
---

# Generate Daily Standup

Produces a daily standup post from live GitHub activity and optionally posts it
to the `mitodl/hq` Check-ins discussion.

**Requires:** `gh` (authenticated) and `jq`.

---

## Step 1 — Fetch GitHub context

Run the bundled context script **before asking any questions**:

```bash
bash skills/process/generate-standup/scripts/get-standup-context.sh [-t YYYY-MM-DD] [-o org1,org2]
```

| Flag | Description | Default |
|------|-------------|---------|
| `-t` | "Today" date (`YYYY-MM-DD`) | today (UTC) |
| `-o` | Comma-separated orgs to search | `mitodl,openedx` |

The script outputs a JSON object:

```json
{
  "meta": {
    "username": "string",
    "display_name": "string",
    "today": "string",
    "yesterday": "string",
    "tomorrow": "string",
    "since": "string"
  },
  "checkin_targets": { "eod": {...}, "bod": {...}, "latest": {...} },
  "prs_authored":        [...],
  "prs_reviewed":        [...],
  "issues":              [...],
  "discussions_opened":  [...],
  "discussion_comments": [...]
}
```

- `meta.display_name` is the GitHub profile name when available; fall back to
  `meta.username` if it is blank.
- `meta.yesterday` is the previous weekday (Friday if today is Monday).
- `meta.tomorrow` is the next weekday (Monday if today is Friday).
- `meta.since` is midnight UTC on `meta.yesterday` — the fetch window start.
- `checkin_targets` holds the candidate post targets in `mitodl/hq`, each with
  `id` (GraphQL node ID), `number`, `title`, `url`, `createdAt`, `date` (the
  calendar date the title names) and `comment_count`. Any of the three may be
  `null`. See [Choosing the post target](#choosing-the-post-target).
- `discussions_opened` — discussions the user **opened** in the window, any
  category. Opening one is announcement-worthy; see Step 3.
- `discussion_comments` — comments and replies the user left in the window,
  with a `url` deep-linked to the comment and a 300-char `excerpt`. Check-ins
  threads are excluded (those are standup posts — reporting them is circular).
- Do **not** infer or fabricate activity beyond what the script returns.

### Choosing the post target

The thread for day D is titled with D's date (`Tuesday, August 11th, 2026`) and
is created on D-1, mid-afternoon UTC. "Newest thread" is therefore not a usable
rule — which thread is newest depends only on what time of day the script ran.
The script resolves the target from the **date in the title** instead:

- **EOD** → `checkin_targets.eod`, the thread titled `meta.tomorrow`. An
  end-of-day report is read by the team at the *next* morning's standup, so it
  belongs on the next day's thread.
- **BOD** → `checkin_targets.bod`, the thread titled `meta.today` — yesterday's
  work reported at today's standup.

`checkin_targets.latest` is the newest thread regardless of date, and is only a
fallback. If the target for the user's `timing` answer is `null` the thread has
not been created yet (before ~15:30 UTC on the preceding day): **say so, show
`latest` by title, and ask** — never silently post to a different day's thread
than the table above selects.

Every PR and issue carries three timestamps and each still-open authored PR
carries its review state. These fields exist so that no claim in the post has to
be guessed — see [Timestamp discipline](#timestamp-discipline) and
[Which PRs actually need review](#which-prs-actually-need-review) in Step 3.

| Field | Meaning |
|-------|---------|
| `createdAt` | when it was opened |
| `updatedAt` | last touched — the search window is built on this |
| `closedAt` | when it closed/merged; `null` while open |
| `state` | lowercase. PRs: `open`, `merged`, or `closed` (closed-unmerged). Issues: `open` or `closed` |
| `isDraft` | PRs only |
| `author.login` | issues only — **not** necessarily the user; see the authorship note in Step 3 |
| `needs_review` | **authored PRs only.** `true` iff a human still owes a review. Already accounts for draft status, approval, requested changes, and bot reviewers — use it as-is; do not re-derive it. |
| `reviewDecision` | `APPROVED`, `CHANGES_REQUESTED`, `REVIEW_REQUIRED`, or `null` (nobody has reviewed). Open authored PRs only. Use for *framing*, not for the needs-review decision. |
| `reviewRequests` | human logins/team slugs with a review still pending; bots filtered out. Open authored PRs only. |
| `review_state_unknown` | `true` if the review lookup failed. Say the state is unknown rather than guessing. |

Entries in `discussions_opened` and `discussion_comments` both carry
`repository`, `category`, `url`, and `createdAt` as plain strings, plus
`number`/`title` and `discussion_number`/`discussion_title` respectively.
Comments add `discussion_url` and `excerpt`.

---

## Step 1b — Mine your own session history

Surface work that never produced a GitHub artifact — investigation, local
changes, planning, incident follow-up — by reviewing your own session activity
since `meta.since`. Use whichever of the following your runtime **actually
has**; check your available tools and move on. Do not go hunting for a tool
that is not there, and do not narrate the search.

1. **Session or memory MCP tools**, if configured — e.g. witan's
   `workflow_session_list` / `recall`, or an equivalent project tracker. Query
   for sessions, traces, or memories touched since `meta.since`, scoped to the
   relevant repos.
2. **Local transcript files**, if your runtime persists them to disk (Claude
   Code writes one JSONL file per session under
   `~/.claude/projects/<cwd-slug>/`, one directory per working directory).
   Filter to files modified since `meta.since`; treat the working-directory
   path as the repo, and the first user message plus the most recent assistant
   text as a proxy summary.
3. **Neither available.** Skip this step and proceed with GitHub-only data —
   do not fabricate session activity. **Say so up front in the message
   presenting the draft**, not as a trailing footnote: off-GitHub work is then
   entirely unrepresented, and only the user can fill that gap (Step 2's
   `off_github` field).

Normalize each session into `repository` (or `null`), `branch` (or `null`),
`summary`, and `updated_at` before moving on. Step 2's session-only detection
and Step 3's bucketing assume that shape whichever source produced it.

**Summarization rules** (apply regardless of which source above was used):

| Evidence available | Action |
|--------------------|--------|
| Explicit summary/checkpoint/overview text | Use as the session summary |
| No explicit summary; has repo + branch + a concrete first message | Derive a brief summary from repo/branch + message intent |
| No explicit summary; no repo, or a trivial/meta prompt | Skip |
| Session is for generating this standup itself | Skip |

Store the resulting list of session summaries; use in Steps 3–4 to enrich
GitHub-derived bullets and fill in non-GitHub work.

---

## Step 2 — Ask clarifying questions

Use a **single `ask_user` call** with all fields at once.

These answers are the *primary* source for the post, not a supplement to the
GitHub data. GitHub tells you which artifacts moved; only the user can say what
the work was and what mattered about it. Where an answer covers the same work
as a GitHub item, keep the user's wording and let the link carry the rest.

First, identify **session-only work** from Step 1b (sessions with `repository:
null` or whose repository doesn't appear in `prs_authored`). Format them as a
short suggestion list for the `off_github` field description.

The most important field is `timing` — it controls the section headers and
which date's activity is treated as "done" work:

```json
{
  "timing": {
    "type": "string",
    "title": "When are you posting?",
    "enum": ["EOD — reporting today's work (today/tomorrow headers)",
             "BOD — reporting yesterday's work (yesterday/today headers)"],
    "description": "EOD: post at end of your work day; yesterday section covers today's date (meta.today). BOD: post at start of your work day; yesterday section covers meta.yesterday."
  },
  "blockers": {
    "type": "string",
    "title": "Blockers",
    "description": "Are you blocked on anything? Include a link and the @handle of whoever needs to unblock you. Leave blank if none."
  },
  "announcements": {
    "type": "string",
    "title": "Announcements",
    "description": "Anything to announce not in GitHub? (OOO, special review requests, schedule changes, external docs/runbooks to link, etc.) Leave blank if none."
  },
  "off_github": {
    "type": "string",
    "title": "Off-GitHub work",
    "description": "Meetings, planning, research, design, talks, incident follow-up, or other work that won't appear in GitHub. Leave blank if none.\n\nPossible session-only work detected:\n<bullet list of session-only summaries, or 'none detected'>"
  },
  "extra_context": {
    "type": "string",
    "title": "Extra context to preserve",
    "description": "Any nuance, concerns, caveats, external links, or wording you want carried through into the final post. This is where to capture the human part that raw GitHub activity misses. Leave blank if none."
  }
}
```

---

## Step 3 — Classify activity

From the user's `timing` answer, determine:

| Timing | `report_date` (done work) | `planned_date` (next work) | Past header | Future header |
|--------|--------------------------|---------------------------|-------------|---------------|
| EOD    | `meta.today`             | `meta.tomorrow`           | `What did I work on today?` | `What am I working on tomorrow?` |
| BOD    | `meta.yesterday`         | `meta.today`              | `What did I work on yesterday?` | `What am I working on today?` |

**Timezones, before any date comparison below.** `meta.today` is the **UTC**
date at script run time, and GitHub's timestamps are UTC too — but the workday
being reported is US Eastern. Convert item timestamps to Eastern (UTC−4 in DST,
UTC−5 otherwise) and compare those against `report_date`: a
`2026-07-31T00:51Z` comment was 8:51pm Eastern on **July 30**, so matching the
raw UTC prefix files a Thursday evening's work under Friday. Anything before
04:00Z (05:00Z outside DST) belongs to the previous Eastern day.

One case the conversion can't fix: for an EOD post after ~8pm ET, `meta.today`
is *already tomorrow's* Eastern date, so nothing will match `report_date`. Re-run
the script with `-t <the Eastern date>` instead of working around it.

This governs **inclusion and verb choice alike**. When a converted item lands on
a different day than its UTC prefix suggests, note which day you placed it on in
the message presenting the draft — not in the post itself.

**Bucketing rules:**

- **Done (past section):** Any PR or issue with `updatedAt` on
  `report_date`. Include both merged and still-open items that were
  actively worked on that day (merged PRs have `state == "merged"`). This
  decides *inclusion* only — see [Timestamp discipline](#timestamp-discipline)
  before choosing the verb you attach to it.
- **Planned (future section):** Open PRs and issues the user is continuing,
  plus anything explicitly stated in user answers. Omit items with no
  `updatedAt` since `meta.since` (stale).
- **Announcements:** authored PRs with `needs_review: true` — see
  [Which PRs actually need review](#which-prs-actually-need-review) below.
  Also every entry in `discussions_opened` with `createdAt` on `report_date`
  (**opening a new discussion is always announcement-worthy** — it's a request
  for the team's attention, regardless of category), plus blockers and OOO info.
- **Discussion comments (past section):** entries in `discussion_comments` with
  `createdAt` on `report_date` belong under done work. These are frequently
  substantive design work with no PR or issue attached, so they are easy to drop
  — don't. Summarize from the `excerpt` and link the comment `url` (the deep
  link), not just the parent thread. A long comment laying out a proposal is
  worth a real sentence, not "commented on
  <https://github.com/mitodl/hq/discussions/12502>".
- **Deduplication:** A PR in both `prs_authored` and `prs_reviewed` → list
  once under the most relevant bucket.

### Timestamp discipline

`updatedAt` is only the search window. It says an item was *touched* in the
window — nothing more. A PR opened last week and merged today has an
`updatedAt` of today, and so does one opened today; they are not the same
report. **Every lifecycle verb you attach to a PR or issue — opened, merged,
closed, reviewed — must be licensed by the field that actually means that
verb.** (Verbs in the user's own `off_github` / `extra_context` notes are theirs;
don't second-guess those against GitHub fields.)

| To write… | The item must have… |
|-----------|---------------------|
| "opened", "filed", "put up" | `createdAt` on `report_date` |
| "merged", "shipped", "landed" | `state == "merged"` **and** `closedAt` on `report_date` |
| "closed" | `state == "closed"` and `closedAt` on `report_date` |
| "worked on", "picked up again" | `updatedAt` on `report_date` — the only claim `updatedAt` alone supports |

Check the field before writing the verb, not after. When only `updatedAt` falls
on `report_date`, "worked on" is the strongest available claim; reach for a
specific verb only with the timestamp to back it.

**Authorship is a second check, separate from the timestamp.** Only
`prs_authored` is author-scoped. `prs_reviewed` is every PR you have *ever*
reviewed, and `issues` uses `involves:`, so it includes issues other people filed
that you merely commented on — both buckets routinely contain items someone else
opened, merged, or closed on `report_date`, satisfying the table exactly. Outside
`prs_authored`, confirm `author.login == meta.username` before "opened"/"filed",
and never write "merged"/"shipped"/"closed" at all: you reviewed or commented on
it, someone else landed it.

**"Reviewed" is not licensed by `updatedAt`.** `prs_reviewed` matches PRs you
reviewed at any time in the past, and `updatedAt` moves whenever anyone touches
the PR — so a PR you reviewed weeks ago surfaces in today's window because
someone else pushed to it. The data carries no timestamp for *your* review. Say
"reviewed" only when the user's own notes say so, or after checking
`gh pr view <url> --json reviews` for a `submittedAt` on `report_date`.
Otherwise the item is "worked on", or omitted.

### Which PRs actually need review

**The PRs you describe as needing review are exactly the authored PRs with
`needs_review: true`** — no additions, no substitutions. "Open" does not mean
"needs review", and a **"needs review" label or project field is not evidence** —
reviewers routinely approve a PR and forget to clear the label. The script already
folded in draft status, approval, requested changes, and bot reviewers, so do not
rebuild that logic from `reviewDecision`.

Other PRs may still appear in announcements under a *different* claim — approved
and ready to merge, review state unknown, or a specific request the user made in
Step 2 — but never as "needs review".

Use `reviewDecision` only to *phrase* the bullet:

| `reviewDecision` | Framing |
|------------------|---------|
| `null` / `REVIEW_REQUIRED` | needs review — name pending `reviewRequests` handles if any |
| `CHANGES_REQUESTED` | the ball is with the author: "address feedback on …" (Planned, not Announcements) |
| `APPROVED` | mention as ready-to-merge, if at all |

Two caveats:

- `APPROVED` **survives new commits.** An approval from before the author's
  latest push still reads `APPROVED`, so a stale approval is indistinguishable
  from a current one here. If you know the PR moved after approval, say it may
  need another look.
- `review_state_unknown: true` means the lookup failed. Report the state as
  unknown; do not fall back to "open, so it needs review".

**Incorporating agent sessions:**

- If a session maps to a PR/issue already in the GitHub data, enrich that
  bullet with context from the session summary — do not create a duplicate.
- If a session represents work with no GitHub artifact, add it as its own
  bullet under done or planned based on `updated_at` vs `report_date`.
- Prefer explicit user-provided notes (`announcements`, `off_github`,
  `extra_context`) over auto-generated summaries when both cover the same work.
  Use GitHub/session data to fill gaps, not to overwrite the human phrasing.
- If several items are part of the same theme (for example, reviewing a batch
  of PRs or continuing a single incident follow-up), grouping them under one
  parent bullet with sub-bullets is encouraged when it reads more naturally.

---

## Step 4 — Render the standup

Use `meta.display_name` when available; fall back to `meta.username`.
Prefer the human-readable name over the raw GitHub login.

```markdown
_<Display Name>_

> Standup announcements

- <item>

> <past header>

- <item>

> <future header>

- <item>
```

**Writing style:** a standup is skimmed by the whole team in a few seconds.
One line per item, plain words, no narration. A bare link, or a link plus a
few words, is a good bullet. Don't paste a PR's title back after its link —
GitHub renders it already; add a description only when it says something the
title doesn't. No lead-in sentence, no closing summary, no "as mentioned
above" cross-references, no filler adjectives, and no emoji unless the user
used them. Multi-line bullets are for genuine nuance (a caveat, a concern, an
open question), not for elaborating on work the link already explains.

**Formatting rules:**

- **Every entry is a bullet.** Each top-level item starts with a hyphen bullet
  marker at column 0 — never a bare prose line, never a numbered list.
  Sub-bullets are indented exactly two spaces, and a bullet's own continuation
  lines are indented two spaces as well so they stay inside the bullet instead
  of ending the list.
- **Lead with the GitHub reference.** Any entry about a PR or issue *begins*
  with its full `https://github.com/<org>/<repo>/(pull|issues)/<number>` URL,
  followed by an em dash and the description — not the URL buried mid-sentence.
  GitHub renders such a URL as a linked `org/repo#123` reference carrying the
  glyph for its type and state, so a reader scanning the post sees at a glance
  which items are PRs, which are issues, and which are merged or closed. Bury
  the reference in prose and that signal is gone. The same applies to
  discussion comments: lead with the deep link.
  - Grouping bullets are the one exception — a parent like `Reviewed a batch of
    PRs:` with reference-led sub-bullets under it is fine.
- **Empty sections:** write `- None`, never omit the section header.
- **Blockers** go in announcements as a bullet; tag with `@handle` and link.
- **Name line:** prefer a human-friendly display name, commonly wrapped in
  underscores for italics to match team style.
- **Links:** use plain URLs (`https://github.com/...`), not markdown
  `[text](url)` formatting. External links (runbooks, docs, Slack threads,
  etc.) are welcome when they add context — also as plain URLs.
- **Never use a bare `#<number>` reference** (e.g. `ol-infrastructure #5133`)
  for a PR/issue outside the posting repo. The standup is posted in
  `mitodl/hq`, so GitHub auto-links bare `#number` text to an issue/PR in
  `mitodl/hq` itself — silently producing a link to the wrong item whenever
  the referenced PR lives in another repo. Always spell out the full
  `https://github.com/<org>/<repo>/pull/<number>` URL instead; drop the `#N`
  entirely rather than pairing it with the repo name.
- **Level of detail:** match what the data supports. If a PR/issue title is
  self-explanatory, a bare link is sufficient. Add a brief description only
  when context genuinely helps (e.g., the PR title doesn't convey purpose, or
  the work involved investigation/discussion not captured in a link).
- **Prefer natural phrasing over templated phrasing:** avoid mechanical bullets
  like `worked on <url>` when a clearer summary is available. Preserve the
  user's own wording and concerns wherever possible. **Add the detail to the
  description, never by upgrading the verb** past what
  [Timestamp discipline](#timestamp-discipline) licenses — if only `updatedAt`
  falls on `report_date`, "worked on" stays, and the *rest* of the bullet
  carries the specifics.
- **Use nested bullets when helpful:** a parent bullet like `Reviewed a bunch
  of PRs:` or an issue link followed by indented explanation often reads better
  than a flat list of disconnected one-liners.
- **Do not impose narrative style:** some people post links; some post prose;
  both are correct. Let the available data and the user's notes guide the
  output.
- **Do not group PRs** across repos into parent bullets unless the user
  explicitly works across many repos on the same thing and grouping is clearly
  cleaner — default to separate bullets. Grouping within a single theme is fine.
- **Planned section** should reflect what's actually next, not a mechanical
  list of every open PR. Omit items the user is clearly done with, and include
  forward-looking caveats or goals when the user supplied them.

---

## Step 5 — Confirm and post

Display the rendered standup, then use `ask_user` to confirm:

```json
{
  "action": {
    "type": "string",
    "title": "Post this standup?",
    "enum": ["Post it", "Edit first", "Cancel"],
    "description": "Post as a comment on <target title> (<target url>), make edits, or cancel."
  }
}
```

**Do not post unless the user selects "Post it".**

On confirmation, post using the bundled script:

```bash
echo "<rendered standup>" \
  | bash skills/process/generate-standup/scripts/post-standup-comment.sh \
      -d "<target id>"
```

The target is the one [chosen in Step 1](#choosing-the-post-target) from the
user's `timing` answer — `checkin_targets.eod` for EOD, `.bod` for BOD. Name
the thread by its title in the confirmation, so a wrong day is caught before
the post lands rather than after.

The script prints the comment URL on success.

---

## Example output (EOD, link-primary style)

```markdown
Anna G

> Standup announcements

- https://github.com/mitodl/mitxonline/pull/3600 — needs review

> What did I work on today?

- https://github.com/mitodl/mitxonline/pull/3600 — worked on it
- https://github.com/mitodl/mit-learn/pull/3346 — updated UI and fixed tests,
  received review

> What am I working on tomorrow?

- https://github.com/mitodl/mit-learn/pull/3346 — address feedback
- https://github.com/mitodl/hq/issues/11440 — resolve
```

## Example output (BOD, narrative style)

```markdown
Tobias Macey

> Standup announcements

- PRs needing review:
  - https://github.com/mitodl/ol-infrastructure/pull/4659 — add Archive/Deep Archive access tier support to OLBucket
  - https://github.com/mitodl/ol-data-platform/pull/2238 — automate Iceberg table maintenance across the lakehouse
- https://github.com/mitodl/ol-infrastructure/pull/4640 — approved and ready to merge

> What did I work on yesterday?

- Worked on addressing the hanging open issue for Dagster assets using Polars to read Iceberg tables
- https://github.com/mitodl/ol-infrastructure/pull/4659 — opened for S3 cost optimization
- https://github.com/mitodl/hq/discussions/12488#discussioncomment-17801234 — laid out the two
  options for Iceberg table maintenance scheduling; leaning toward a single Dagster schedule
  over per-table sensors

> What am I working on today?

- Finish fixing the Polars/Iceberg hang in Dagster
- Test the Concourse release workflow end to end
- Wrap up self assessment
```

## Example output (EOD, hand-written narrative style)

```markdown
_Chris Patti_

> Standup announcements

- Retrospective on yesterday's XPro Production Certificate Outage — https://pe.ol.mit.edu/runbooks_post_mortems/20260603_xpro_outage/

> What did I work on today?

- Pycon tech talk!
- Reviewed a bunch of PRs:
  - https://github.com/mitodl/ol-infrastructure/pull/4715
  - https://github.com/mitodl/ol-infrastructure/pull/4713 — and a couple more I forgot :)
- Engaged in a wrestling match with Rootly's post incident retrospective creation tools.
  Lost, then after getting support from them won - kind of? It's not as clean as I'd like
  but details from yesterday's incident are documented at
  https://pe.ol.mit.edu/runbooks_post_mortems/20260603_xpro_outage/

> What am I working on tomorrow?

- https://github.com/mitodl/ol-infrastructure/issues/4702 — streamlined EKS credentialing
  - I'm concerned that the issues Tobias raised around devs having neither the artifacts nor the permissions to run the streamlined EKS credentialing process we wrote is a deal breaker. We talked about potential solutions for a few seconds today, but I was mostly Retrospective-ing from the outage yesterday and didn't really have time to dig much.
  - I'd like to have our solution either finished, abandoned, or very thoroughly scoped by the end of the week.
```

## Example output (ambiguous timing, hybrid style)

```markdown
Sar

> Standup announcements

- None

> What did I work on yesterday/today?

- https://github.com/mitodl/ol-infrastructure/pull/4658 — wrote and deployed
- Continued investigating SCIM sync failures — updates are reaching Keycloak
  logs but not propagating to Learn/MITx Online; restarting Keycloak temporarily
  restores sync, root cause still unknown

> What am I working on today/tomorrow?

- Continue digging into the SCIM update issue
```

---

See [context script](scripts/get-standup-context.sh) for the GitHub
data-fetching implementation and [post script](scripts/post-standup-comment.sh)
for the comment posting implementation.
