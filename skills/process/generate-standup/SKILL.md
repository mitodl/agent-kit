---
name: generate-standup
description: >
  Generates a daily standup post from GitHub activity and Copilot agent session
  history, and posts it to the mitodl/hq Check-ins discussion. Use when asked
  to write, generate, or post a daily standup — fetches PR, issue, and
  code-review activity via the gh CLI, queries today's agent sessions from the
  session store, classifies all activity as yesterday/today, asks clarifying
  questions about blockers and off-GitHub work, renders the standup in the
  team's standard format, and posts it as a discussion comment with user
  confirmation.
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
  "meta":               { "username", "today", "since" },
  "checkin_discussion": { "id", "number", "title", "url", "createdAt" },
  "prs_authored":       [...],
  "prs_reviewed":       [...],
  "issues":             [...],
  "rfc_discussions":    [...]
}
```

- `meta.since` is 48 hours before `meta.today` (the activity window).
- `checkin_discussion` is the most recent Check-ins discussion in `mitodl/hq`
  — this is where the standup will be posted. Keep its `id` (GraphQL node ID)
  and `url` for Steps 4–5.
- Do **not** infer or fabricate activity beyond what the script returns.

---

## Step 1b — Query agent session history

Using the `sql` tool (`database: "session_store"`), fetch Copilot agent sessions
active within the same 48-hour window as `meta.since`:

```sql
SELECT
  s.id,
  s.repository,
  s.branch,
  s.summary,
  s.created_at,
  s.updated_at,
  c.title        AS checkpoint_title,
  c.overview     AS checkpoint_overview,
  c.work_done    AS checkpoint_work_done
FROM sessions s
LEFT JOIN checkpoints c ON c.session_id = s.id
WHERE s.updated_at >= '<meta.since>'
ORDER BY s.updated_at DESC
```

For sessions with **no checkpoints**, fetch the first user turn as a fallback:

```sql
SELECT s.id, s.repository, s.branch, t.user_message
FROM sessions s
JOIN turns t ON t.session_id = s.id AND t.turn_index = 0
WHERE s.updated_at >= '<meta.since>'
  AND NOT EXISTS (SELECT 1 FROM checkpoints c WHERE c.session_id = s.id)
ORDER BY s.updated_at DESC
```

**Summarization rules:**

| Evidence available | Action |
|--------------------|--------|
| Checkpoint with `work_done` / `overview` | Use as session summary |
| No checkpoint; has repo + branch + concrete first turn | Derive a brief summary from repo/branch + turn intent |
| No checkpoint; NULL repo or trivial/meta prompt | Skip — too weak to include |
| Session is for generating this standup | Skip — exclude to avoid circularity |

Store the resulting list of session summaries internally; you will use it in
Steps 3–4 to enrich GitHub-derived bullets and fill in non-GitHub work.

---

## Step 2 — Ask clarifying questions

Use a **single `ask_user` call** with all four fields at once.

Before making the call, identify **session-only work** from Step 1b: sessions
with `repository: null` or whose repository does not appear in any
`prs_authored` entry. Format them as a short suggestion list to embed in the
`description` of the `off_github` and `missing_yesterday` fields — this gives
the user context without pre-populating standup content for them.

```json
{
  "blockers": {
    "type": "string",
    "title": "Blockers",
    "description": "Are you blocked on anything? Include a link and the @handle of whoever needs to unblock you. Leave blank if none."
  },
  "announcements": {
    "type": "string",
    "title": "Announcements",
    "description": "Anything to announce not in GitHub? (OOO, special calls for review, new RFCs, etc.) Leave blank if none."
  },
  "off_github": {
    "type": "string",
    "title": "Off-GitHub work today",
    "description": "Meetings, planning, research, or other work that won't appear in GitHub. Leave blank if none.\n\nPossible session-only work detected:\n<bullet list of session-only summaries, or 'none detected'>"
  },
  "missing_yesterday": {
    "type": "string",
    "title": "Missing yesterday work",
    "description": "Completed work from yesterday not captured in GitHub (decisions, documents, off-GitHub tasks). Leave blank if none."
  }
}
```

---

## Step 3 — Classify activity

| Category | Signal |
|----------|--------|
| **Completed yesterday** | `state: "merged"` or `state: "closed"` (regardless of exact timestamp) |
| **Planned today** | `state: "open"` with `updatedAt` in today's window; draft PRs included |
| **Announcements** | User input from Step 2; RFC discussions created today by the user |
| **Blockers** | User input from Step 2; keywords "blocked", "waiting on", "depends on" |

**GitHub classification rules:**

- **Deduplication:** A PR appearing in both `prs_authored` and `prs_reviewed`
  should be listed once under the most relevant category.
- **Stale open PRs:** `state: "open"` with `updatedAt` before `meta.since`
  → skip (no recent activity).
- **Active-but-quiet open PRs:** `state: "open"` with `updatedAt` between
  `meta.since` and start-of-today (activity yesterday, no GitHub signal today)
  → place in an indented **"also in progress"** sub-section at the bottom of
  the today section. Never omit solely because the section is long.
- **Human reviewers only:** When assessing whether a PR needs review, exclude
  bots (GitHub Copilot, Google Gemini, Sentry, etc.).
- **RFC discussions:** Items in `rfc_discussions` were authored by you today
  → add to **announcements** with a link and a note to read/comment.
- **PR grouping:** After classifying into buckets, group PRs that share an
  identical normalized title across multiple repos into a single parent bullet
  with repo-specific sub-bullets. Only group within the same bucket — do not
  merge items from different sections (yesterday vs today):

  ```markdown
  - Configure bumpver for Concourse release pipeline:
    - [learn-ai #492](url)
    - [mit-learn #3220](url)
    - [mitxonline #3498](url)
  ```

**Incorporating agent sessions (from Step 1b):**

- **GitHub item wins:** If a session clearly maps to a PR or issue already in
  the GitHub data, enrich that existing bullet with narrative from the session
  summary — do **not** create a duplicate bullet.
- **Session-only work:** If a session represents substantive work with no
  corresponding GitHub artifact, add it as its own bullet under **yesterday**
  or **today** based on `updated_at` relative to `meta.today`.
- **Noise filters — skip a session if:**
  - it has no checkpoints and the first turn is trivial, meta, or
    unrelated to real engineering work
  - `repository` is NULL and the session lacks a concrete checkpoint summary
  - the session was for generating this standup
- **Classify by last activity:** Use `sessions.updated_at` to assign a session
  to yesterday vs. today, consistent with the `meta.since` / `meta.today`
  boundary.

---

## Step 4 — Render the standup

Use `meta.username` (the authenticated user's GitHub login) as the name.

```markdown
<Your Name>

> Standup announcements

- <item> — <link if applicable>
- Blockers: <description> — <link> @<handle>

> What did I work on yesterday?

- <concise description> — <link>

> What am I working on today?

- <concise description> — <link>
```

**Formatting rules:**

- Empty sections: write `- N/A`, never omit the section header.
- Blockers go under `> Standup announcements` as a bullet (not a separate
  section). Tag with `@handle` and include a link.
- Open PRs authored by the user that need human review → call out in
  announcements even if not flagged as blockers.
- Be concise but conversational; brief narrative context is welcome.
- Quantify where possible ("merged PR #42", "left 3 review comments").
- Always link to the relevant GitHub URL when available.
- **Multiple related items** → use indented sub-bullets, not inline commas:

  ```markdown
  - Merged uv migration PRs:
    - [learn-ai #435](url)
    - [mitxonline #3327](url)
  ```

---

## Step 5 — Confirm and post

Display the rendered standup, then use `ask_user` to confirm. Do not set a
default — require an explicit choice before posting:

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

On confirmation, post using the bundled script and the GraphQL node ID from
`checkin_discussion.id`:

```bash
echo "<rendered standup>" \
  | bash skills/process/generate-standup/scripts/post-standup-comment.sh \
      -d "<checkin_discussion.id>"
```

The script prints the comment URL on success.

---

## Example output

```markdown
Tobias Macey

> Standup announcements

- OOO Friday 2/28 — no coverage needed
- PRs needing review:
  - [#42 Add caching layer](https://github.com/mitodl/repo/pull/42)
  - [#43 Refactor auth module](https://github.com/mitodl/repo/pull/43)
- Blockers: Waiting on infra access to test staging deployment —
  [issue #88](https://github.com/mitodl/repo/issues/88) @ops-person

> What did I work on yesterday?

- Merged uv migration PRs across 3 repos:
  - [learn-ai #435](https://github.com/mitodl/learn-ai/pull/435)
  - [mitxonline #3327](https://github.com/mitodl/mitxonline/pull/3327)
  - [mit-learn #2975](https://github.com/mitodl/mit-learn/pull/2975)
- Left 3 review comments on [PR #41](https://github.com/mitodl/repo/pull/41)
- Published [RFC: Data retention policy](https://github.com/mitodl/hq/discussions/12)
- Investigated auth token refresh bug in `mitodl/mit-learn` (agent session;
  no PR yet)

> What am I working on today?

- Continue [PR #42: Add caching layer](https://github.com/mitodl/repo/pull/42)
- Triage [issue #91: Memory leak in worker](https://github.com/mitodl/repo/issues/91)
- Respond to comments on [RFC #12](https://github.com/mitodl/hq/discussions/12)
```

---

See [context script](scripts/get-standup-context.sh) for the GitHub
data-fetching implementation and [post script](scripts/post-standup-comment.sh)
for the comment posting implementation.
