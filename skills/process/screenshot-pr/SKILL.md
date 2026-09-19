---
name: screenshot-pr
description: >
  Capture UI changes for a pull request as screenshots with a re-runnable
  Playwright script. Use when asked to screenshot, capture, document, re-shoot
  or recapture UI for a PR, to show what a change looks like, or to grab images
  for a PR description or comment at desktop, tablet or mobile width. Covers
  asserting on page content so a gateway 5xx, an expired login, a stale
  dev-server build or the wrong per-user state fails the run instead of being
  saved as an image; Keycloak/APISIX auth contexts, including stacks holding
  more than one session; clicking through to a state and cropping popovers and
  other portalled overlays; and re-shooting a named subset.
license: BSD-3-Clause
metadata:
  category: process
---

# Screenshot PR Changes

Taking the picture is the easy part. Knowing the picture shows what you think
it shows is the job: a screenshot tool will photograph a gateway error page, a
logged-out page, or last week's build, exit `0`, and hand you a plausible PNG.

**Requires:** `uv`. The two scripts carry PEP 723 headers pinning Playwright,
so `uv run` builds their environment; nothing needs installing into the
project. Playwright ships a Chromium revision per version, so a machine that
has never run these needs that revision once:

```bash
uv run --with "playwright==1.61.0" playwright install chromium
```

**This skill's scripts** live in `scripts/` beside this file — under
`~/.claude/skills/screenshot-pr/` when installed, or
`skills/process/screenshot-pr/` in an agent-kit checkout. Your working
directory is the project being screenshotted, not either of those, so resolve
the literal path now and substitute it below; a shell variable does not
survive from one tool call to the next.

---

## The one rule: every shot asserts

Before a shot is written to disk, require text that must be **present** on the
page. Where a state is defined by what it lacks, also require text that must be
**absent**. One mechanism catches every common failure:

| The assertion catches | Without it you get |
|---|---|
| Gateway 5xx or an app error page | A screenshot of the error, and exit code 0 |
| An expired or partial login | The anonymous page, which often looks exactly like a valid signed-in one |
| A stale dev-server build | Yesterday's UI, silently |
| The wrong fixture, user, or feature flag | The right layout showing the wrong content |

Asserting new copy is present on the states that should have it also turns the
capture run into a deploy check: a dev server still serving the old bundle
fails the run instead of quietly filling the PR with stale images.

**Absence is conditional, not required.** It earns its place only when two
states in the set render similarly enough that presence cannot tell them
apart — "this user has no discount" and "this user is logged out" both render
the same default card, and what separates them is `Sign in` in the header,
outside the crop. The test: if you cannot name the *wrong* page that would
also satisfy your `expect`, leave `absent` empty. An absence check you had to
reach for is worse than none, because it reads as coverage.

---

## Step 1 — Find the base URL

Stop at the first hit.

1. **Ingress hostnames.** A k3d/Tilt or Kubernetes stack serves the app on a
   real hostname, not a localhost port — check `Tiltfile`, `*.yaml` ingress
   definitions, or `kubectl -n <ns> get ingress`. Prefer these; they route
   through the gateway that holds the session.
2. **`.env` / `.env.local`**: `grep -E "(BASE_URL|SITE_URL|APP_URL)" .env .env.local 2>/dev/null | head`
3. **`docker-compose.yml`**, if the project uses one: note `APISIX_PORT`
   (often `9080`). Authenticated pages must be captured through the gateway
   port, never the raw app port, or the session cookie is not presented.
4. **Probe**: `for port in 9080 3000 5173 8000 8013; do curl -s -o /dev/null -w "%{http_code} localhost:$port\n" --max-time 1 "http://localhost:$port/"; done`

Confirm whatever you found is actually serving, because a hostname out of a
stale `Tiltfile` turns every later step into a confusing failure:

```bash
curl -sk -o /dev/null -w "%{http_code}\n" "<base_url>/"
```

Record `base_url`, and `login_url` — conventionally `<base_url>/login`, but
check the app's routes. If nothing turns up, ask.

---

## Step 2 — Establish an auth context

[`scripts/get-auth-context.py`](scripts/get-auth-context.py) writes a
Playwright storage-state JSON, which is what `capture.py` loads per user:

```bash
uv run <skill_dir>/scripts/get-auth-context.py \
  "<login_url>" /tmp/screenshot-auth.json \
  --username admin@odl.local --password localdev123 \
  --verify "<base_url>/api/v0/users/me/"
```

`capture.py` calls this itself, once per user in its shot list, so run it by
hand only to check the stack before writing a plan.

**Verify against an authenticated endpoint, not the URL.** Landing somewhere
other than `/login` proves nothing. `--verify` fetches a URL that returns the
current user and fails unless the response names the user you logged in as.
Usual candidates: `/api/v0/users/me/`, `/api/users/me/`, `/api/user/`,
`/accounts/session/`. Pass `--verify` more than once when the app depends on
more than one session: a frontend that proxies a second service's API through
its own host holds a **separate** session per upstream, and logging in mints
only the first. A context holding one of two captures pages whose per-user
data silently 403s — which renders as the ordinary anonymous state, not as an
error.

If no endpoint answers with the current user in JSON, say so and fall back to
asserting on signed-in-only copy in every shot; do not skip verification
silently.

The form selectors default to Keycloak's. For another IdP — a hosted Auth0 or
Okta page, a plain Django login — pass `--username-selector`,
`--password-selector` and `--submit-selector`. If login still fails, ask once
for credentials or an existing auth-file path, in a single question, and
re-run. Do not retry indefinitely.

> **Never mint the context in a headed browser.** A headed Chrome sends
> `Chrome/<major>.0.0.0` while captures run headless as
> `HeadlessChrome/<full-build>`. On a stack that binds the session to the
> User-Agent, the server rejects the mismatched session and silently issues an
> anonymous one — no error, just logged-out screenshots. Both ends of this
> script run headless, which is the point.

---

## Step 3 — Propose a plan and confirm

Work out which routes the diff touches, then put the plan to the user in one
question and wait: the base URL, one line per shot (`<name> <path> [selector]`),
any interactions needed before the shot, the viewports, and the output
directory (`screenshots/<branch-or-pr-slug>/` is a reasonable default to
propose). Re-display after each revision; do not start capturing until they
confirm.

Put every image in **one flat directory** with descriptive filenames — the
usual next step is a bulk drag-and-drop into a GitHub comment, and a
`desktop/`, `tablet/`, `mobile/` tree makes that three uploads and an
ambiguous set of names.

Default viewports, when the user has no preference:

| Name | Width | Height | Reference |
|------|-------|--------|-----------|
| desktop | 1440 | 1100 | Laptop |
| tablet | 768 | 1024 | iPad |
| mobile | 390 | 844 | iPhone 14 |

Shoot the full grid only where layout actually changes; a breakpoint that the
diff does not touch is noise in the PR. Say in the report which widths were
skipped.

---

## Step 4 — Capture

Copy [`scripts/capture.py`](scripts/capture.py) **and**
[`scripts/get-auth-context.py`](scripts/get-auth-context.py) out of this
skill's `scripts/` directory into the project's scratch or feature-work
directory — the first invokes the second as a sibling. Then fill in the block
marked in the file: `BASE`, `LOGIN_URL`, `VERIFY_URLS`, `USERS`, `TARGET`,
`OVERLAY`, `OUT` and the `SHOTS` list.

`TARGET` is the one to think about rather than accept. It is both the crop and
the scope of every presence assertion, so a wrong value does not error — it
crops every shot to the wrong element while all the assertions still pass.
Look at the page and name the region under review.

```bash
uv run capture.py                    # everything
uv run capture.py --only hero        # re-shoot a subset
uv run capture.py --list             # the plan, no browser
```

A `Shot` is a row of data, which is what makes the set re-runnable: after a
review comment or a copy change, `--only <name>` re-takes one image with the
same assertions it was captured under. Reconstructing an ad-hoc command line
weeks later is where the assertions get dropped.

What it handles per shot:

- **Assertions**, present and absent, as separate lists. Presence is checked
  inside `TARGET`, before and again after the settle; absence is checked
  against the whole page, because the copy that gives away a logged-out state
  usually lives in the header. The response status is checked too, so a 404
  fails in a second rather than timing out on a missing `TARGET`.
- **Interactions.** `click=[...]` presses controls in order before the
  assertions, so `expect` can name what the click revealed and the
  interaction is asserted rather than assumed. These are real `page.click()`
  calls, which move Chrome's interaction modality to "pointer" — an injected
  `element.click()` leaves it unset, so a control whose handler calls
  `focus()` photographs with a `:focus-visible` ring the real UI never shows.
  `scroll_to=` is not for framing, since an element screenshot of `TARGET`
  captures its full height regardless; it is for content that does not render
  until visible, and for overlay shots, whose crop is viewport-bounded.
- **Overlays.** `opens_overlay=` clicks a trigger and crops to the union of
  `TARGET` and the overlay, computed from live bounding boxes, so a popover
  rendered in a portal frames with the control that opened it.
  `overlay_expect=` is required alongside it and filters `OVERLAY`, which is
  deliberately broad and otherwise matches whichever portal comes first in the
  DOM.
- **Fresh auth per run**, for as many users as the shot list mentions.
- **Sequential execution with retry.** A local dev server running in watch
  mode (`next dev` and friends) holds the whole compiled app in memory and
  gets OOM-killed partway through a long capture run; the orchestrator
  restarts it and the next request recompiles, taking most of a minute. Retry
  with a wait, rather than losing the shot. A gateway 5xx retries for the same
  reason — the gateway stays up and answers while the upstream recompiles.
  Assertion failures and a 4xx do not: the page says the same thing next time.
- **Staged output.** Shots are captured to a temporary directory and moved
  into place at the end, so a run that never reached the app leaves the last
  good set alone — including a signed-out `--only`, where there is no login to
  fail first. Once the app has answered, replacement is unconditional: a shot
  that failed leaves no image behind, because the command exits non-zero but
  nobody re-reads the log before dragging the directory into a comment.

Two things to watch in the output:

- At `device_scale_factor=2` a full-viewport PNG runs to megabytes. Cropped
  element shots of the same page land in the tens of kilobytes. Crop.
- A `click` moves the pointer away afterwards so the control does not
  photograph hovered; `opens_overlay` deliberately does not, since moving off
  can dismiss what was opened.

---

## Step 5 — Look at every image

Open each PNG before reporting. This is on top of the assertions, not instead
of them: the assertions cover what you thought to assert, your eyes cover the
rest — a collapsed layout, a truncated string, a loading skeleton that never
resolved, the wrong breakpoint.

---

## Step 6 — Report

Give the absolute directory path and list the files with their sizes, and
state plainly:

- which shots failed, and with what error;
- which viewports were not captured;
- any state you could not reach with the available fixtures, rather than
  quietly omitting it.

If a state cannot be photographed because no fixture produces it, say so and
say what fixture would be needed. That is a finding, not a gap to hide.

Attaching the images is the user's step — GitHub has no API for uploading them
— so hand over the directory and let them drag it into the PR or comment box.
Do not try to attach them yourself.
