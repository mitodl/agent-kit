---
name: screenshot-pr
description: >
  Capture UI changes for a pull request as screenshots with a re-runnable
  Playwright script. Use when asked to screenshot, capture, document or re-shoot
  UI for a PR, at desktop, tablet or mobile width. Every shot asserts on page
  content, so a gateway 5xx, an expired login, a stale dev-server build or the
  wrong user's data fails the run instead of being saved as an image. Covers
  Keycloak/APISIX logins (including stacks with more than one session),
  clicking through to a state, cropping portalled popovers, and re-shooting a
  named subset.
license: BSD-3-Clause
metadata:
  category: process
---

# Screenshot PR Changes

A screenshot tool will photograph a gateway error page, a logged-out page or last week's build, exit `0`, and hand you a plausible PNG. The job is knowing the picture shows what you think it shows.

**Requires** `uv`. Both scripts pin Playwright in a PEP 723 header, so `uv run` builds their environment. A machine that has never run them needs the pinned Chromium once:

```bash
uv run --with "playwright==1.61.0" playwright install chromium
```

The scripts live in `scripts/` beside this `SKILL.md`, wherever it was installed. Resolve that literal path now and use it for `<skill_dir>` below — your working directory is the project being screenshotted, and a shell variable does not survive between tool calls.

## The rule: every shot asserts

Each shot names text that must be **present** inside the region under review. That one check catches:

| Failure | What you'd otherwise get |
|---|---|
| Gateway 5xx or app error page | A picture of the error, exit `0` |
| Expired or partial login | The anonymous page, which often looks like a valid one |
| Stale dev-server build | Yesterday's UI |
| Wrong fixture, user or flag | The right layout with the wrong content |

Asserting on copy the branch adds makes the run a deploy check too.

Text that must be **absent** is optional. Use it only when two states in the set look alike — "no discount" and "logged out" can render the same card, told apart only by `Sign in` in the header. If you can't name the wrong page that would also pass your `expect`, leave `absent` empty; a check you had to invent reads as coverage without being any.

## Step 1 — Find the base URL

Stop at the first hit:

1. **Ingress hostnames** — for a k3d/Tilt or Kubernetes stack, check the `Tiltfile`, ingress YAML, or `kubectl -n <ns> get ingress`. These route through the gateway that holds the session.
2. **`.env` / `.env.local`** — `grep -E "(BASE_URL|SITE_URL|APP_URL)" .env .env.local 2>/dev/null`
3. **`docker-compose.yml`** — use the gateway port (`APISIX_PORT`, often `9080`), never the raw app port, or the session cookie isn't presented.
4. **Probe** — `for port in 9080 3000 5173 8000 8013; do curl -s -o /dev/null -w "%{http_code} localhost:$port\n" --max-time 1 "http://localhost:$port/"; done`

Confirm it answers (`curl -sk -o /dev/null -w "%{http_code}\n" "<base_url>/"`), and note the `login_url` — usually `<base_url>/login`. If nothing turns up, ask.

## Step 2 — Check auth

`capture.py` mints a fresh login per user on every run. It's built for local stacks and their test accounts: on a shared stack, use a dedicated low-privilege test account rather than a real one, and keep the copied script somewhere git doesn't track.

Run the helper by hand once to check the stack before you plan:

```bash
uv run <skill_dir>/scripts/get-auth-context.py \
  "<login_url>" /tmp/screenshot-auth.json \
  --username admin@odl.local --password localdev123 \
  --verify "<base_url>/api/v0/users/me/"
```

`--verify` takes a URL that returns the current user, and fails unless it names the user who signed in — landing somewhere other than `/login` proves nothing. Try `/api/v0/users/me/`, `/api/users/me/`, `/api/user/`, `/accounts/session/`. Repeat `--verify` once per session the pages depend on: a frontend proxying another service's API through its own host holds a separate session per upstream, and a context missing one renders that data as the anonymous state rather than an error.

`--verify` is mandatory and has no bypass. If nothing on the stack answers with the current user as JSON, signed-in shots can't be captured — say so and ask which URL to use. Signed-out shots can go ahead.

Form selectors default to Keycloak's; for another IdP pass `--username-selector`, `--password-selector` and `--submit-selector`. If login still fails, ask once for credentials and re-run.

> **Never mint a context in a headed browser.** Headed Chrome sends a different User-Agent from the headless captures, and a stack that binds sessions to the User-Agent silently swaps in an anonymous one.

## Step 3 — Propose a plan and confirm

Put the plan to the user in one message and wait: base URL, one line per shot (`<name> <path> [selector]`), any clicks needed first, viewports, and an output directory (`screenshots/<branch-or-pr-slug>/` is a fine default). Re-show it after each revision.

Keep every image in **one flat directory** with descriptive names; the next step is a drag-and-drop into a GitHub comment.

| Viewport | Width × height |
|---|---|
| desktop | 1440 × 1100 |
| tablet | 768 × 1024 |
| mobile | 390 × 844 |

Shoot a width only where the diff changes its layout, and say which you skipped.

## Step 4 — Capture

Copy **both** `scripts/capture.py` and `scripts/get-auth-context.py` into the project's scratch or feature-work directory (the first calls the second as a sibling). Fill in the marked block and the `SHOTS` list; each `Shot` field is documented where it's declared.

Choose `TARGET` by looking at the page. It is both the crop and the scope of every presence check, so a wrong value crops to the wrong element while every assertion still passes.

```bash
uv run capture.py                # everything
uv run capture.py --only hero    # re-shoot a subset under the same assertions
uv run capture.py --list         # the plan, no browser
```

The script retries only until the app answers — a refused connection, a navigation timeout or a 5xx (a watch-mode dev server gets OOM-killed mid-run and takes a minute to recompile). Anything after that, a 4xx, a failed assertion or a missing selector, fails at once. Output is staged and only replaces the previous set once the app has answered: a stack that's down leaves the old images alone, and a shot that failed leaves none.

## Step 5 — Look at every image

Open each PNG before reporting. The assertions cover what you thought to check; your eyes cover the rest — collapsed layout, truncated text, a skeleton that never resolved, the wrong breakpoint.

## Step 6 — Report

Give the absolute directory path and each file with its size, and say plainly which shots failed and why, which viewports you skipped, and any state no available fixture could produce (and what fixture would).

Attaching the images is the user's step — GitHub has no upload API — so hand over the directory.
