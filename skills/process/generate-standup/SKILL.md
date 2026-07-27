---
name: generate-standup
description: >
  Generates a daily standup post from GitHub activity and agent session
  history, and posts it to the mitodl/hq Check-ins discussion. Use when asked
  to write, generate, or post a daily standup — fetches PR, issue, and
  code-review activity via the gh CLI, queries recent agent sessions, asks
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
  "checkin_discussion": { "id", "number", "title", "url", "createdAt" },
  "prs_authored":    [...],
  "prs_reviewed":    [...],
  "issues":          [...],
  "rfc_discussions": [...]
}
```

- `meta.display_name` is the GitHub profile name when available; fall back to
  `meta.username` if it is blank.
- `meta.yesterday` is the previous weekday (Friday if today is Monday).
- `meta.tomorrow` is the next weekday (Monday if today is Friday).
- `meta.since` is midnight UTC on `meta.yesterday` — the fetch window start.
- `checkin_discussion` is the most recent Check-ins discussion in `mitodl/hq`.
  Keep its `id` (GraphQL node ID) and `url` for Steps 4–5.
- Do **not** infer or fabricate activity beyond what the script returns.

---

## Step 1b — Mine your own session history

The goal of this step is to surface work you did that never produced a
GitHub artifact — investigation, local changes, planning, incident
follow-up — by reviewing your own recent session activity since
`meta.since`. How you do this depends entirely on what your current agent
runtime exposes; there is no fixed schema or tool name to assume. Work
through these options in order and use the first one that applies:

1. **A dedicated session-history tool.** If your environment exposes a tool
   for querying past sessions/checkpoints (for example a `sql`-style tool
   backed by a session store, or an equivalent structured API), query it for
   sessions updated since `meta.since`, along with any stored
   summary/checkpoint/overview field and — for sessions with no stored
   summary — the first user message as a fallback signal of intent.
2. **Team or project memory/work-tracking MCP tools**, if this environment
   has any configured (e.g. a shared memory graph, workflow/session tracker,
   or project-tracking server). Use whatever read/query tools they expose to
   find sessions, traces, or memories touched since `meta.since`, scoped to
   the relevant repos.
3. **Local transcript files**, if your runtime persists them to disk (e.g.
   Claude Code writes one JSONL file per session under
   `~/.claude/projects/<cwd-slug>/`, one project directory per working
   directory). Filter to files modified since `meta.since`, and treat the
   working-directory path as the repo, plus the first user message and the
   most recent assistant text as a proxy summary of what happened.
4. **None of the above available or discoverable.** Skip this step
   entirely and proceed with GitHub-only data — do not fabricate session
   activity.

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

**Bucketing rules:**

- **Done (past section):** Any PR or issue with `updatedAt` on
  `report_date`. Include both merged and still-open items that were
  actively worked on that day (merged PRs have `state == "merged"`).
- **Planned (future section):** Open PRs and issues the user is continuing,
  plus anything explicitly stated in user answers. Omit items with no
  `updatedAt` since `meta.since` (stale).
- **Announcements:** PRs authored by the user that are still open and need
  human review (exclude bots: Copilot, Gemini, Renovate, Dependabot, Sentry).
  Also include RFC discussions created today, blockers, and OOO info.
- **Deduplication:** A PR in both `prs_authored` and `prs_reviewed` → list
  once under the most relevant bucket.

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

**Formatting rules:**

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
  user's own wording and concerns wherever possible.
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
    "description": "Post as a comment on <title> (<checkin_discussion.url>), make edits, or cancel."
  }
}
```

**Do not post unless the user selects "Post it".**

On confirmation, post using the bundled script:

```bash
echo "<rendered standup>" \
  | bash skills/process/generate-standup/scripts/post-standup-comment.sh \
      -d "<checkin_discussion.id>"
```

The script prints the comment URL on success.

---

## Example output (EOD, link-primary style)

```markdown
Anna G

> Standup announcements

- https://github.com/mitodl/mitxonline/pull/3600 needs review

> What did I work on today?

- worked on https://github.com/mitodl/mitxonline/pull/3600
- updated UI and fixed tests https://github.com/mitodl/mit-learn/pull/3346, received review

> What am I working on tomorrow?

- address feedback https://github.com/mitodl/mit-learn/pull/3346
- resolve https://github.com/mitodl/hq/issues/11440
```

## Example output (BOD, narrative style)

```markdown
Tobias Macey

> Standup announcements

- PRs needing review:
  - ol-infrastructure: add Archive/Deep Archive access tier support to OLBucket — https://github.com/mitodl/ol-infrastructure/pull/4659
  - ol-data-platform: automate Iceberg table maintenance across the lakehouse — https://github.com/mitodl/ol-data-platform/pull/2238

> What did I work on yesterday?

- Worked on addressing the hanging open issue for Dagster assets using Polars to read Iceberg tables
- Opened https://github.com/mitodl/ol-infrastructure/pull/4659 for S3 cost optimization

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
  - https://github.com/mitodl/ol-infrastructure/pull/4713 and a couple more I forgot :)
- Engaged in a wrestling match with Rootly's post incident retrospective creation tools. Lost, then after getting support from them won - kind of? It's not as clean as I'd like but details from yesterday's incident are documented at https://pe.ol.mit.edu/runbooks_post_mortems/20260603_xpro_outage/

> What am I working on tomorrow?

- https://github.com/mitodl/ol-infrastructure/issues/4702
  - I'm concerned that the issues Tobias raised around devs having neither the artifacts nor the permissions to run the streamlined EKS credentialing process we wrote is a deal breaker. We talked about potential solutions for a few seconds today, but I was mostly Retrospective-ing from the outage yesterday and didn't really have time to dig much.
  - I'd like to have our solution either finished, abandoned, or very thoroughly scoped by the end of the week.
```

## Example output (ambiguous timing, hybrid style)

```markdown
Sar

> Standup announcements

- None

> What did I work on yesterday/today?

- Wrote and deployed https://github.com/mitodl/ol-infrastructure/pull/4658
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
