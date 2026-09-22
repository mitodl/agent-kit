---
name: run-canary-locally
description: >
  Get a passing local run of the Playwright canary specs in
  src/ol_concourse/pipelines/canaries. Use when running, debugging, or
  reproducing a canary journey outside Concourse — triaging a red
  canary-<property> build, opening a failed run's trace.zip, iterating on a spec
  before shipping it, or reproducing a pipeline failure in the exact image
  Concourse uses. Covers npm ci, the browser install npm ci does NOT do,
  CANARY_BASE_URL, supplying credentials without a lockout, and the trace viewer.
license: BSD-3-Clause
metadata:
  category: process
---

# Running the Canaries Locally

**Repository:** [`mitodl/ol-infrastructure`](https://github.com/mitodl/ol-infrastructure).
Every unqualified path below is relative to that checkout.

## When to use

- A `canary-<property>` build went red and you want to reproduce it.
- You are writing or changing a spec and need the edit/run loop.
- You want to read a failed run's trace.

For *what* to write and where it goes, use the `add-canary-journey` skill. This
skill is only about getting a run to happen on your machine.

The authoritative design notes live in
[`src/ol_concourse/pipelines/canaries/AGENTS.md`](https://github.com/mitodl/ol-infrastructure/blob/main/src/ol_concourse/pipelines/canaries/AGENTS.md)
and [`README.md`](https://github.com/mitodl/ol-infrastructure/blob/main/src/ol_concourse/pipelines/canaries/README.md). This
skill does not restate them; it gets you to a passing run.

## Prerequisites

- Node and `npm` on PATH. There is no `uv`/Python involvement in a canary run —
  Python only renders the *pipeline*, never the tests.
- Network reach to the target property. Every current target is publicly
  reachable, so no VPN.

All commands below run from the canary directory:

```bash
cd src/ol_concourse/pipelines/canaries
```

## Step 1 — Install, including the part `npm ci` does not do

```bash
npm ci
npx playwright install chromium
```

**Both lines are required.** In Playwright 1.63 the `playwright`,
`playwright-core` and `@playwright/test` packages all have
`hasInstallScript=false` and ship no `install.js` — verified in
`package-lock.json` — so `npm ci` installs six packages and **downloads no
browser at all**. Skipping the second line gets you:

```
browserType.launch: Executable doesn't exist at
/home/<you>/.cache/ms-playwright/chromium_headless_shell-1243/...
```

which reads like a broken install and is not one.

This is also why the pipeline does not need that second line: browsers are baked
into the `mcr.microsoft.com/playwright` image at a revision keyed to the exact
Playwright version, which is the whole reason `package.json`'s pin is the single
source of truth for the image tag.

Install only `chromium` unless you are specifically testing WebKit — the
pipeline schedules Chromium alone.

## Step 2 — Choose a target

`CANARY_BASE_URL` is **required and never defaults** — `playwright.config.ts`
throws without it, because a canary silently pointed at the wrong environment
reports green while the real one burns.

```bash
export CANARY_BASE_URL=https://rc.learn.mit.edu   # what canary-mit-learn targets
```

Point it at a local stack the same way (`http://localhost:8063` or whatever the
app serves). Specs must not branch on the base URL — assert what is true of
every environment.

## Step 3 — Run the anonymous journeys first

Most `mit-learn` journeys need no login, so start here and stay here if you can:

```bash
npx playwright test specs/mit-learn --project=chromium --reporter=list
```

To run a subset — note you **cannot use `.only`**, because `forbidOnly` is on
unconditionally and will fail the whole run on purpose. Filter instead:

```bash
npx playwright test specs/mit-learn/homepage.spec.ts          # one file
npx playwright test specs/mit-learn -g "renders the channel"   # by title
```

Useful while iterating:

| Flag | Why |
|---|---|
| `--headed` | Watch it drive a real window. |
| `--ui` | Time-travel runner; best for building a locator. |
| `--debug` | Step through with the inspector. |
| `--trace on` | Force a trace on a **passing** run (`retain-on-failure` means a pass leaves none). |
| `--repeat-each=3` | Prove a journey is stable, not lucky. |

For `mit-learn`, the journeys needing no credentials are `homepage.spec.ts`,
`search-direct-url.spec.ts` and `channel-and-drawer.spec.ts`;
`login-and-search.spec.ts` is the signed-in one.

## Step 4 — Credentials, which are the sharp edge

> [!WARNING]
> **A mistyped password is not a free retry.** Realm `olapps` is configured
> `failureFactor=10`, `permanentLockout=true`, `maxTemporaryLockouts=1`,
> `maxDeltaTimeSeconds=43200`. Ten consecutive failures earn one temporary
> lockout; the **next ten permanently disable the account**, and only an admin
> can undo that. The 12-hour counter reset means failures accumulate rather than
> ageing out.

Two consequences that are easy to miss:

1. Your local attempts and the pipeline's land on the **same account**. The
   scheduled canary is spending attempts every 10 minutes. Hand-debugging a
   login against `canary_mit_learn` while it is already failing is how the
   account gets disabled.
2. Rotation must change Keycloak **and** the SOPS/Vault value together. The gap
   between the two is itself enough to disable the account, and it presents as a
   captcha error rather than an auth error.

### Prefer your own account

**Do not use the pipeline's credential for local work.** Provision yourself a
separate account in the `olapps` realm on `sso-qa.ol.mit.edu`. A usable one
needs all of:

- `emailVerified=true` and an **empty** `requiredActions` — the realm sets
  `verifyEmail=true` and has `VERIFY_EMAIL` as a *default action*, so a freshly
  created user cannot log in until it is cleared.
- the `fullName` attribute, which the realm's user profile marks required and
  which **rejects parentheses**.
- **one manual login**, to consume first-login onboarding. Otherwise login lands
  on `/onboarding?…&is_new_user=1` and `sign-in.ts` throws by design.

Then supply it without putting the secret in shell history or a file:

```bash
export CANARY_USER_EMAIL='you+canary@mit.edu'
read -rs CANARY_USER_PASSWORD && export CANARY_USER_PASSWORD
npx playwright test specs/mit-learn/login-and-search.spec.ts --project=chromium
```

`read -rs` does not echo and does not enter history. Unset it when done:
`unset CANARY_USER_PASSWORD`.

### If you genuinely need the pipeline credential

Pipe it out of SOPS so the value is never typed, pasted, or written to disk:

```bash
REPO=$(git rev-parse --show-toplevel)
SECRETS="$REPO/src/bridge/secrets/concourse/operations.production.yaml"
KEY='["pipelines"]["infrastructure/canary_mit_learn"]'

CANARY_USER_EMAIL=$(sops -d --extract "$KEY"'["email"]' "$SECRETS") \
CANARY_USER_PASSWORD=$(sops -d --extract "$KEY"'["password"]' "$SECRETS") \
  npx playwright test specs/mit-learn/login-and-search.spec.ts --project=chromium
```

Requires KMS access.

To get the same credential into a Docker run, **export it and forward the
variable *name* only**:

```bash
export CANARY_USER_EMAIL=$(sops -d --extract "$KEY"'["email"]' "$SECRETS")
export CANARY_USER_PASSWORD=$(sops -d --extract "$KEY"'["password"]' "$SECRETS")
docker run --rm -e CANARY_USER_EMAIL -e CANARY_USER_PASSWORD ...
```

`-e NAME` with no `=` tells Docker to copy the value from your environment.
Verified both halves of why that is the right form: the container does receive
the value, and `ps -eo args` on the host shows only `-e CANARY_USER_PASSWORD`,
never the password.

The two forms to avoid, and why:

- **`-e NAME=value`** puts the secret in the command line, where `ps` and your
  shell history both pick it up.
- **`--env-file`** keeps it out of `ps`, but only by writing the secret to disk
  in plaintext — which is the thing this whole section exists to avoid, and the
  file invariably outlives the debugging session that created it.

### The refusal marker will bite you on the second local run

`sign-in.ts` records a rejected credential at
`/tmp/mit-learn-canary-credential-rejected` and refuses to submit it again,
precisely so a bad credential cannot burn ten attempts. In Concourse that file
dies with the container. **On your workstation `/tmp` persists**, so once you
have hit a rejection every later run fails with "Refusing to re-submit a
credential Keycloak already rejected in this run" — including after you fix the
password.

```bash
rm -f /tmp/mit-learn-canary-credential-rejected
```

That is the correct fix, and it is also the moment to be sure the credential is
actually right, because the guard is no longer protecting you.

## Step 5 — Reading a trace

The trace is almost always the fastest route to a diagnosis.

```bash
# A local failure.
npx playwright show-trace canary-results/artifacts/<test-dir>/trace.zip

# The whole local report.
npx playwright show-report canary-results/html
```

For a **pipeline** failure, pull the artifacts Concourse published:

```bash
aws s3 ls --recursive s3://ol-eng-artifacts/canary-results/canary-mit-learn/
aws s3 cp --recursive \
  s3://ol-eng-artifacts/canary-results/canary-mit-learn/<job>/<YYYYMMDDTHHMMSSZ>/ \
  /tmp/canary-failure/
npx playwright show-trace /tmp/canary-failure/artifacts/<test-dir>/trace.zip
```

There is no build number in that path — task containers on our Concourse get no
`BUILD_*` variables — so **match a directory to a build by the build's start
time**. Or drop `trace.zip` into <https://trace.playwright.dev/>, which needs no
local install.

Green runs publish no trace, but every run publishes its small `results.json` to
`s3://ol-eng-artifacts/canary-runs/canary-<property>/<job>/<TS>.json`. That is
where to look for whether a green build had to retry a journey (`stats.flaky`).

## Step 6 — Reproducing a pipeline failure exactly

When a journey passes locally and fails in Concourse, run the same image. Derive
the tag from `package.json` — never hardcode it, and never let the runner and the
image drift:

```bash
TAG=v$(python3 -c "import json;print(json.load(open('package.json'))['devDependencies']['@playwright/test'])")-noble

rm -rf /tmp/canary-run && mkdir -p /tmp/canary-run
rsync -a --exclude node_modules --exclude canary-results . /tmp/canary-run/
docker run --rm -v /tmp/canary-run:/work -w /work \
  -e CANARY_BASE_URL=https://rc.learn.mit.edu \
  -e CANARY_USER_EMAIL -e CANARY_USER_PASSWORD \
  "mcr.microsoft.com/playwright:$TAG" \
  bash -c "npm ci && npx playwright test specs/mit-learn --project=chromium"
```

Copy the tree rather than mounting it in place, so the container's `npm ci`
cannot overwrite your host `node_modules`.

**`specs/mit-learn` includes the signed-in journey, so the credential variables
have to be exported first** (see "If you genuinely need the pipeline credential"
above) — Docker does not inherit them on its own. Without them this is not a
reproduction of anything: `sign-in.ts` throws before the browser opens, and you
get two credential failures instead of the pipeline failure you came to chase.
Measured against the current spec set: 4 passed, 2 failed on
`login-and-search.spec.ts`.

If you only need an anonymous journey, name it instead of the directory and drop
the two credential flags:

```bash
  bash -c "npm ci && npx playwright test specs/mit-learn/homepage.spec.ts --project=chromium"
```

### About `--disable-dev-shm-usage`

`playwright.config.ts` sets it **unconditionally on the chromium project**, not
only under CI, so your local Chromium run matches the pipeline and a local pass
does not diverge from it. Leave it alone.

It exists because a Concourse task container gets the default 64MB `/dev/shm`,
where Chromium puts renderer shared memory; without the flag a journey dies
mid-run with a bare tab crash. It is **Chromium-only** — Firefox and WebKit
reject the flag, which is why it sits on that one project rather than in `use`.

### Running WebKit

Do not try to install WebKit's system libraries on a workstation
(`playwright install-deps` wants root and assumes apt). Run the image instead,
adding `--project=webkit` to the `docker run` above. WebKit is roughly twice
Chromium's wall clock, and that difference is hydration — which is where the
races are, and why shipping a journey means running it once in WebKit.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Executable doesn't exist at …/chromium_headless_shell-…` | `npx playwright install chromium` not run (Step 1), or runner/image versions have drifted. |
| `BEWARE: your OS is not officially supported by Playwright; downloading fallback build for ubuntu24.04-x64` | Benign, on any non-Ubuntu distro. It fetches the Ubuntu build and works. If a journey then fails in a way the pipeline does not, reproduce in the image (Step 6) before believing it. |
| `CANARY_BASE_URL is required` | Step 2. There is deliberately no default. |
| `CANARY_USER_EMAIL and CANARY_USER_PASSWORD are required` | Signed-in journey without credentials. Run the anonymous ones, or Step 4. |
| `Refusing to re-submit a credential Keycloak already rejected` | Stale `/tmp/mit-learn-canary-credential-rejected`. See Step 4. |
| Reached `…/protocol/openid-connect/registrations` or Touchstone | The account does not exist in the realm. Not a captcha and not an SSO problem. |
| Login landed on `/onboarding` | Account has never completed onboarding. Log in by hand once. |
| `item … with '.only' is not allowed due to the 'forbidOnly' option` | A stray `.only`. Filter by path or `-g` instead. |
| Passes locally, fails in Concourse | Reproduce in the image (Step 6). Suspect a hydration race; try WebKit. |

## Checklist

- [ ] `npm ci` **and** `npx playwright install chromium`
- [ ] `CANARY_BASE_URL` exported
- [ ] Anonymous journeys pass before touching credentials
- [ ] Used your own dev account, not `canary_mit_learn`
- [ ] Password supplied via `read -rs` or `sops -d --extract`, not history or a file
- [ ] `/tmp/mit-learn-canary-credential-rejected` cleared if a rejection was hit
- [ ] `unset CANARY_USER_PASSWORD` afterwards
