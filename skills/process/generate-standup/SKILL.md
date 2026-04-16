---
name: generate-standup
description: >
  Generates a daily standup post from GitHub activity and posts it to the
  mitodl/hq Check-ins discussion. Use when asked to write, generate, or post
  a daily standup — fetches PR, issue, and code-review activity via the gh CLI,
  classifies it as yesterday/today, asks clarifying questions about blockers and
  off-GitHub work, renders the standup in the team's standard format, and posts
  it as a discussion comment with user confirmation.
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

## Step 2 — Ask clarifying questions

Ask these one at a time, waiting for each answer:

1. **Blockers** — Are you blocked on anything? Include a link and the handle
   of whoever needs to be tagged.
2. **Announcements** — Anything to announce not captured above? (OOO, PRs
   needing review, new RFCs, etc.)
3. **Off-GitHub work today** — Meetings, planning, research, or other work
   that won't appear in GitHub?
4. **Missing yesterday work** — Completed work from yesterday not captured
   (decisions, documents, off-GitHub tasks)?

---

## Step 3 — Classify GitHub activity

| Category | Signal |
|----------|--------|
| **Completed yesterday** | `state: "merged"` or `state: "closed"` (regardless of exact timestamp) |
| **Planned today** | `state: "open"` with `updatedAt` in today's window; draft PRs included |
| **Announcements** | User input from Step 2; RFC discussions created today by the user |
| **Blockers** | User input from Step 2; keywords "blocked", "waiting on", "depends on" |

**Classification rules:**

- **Deduplication:** A PR appearing in both `prs_authored` and `prs_reviewed`
  should be listed once under the most relevant category.
- **Stale open PRs:** `state: "open"` with `updatedAt` before `meta.since`
  → skip (no recent activity).
- **Human reviewers only:** When assessing whether a PR needs review, exclude
  bots (GitHub Copilot, Google Gemini, Sentry, etc.).
- **RFC discussions:** Items in `rfc_discussions` were authored by you today
  → add to **announcements** with a link and a note to read/comment.

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

Display the rendered standup, then ask:

> "Shall I post this as a comment on [**\<title\>**](\<checkin_discussion.url\>)?
> (yes / no / edit first)"

**Do not post until the user explicitly confirms.**

On confirmation, post using the GraphQL node ID from `checkin_discussion.id`:

```bash
DISCUSSION_ID="<checkin_discussion.id>"
BODY="<rendered standup>"

jq -n \
  --arg discussionId "$DISCUSSION_ID" \
  --arg body "$BODY" \
  --arg query '
    mutation($discussionId: ID!, $body: String!) {
      addDiscussionComment(input: {discussionId: $discussionId, body: $body}) {
        comment { url }
      }
    }' \
  '{query: $query, variables: {discussionId: $discussionId, body: $body}}' \
| gh api graphql --input -
```

Print the comment URL from `addDiscussionComment.comment.url`.

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

> What am I working on today?

- Continue [PR #42: Add caching layer](https://github.com/mitodl/repo/pull/42)
- Triage [issue #91: Memory leak in worker](https://github.com/mitodl/repo/issues/91)
- Respond to comments on [RFC #12](https://github.com/mitodl/hq/discussions/12)
```

---

See [context script](scripts/get-standup-context.sh) for the full GitHub
data-fetching implementation.
