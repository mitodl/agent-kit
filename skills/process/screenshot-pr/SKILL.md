---
name: screenshot-pr
description: >
  Take screenshots of UI changes for a pull request using shot-scraper. Use
  this skill when asked to screenshot, capture, or document UI changes in a
  PR — discovers the local dev base URL, establishes auth (defaulting to the
  admin@odl.local Keycloak/APISIX test user), proposes a screenshot plan at
  desktop/tablet/mobile viewports, asks for confirmation, then runs
  shot-scraper multi to produce the images.
license: BSD-3-Clause
metadata:
  category: process
---

# Screenshot PR Changes

Capture UI changes for a pull request across three standard viewports using
[shot-scraper](https://shot-scraper.datasette.io/en/stable/).

**Requires:** `shot-scraper` available on `PATH` (it is a system-level utility, not a per-project dependency — do not install it with `uv` or `pip` inside the project).

Check it is present before proceeding:

```bash
which shot-scraper && shot-scraper --version
```

If the command is missing, ask the user to install it globally (e.g. `uv tool install shot-scraper --global`, `pipx install shot-scraper`, or their preferred method) and confirm Chromium is available (`shot-scraper install`). Do not install it yourself.

---

## Step 0 — Discover the base URL

Run these checks in order and stop at the first hit:

**a) Check `.env` (or `.env.local`) in the repo root:**

```bash
grep -E "(BASE_URL|SITE_URL|APP_URL)" .env .env.local 2>/dev/null | head -10
```

Look for vars like `MITX_ONLINE_BASE_URL`, `SITE_URL`, `NEXT_PUBLIC_BASE_URL`.

**b) Check `docker-compose.yml` for env defaults:**

```bash
grep -E "(BASE_URL|APISIX_PORT|_PORT)" docker-compose.yml 2>/dev/null | head -20
```

Pay attention to `APISIX_PORT` (default `9080`) — screenshots of authenticated
pages must go through the APISIX port, **not** the raw app port, so session
cookies work. If the app hostname is `api.open.odl.local` and APISIX is at
`8065`, the base URL for screenshots is `http://api.open.odl.local:8065`.

**c) Probe common ports for a live server:**

```bash
for port in 9080 3000 5173 8000 8013; do
  curl -s -o /dev/null -w "%{http_code} localhost:$port\n" --max-time 1 "http://localhost:$port/" 2>/dev/null
done
```

**d) Check the README or session context** for any URLs mentioned.

Record two values: `base_url` (used for all screenshot URLs) and `login_url`
(`<base_url>/login`, used for auth). If nothing is found, ask the user.

---

## Step 1 — Establish an auth context

Authentication in this dev stack flows through **APISIX → Keycloak**. The
`shot-scraper --auth` flag takes a Playwright storage-state JSON file; this
step generates or reuses one.

### 1a — Check for an existing context

```bash
test -f /tmp/screenshot-auth.json && echo "exists"
```

If the file exists and is less than 8 hours old, reuse it and skip to Step 2.

```bash
find /tmp/screenshot-auth.json -mmin -480 2>/dev/null
```

### 1b — Try automated login

Use the helper script with the **default dev credentials** (`admin@odl.local` /
`admin`):

```bash
"$(dirname $(which shot-scraper))/python3" \
  skills/process/screenshot-pr/scripts/get-auth-context.py \
  "<login_url>" /tmp/screenshot-auth.json \
  --username admin@odl.local \
  --password admin
```

Using `dirname $(which shot-scraper)` guarantees the script runs under the same
Python — and therefore the same Playwright install — as shot-scraper itself,
regardless of what `python3` resolves to on the host.

If the script exits `0`, auth context is ready — proceed to Step 2.

### 1c — Fallback: prompt for credentials

If automated login fails (non-ODL project, different Keycloak realm, or
credentials rejected), use a **single `ask_user` call**:

```json
{
  "username": {
    "type": "string",
    "title": "Username / email",
    "description": "Leave blank to skip authentication entirely."
  },
  "password": {
    "type": "string",
    "title": "Password",
    "description": "Leave blank to skip authentication entirely."
  },
  "auth_file": {
    "type": "string",
    "title": "Existing auth context file (optional)",
    "description": "Path to an existing Playwright storage-state JSON if you have one. Leave blank to use credentials above or skip auth."
  }
}
```

**If credentials supplied:** re-run the helper script with those values. Report
the error clearly if it fails again — do not retry indefinitely.

**If an existing auth file path supplied:** use that file directly; skip the
helper script.

**If all fields blank:** set `auth_file=""` and proceed without authentication.

### 1d — Last resort: manual interactive login

If no credentials are available and no auth file exists, the user can log in
via a real browser window:

```bash
shot-scraper auth "<login_url>" /tmp/screenshot-auth.json
```

> ⚠️ **User-Agent mismatch warning.** On stacks that bind the session to the
> User-Agent (e.g. Keycloak/APISIX), this fallback can silently produce
> logged-out screenshots. `shot-scraper auth` opens a headed browser whose UA
> is `Chrome/<major>.0.0.0`; `shot-scraper multi` captures with a headless
> browser whose UA is `HeadlessChrome/<full-build>`. The server rejects the
> mismatched session and silently issues an anonymous one — no error, just
> logged-out shots. Passing `--user-agent` to `shot-scraper auth` does not
> help; the command ignores it. The `get-auth-context.py` path avoids this
> because both ends run headless.

After establishing any auth context (any path), confirm it is actually
authenticated before proceeding: load a known login-required page and verify
the response looks authenticated, not an anonymous or redirect response.

---

## Step 2 — Gather context

With the base URL confirmed and auth established, review what you already know:

- **PR description and title** — what features or pages are changing?
- **Diff summary** — which routes, templates, components, or CSS files are
  touched? Look for URL path patterns, view names, or named URL patterns.
- **Session context** — any page names or paths mentioned during this session.

Build a list of paths to capture. For each one, construct the full URL as
`<base_url><path>`.

---

## Step 3 — Propose a screenshot plan and confirm

Present the plan clearly and ask for confirmation with a **single `ask_user`
call** (allow any number of edit rounds before proceeding):

```json
{
  "base_url": {
    "type": "string",
    "title": "Base URL",
    "description": "Root URL for all screenshots (must go through APISIX if auth is needed). Discovered: <discovered_value_or_'not_detected'>."
  },
  "shots": {
    "type": "string",
    "title": "Screenshot plan",
    "description": "One shot per line: <label> <path> [css-selector]. Labels become filenames. Example:\n  homepage /\n  dashboard /dashboard\n  course-detail /courses/1 .course-hero",
    "default": "<generated list, one per line>"
  },
  "interactions": {
    "type": "string",
    "title": "Interactions before screenshot (optional)",
    "description": "Describe in plain English what needs to happen on the page before the shot is taken. The agent will translate this into JavaScript. One instruction per shot (reference the label). Examples:\n  dashboard: scroll to the My Learning section\n  dashboard: click the 'Show all' button, then scroll to the bottom of the list\n  course-detail: expand the accordion labelled 'Upcoming runs'\nLeave blank to screenshot the page as it loads."
  },
  "output_dir": {
    "type": "string",
    "title": "Output directory",
    "description": "Where to save screenshots.",
    "default": "screenshots/<pr-slug-or-branch>"
  }
}
```

Re-display the updated plan after each revision. Do not proceed until the user
explicitly confirms.

---

## Step 4 — Build the shot-scraper YAML

Write a temp file (e.g. `/tmp/screenshot-pr-<branch>.yml`). For every
confirmed shot, emit three entries — one per viewport:

**Standard viewports:**

| Name | Width | Height | Device reference |
|------|-------|--------|------------------|
| desktop | 2560 | 1440 | 1440p monitor |
| tablet | 768 | 1024 | iPad (standard) |
| mobile | 390 | 844 | iPhone 14 |

Always set both `width` and `height` to get a viewport-sized screenshot, not a full-page capture.

**`wait` guidance:** `shot-scraper` stops at `networkidle`, which fires on the
initial HTML shell — before client-side frameworks (Next.js, React Query, etc.)
have hydrated and fetched their data. Always add `wait: 5000` unless you have
confirmed the page is fully server-rendered. Reduce it only if you have verified
the content is present sooner.

**Translating interactions to JavaScript (`javascript` key):**

If the user described interactions in Step 3, translate each one into a
`javascript` value for the relevant shots. The JS runs after `wait` completes,
so the page is fully loaded. Use `await` freely — shot-scraper evaluates the
value as an async function body.

Common patterns:

| User says | JavaScript |
|-----------|------------|
| scroll to the My Learning section | `document.querySelector('#my-learning')?.scrollIntoView({block:'start'})` |
| scroll to element with text "Upcoming" | `[...document.querySelectorAll('*')].find(el => el.childElementCount === 0 && el.textContent.trim() === 'Upcoming')?.scrollIntoView({block:'start'})` |
| click the button labelled "Show all" | `[...document.querySelectorAll('button, a')].find(el => el.textContent.trim() === 'Show all')?.click()` |
| expand the accordion | `document.querySelector('[aria-expanded="false"]')?.click()` |
| scroll to bottom of page | `window.scrollTo(0, document.body.scrollHeight)` |
| wait for an element to appear | `await new Promise(r => { const i = setInterval(() => { if (document.querySelector('.target')) { clearInterval(i); r(); } }, 100); setTimeout(() => { clearInterval(i); r(); }, 5000); })` |

**Important:** shot-scraper runs `javascript` via `page.evaluate()`, which does
not accept top-level `await`. Wrap any async code in an immediately-invoked
async function (IIFE):

```javascript
(async () => {
  document.querySelector('#my-learning')?.scrollIntoView({block: 'start'});
  await new Promise(r => setTimeout(r, 500));
})()
```

For purely synchronous interactions (scroll, click with no network trigger) the
IIFE wrapper is optional — a plain expression works fine:

```javascript
document.querySelector('#my-learning')?.scrollIntoView({block: 'start'})
```

When a click triggers a network request or animation, always use the IIFE and
wait before the screenshot resolves. Suggested minimums:
- Simple scroll: 500ms
- CSS expand/collapse animation (e.g. MUI Accordion): 2000ms
- Click that triggers a network request: 2000ms+

If the same interaction applies to all three viewport shots for a label, repeat
the `javascript` value on all three entries.

```yaml
- url: "<base_url><path>"
  output: "<output_dir>/desktop/<label>.png"
  width: 2560
  height: 1440
  wait: 5000
  # selector: "<selector>"   # include only when set

- url: "<base_url><path>"
  output: "<output_dir>/tablet/<label>.png"
  width: 768
  height: 1024
  wait: 5000

- url: "<base_url><path>"
  output: "<output_dir>/mobile/<label>.png"
  width: 390
  height: 844
  wait: 5000
```

Create output directories:

```bash
mkdir -p <output_dir>/{desktop,tablet,mobile}
```

---

## Step 5 — Run shot-scraper

**Prefer sequential `shot` calls over `multi` when any shot uses `wait: 5000`
or a `javascript` interaction.** `shot-scraper multi` runs all entries in
parallel; with heavy JS pages and long waits this will time out. Instead, run
one `shot-scraper shot` per viewport:

```bash
SHOT_SCRAPER_AUTH="${auth_file:+--auth \"$auth_file\"}"
JS="<javascript expression>"

shot-scraper shot "<url>" $SHOT_SCRAPER_AUTH --browser chromium \
  --width 2560 --height 1440 --wait 5000 --javascript "$JS" \
  -o "<output_dir>/desktop/<label>.png"

shot-scraper shot "<url>" $SHOT_SCRAPER_AUTH --browser chromium \
  --width 768 --height 1024 --wait 5000 --javascript "$JS" \
  -o "<output_dir>/tablet/<label>.png"

shot-scraper shot "<url>" $SHOT_SCRAPER_AUTH --browser chromium \
  --width 390 --height 844 --wait 5000 --javascript "$JS" \
  -o "<output_dir>/mobile/<label>.png"
```

`shot-scraper multi` is fine for simple pages with no `javascript` and a short
`wait` (≤1000ms). If any shot fails, note which ones and continue.

---

## Step 6 — Report results

List created files by viewport:

```
desktop/  (N files)   tablet/  (N files)   mobile/  (N files)
  homepage.png          homepage.png          homepage.png
  dashboard.png         dashboard.png         dashboard.png
```

If any shots failed, list them with the HTTP status or error.

Suggest next steps if relevant:
- Add screenshots to the PR description: `gh pr edit --body-file -`.
- Re-run with `--no-clobber` to skip already-captured shots on retry.
- If auth expired, delete `/tmp/screenshot-auth.json` and re-run from Step 1.
- For pages requiring state that's hard to automate (e.g. mid-checkout flows),
  use `shot-scraper shot -i <url> --auth /tmp/screenshot-auth.json` to open
  an interactive browser with the session pre-loaded.

---

See [get-auth-context.py](scripts/get-auth-context.py) for the Playwright
login helper.
