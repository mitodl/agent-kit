---
name: add-canary-journey
description: >
  Add a Playwright canary journey to an existing web property, or onboard a
  whole new property, in src/ol_concourse/pipelines/canaries. Use when asked to
  add or extend an end-to-end canary test, cover a new user journey, add a new
  target property or environment to the canary fleet, or register a canary
  pipeline. Covers the specs/<property>/ drop-in, the CanaryParams + canary_names
  two-list-edit onboarding, content fixtures, and the guardrails around the
  package.json-derived image tag.
license: BSD-3-Clause
metadata:
  category: process
---

# Adding a Canary Journey or Property

**Repository:** [`mitodl/ol-infrastructure`](https://github.com/mitodl/ol-infrastructure).
Every unqualified path below is relative to that checkout.

## When to use

Someone wants a new end-to-end user journey watched continuously, or a new web
property brought into the canary fleet.

To *run* what you write, use the `run-canary-locally` skill. This skill is about
what to write and which files to touch.

**Read these rather than working from memory** — they are authoritative and this
skill deliberately does not duplicate them:

- [`canaries/AGENTS.md`](https://github.com/mitodl/ol-infrastructure/blob/main/src/ol_concourse/pipelines/canaries/AGENTS.md)
  — the non-negotiables and the locator/assertion rules.
- [`canaries/README.md`](https://github.com/mitodl/ol-infrastructure/blob/main/src/ol_concourse/pipelines/canaries/README.md)
  — what a canary is, what does not belong, and the deployment runbook.
- [`specs/<property>/README.md`](https://github.com/mitodl/ol-infrastructure/blob/main/src/ol_concourse/pipelines/canaries/specs/mit-learn/README.md)
  — that property's ranked journey table and content dependencies.
- [ADR 0011](https://github.com/mitodl/ol-infrastructure/blob/main/docs/adr/0011-playwright-canary-specs-in-ol-infrastructure.md)
  — why the specs live in this repo.

## First: is this a canary at all?

A canary is **not** a PR test. The two have opposite failure economics: a PR test
should be strict, while a canary must fail *only* when something is genuinely
wrong for users, because its only output is a red Concourse build that a human
is expected to trust.

| | |
|---|---|
| Belongs here | A journey a real user takes, that we want to know about within minutes of it breaking in a deployed environment. |
| Does not belong here | Anything gating a pull request. That lives in the application's own repo against its own dev stack. |

**Assert on the journey completing, not on the content it finds along the way.**
A canary asserting on CMS copy or a course price goes red every time an editor
changes a word, and reds that mean nothing are how a canary trains its audience
to ignore it.

## Which flow are you in?

The two cost very different amounts. Check before you start.

| | Flow A: new journey, existing property | Flow B: new property |
|---|---|---|
| Files | `specs/<property>/<journey>.spec.ts` | that, plus `pipeline.py` **and** `meta.py` |
| Pipeline edit | **None** | Two list edits |
| Needs a credential decision | Only if signed-in | Usually yes |
| Deploy | `canary-meta` picks it up from `main` | same |

---

## Flow A — a journey on a property that already has a canary

### 1. Check the journey is worth having before writing it

The property's `README.md` carries a **ranked journey table derived from
production traces**. Add against that ranking, not against intuition about what
users probably do. On `mit-learn` the measured #1 route (39,086 non-bot
renders/day) was one nobody had thought to cover, while the route worth paging on
most serves only 121/day — it earns its place on blast radius, since every
authenticated journey sits behind it.

Also check the **"Deferred, with reasons"** section. A journey may already have
been ranked and consciously not implemented, usually blocked on provisioning
rather than on spec-writing.

### 2. Write the spec

```
specs/<property>/<journey>.spec.ts
```

Reuse the property's `helpers/`. Login flows in particular are shared — do not
re-derive a Keycloak flow per spec. On `mit-learn`, a signed-in journey imports
`test` from `./helpers/signed-in-test` rather than from `@playwright/test`.

Locator and assertion rules are in `AGENTS.md` and worth reading in full. The
ones most often gotten wrong:

- **Role- and label-based locators**, not CSS paths. They break when the
  user-visible affordance breaks, which is the signal a canary is for.
- **Web-first assertions only.** `expect(locator)` auto-retries;
  `expect(await locator.count())` does not, and is the top source of flake.
- **Never `waitForTimeout`.** Wait for the thing you actually need.
- **Scope a listing assertion to the listing.** An unscoped
  `getByRole("article")` often matches a curated strip and sails straight
  through an empty search index — one of the failures a canary exists to catch.
- **Do not key assertions on the environment.** No
  `{ [RC]: …, [LOCAL]: … }` expectation tables; assert what is true everywhere.
- **Retry the action, not just the assertion, when interacting before
  hydration.** Wrap the action and its outcome in
  `expect(async () => { … }).toPass()`.

### 3. If the journey touches live content

Put the reference in `specs/<property>/helpers/fixtures.ts` **and** the
property README's content-dependency table — both places or neither. Two
journeys needing the same content share one constant, so re-checking a
dependency is one edit.

But first: **derive the fixture rather than pinning it.** Ask whether the page
will hand it to you. `mit-learn`'s drawer journey needs a resource id, so it
clicks whichever card the index returned first and reads the name off that card
— it has no resource of its own to go stale. A pinned id that is later
unpublished or reindexed produces a red build about the *catalogue*, not about
the property.

Where a reference is unavoidable, prefer the **structural** thing over the
curated one, and write the argument down. On `mit-learn` a *unit* channel is an
offeror and exists as long as that offeror publishes anything (42,342 indexed
resources), while a *topic* channel is curated and measurably thin (4). Both
exercise the same route and API fan-out, so choosing the durable one costs
nothing.

### 4. Verify before shipping

```bash
cd src/ol_concourse/pipelines/canaries
npm run typecheck
CANARY_BASE_URL=https://rc.learn.mit.edu npx playwright test specs/<property>
```

- **Run it at least twice in a row.** A canary that passes once is not yet a
  canary. `--repeat-each=3` is the cheap version.
- **Run it once in WebKit.** This is not cross-browser coverage — the pipeline
  schedules Chromium alone — it is the cheapest way to catch a hydration race,
  because WebKit loses the races Chromium wins by being faster. A real race in
  `login-and-search.spec.ts` was caught only this way; Chromium passed it every
  time, which is what that class of bug looks like right up until the target has
  a bad day. See `run-canary-locally` for how (run the image; do not install
  WebKit's system libraries locally).

### 5. There is no pipeline edit

`CanaryParams.spec_paths` defaults to the property's whole `specs/<property>/`
directory, so a new spec is picked up with no change to `pipeline.py` or
`meta.py`. Narrow `spec_paths` only to deliberately *exclude* a journey from the
schedule.

---

## Flow B — onboarding a new property

Exactly **two list edits**, the same onboarding shape as
[`simple_pulumi`](https://github.com/mitodl/ol-infrastructure/blob/main/src/ol_concourse/pipelines/infrastructure/simple_pulumi/).
Confirmed against the code, not from memory: there is no third edit. In
particular **nothing needs adding to the Grafana alerting module** — canaries
emit no metrics and raise no alerts, because Concourse build status is the sole
result signal.

### 1. Create the spec directory

```
specs/<property>/README.md          # environment targeted, who owns the journeys
specs/<property>/<journey>.spec.ts
```

The README should carry the ranked journey table, the content-dependency table,
and the account notes. Follow `specs/mit-learn/README.md` as the model.

Then write the journeys exactly as in Flow A.

### 2. Edit 1 — a `CanaryParams` entry in `pipeline.py`

```python
pipeline_params: dict[str, CanaryParams] = {
    "mit-learn": CanaryParams(...),
    "<property>": CanaryParams(
        canary_name="<property>",
        base_url="https://<target>",
        # The Concourse credential's NAME, not a credential. Omit entirely for a
        # property whose journeys are all anonymous. The two trailing pragmas are
        # what `pipeline.py` already carries: a name that looks like a secret to
        # ruff's S106 and to detect-secrets, and is not one.
        credential_secret="canary_<property>",  # noqa: S106  # pragma: allowlist secret
    ),
}
```

Read the `CanaryParams` docstring for every field — it explains the default and
why it is the default. `browsers` defaults to Chromium alone on purpose: each
extra browser multiplies the load the canary puts on a live property.

**Never put a credential value here. This file is public source.**

### 3. Edit 2 — the name in `canary_names` in `meta.py`

```python
canary_names = [
    "mit-learn",
    "<property>",
]
```

`canary_names` is the source of truth for what is actually **deployed**;
`pipeline_params` is deliberately the wider of the two, so a canary can be added
and reviewed before it starts running against a live property. Adding the name
here is what puts it live.

### 4. If the journeys need a login

- Add the credential under `pipelines:` in
  `src/bridge/secrets/concourse/operations.production.yaml` as
  `infrastructure/canary_<property>` with `email` and `password` keys, applied by
  the `concourse` Pulumi project. Use `sops_secret`, never a text editor.
- Set `credential_secret` to `canary_<property>` — the *name*, which Concourse's
  Vault credential manager resolves at task start so the value never lands in
  the rendered `definition.json`.
- Use a plus-address on a domain MIT controls, so a password-reset mail can
  never be received by anyone else.
- **Read the lockout section of `specs/mit-learn/README.md` before provisioning
  an account.** Realm `olapps` is `failureFactor=10`, `permanentLockout=true`
  with a 12-hour counter reset, so a credential that has drifted between
  Keycloak and Vault permanently disables the account in about two hours at a
  10-minute cadence. Rotation must change both together.
- Log the new account in **by hand once** to consume first-login onboarding,
  or every run lands on `/onboarding` instead of the dashboard.

### 5. Validate and deploy

```bash
cd src/ol_concourse/pipelines/canaries
uv run python pipeline.py <property>   # renders definition.json; prints the fly command
uv run python meta.py                  # the fleet still renders
npm run typecheck
```

From the repo root, per the standard checklist:

```bash
uv run ruff format src/ && uv run ruff check src/ && uv run mypy src/
```

`canary-meta` re-sets itself and every managed pipeline from `main`, so merging
is the deploy. Only a brand-new `canary-meta` needs the one-time manual set in
the README.

---

## Guardrails that have already cost someone a debugging session

- **The image tag is derived from `package.json`; never hardcode it.** The
  browsers are baked into the image at a revision keyed to that exact Playwright
  version, so a drifted runner fails with a message naming *neither* version:
  `browserType.launch: Executable doesn't exist at /ms-playwright/chromium_headless_shell-…`.
  Keeping the pin as the single source of truth is most of why the specs live in
  this repo, and it makes a Renovate bump self-contained.
- **A range pin is refused, loudly.** `playwright_image_tag()` raises on
  anything but an exact `X.Y.Z` — verified: `^1.63.0`, `~1.63.0`, `1.63` and
  `latest` are all rejected, as is a missing pin. Do not "fix" that by relaxing
  the regex; a range is exactly how the runner drifts from the image.
- **Do not add npm dependencies.** The direct set is `@playwright/test`,
  `@types/node` and `typescript`, which keeps `npm ci` a six-package,
  sub-second install paid on every run of every canary forever.
- **Task containers get no `BUILD_*` metadata** on our Concourse — only
  `ATC_EXTERNAL_URL`, verified with an `env` probe. A script expanding
  `$BUILD_PIPELINE_NAME` dies on `set -u` before a single test runs, which is
  why the pipeline renders the pipeline and job names in as literals and stamps
  a UTC timestamp. Match an artifact directory to a build by the build's start
  time.
- **`.only` fails the whole run**, unconditionally and on purpose:
  `item … with '.only' is not allowed due to the 'forbidOnly' option`. A stray
  one would silently stop every other journey for that property from being
  checked.
- **Do not raise `retries` to quiet a flaky journey.** Fix the locator. It is 1;
  above that, real intermittent user-facing breakage becomes silence.
- **No credentials, and no target URL, in source.** This repository is public.
  Read both from the environment and fail loudly when unset.
- **Do not add a result or notification channel.** No metric push, no Grafana
  alert, no Slack, no Rootly. Green or red in Concourse is the whole contract,
  and Grafana Synthetic Monitoring separately answers the different question of
  whether the endpoint responds at all.
- **Keep the failure-artifact step's ordering.** The collection runs after a
  failed test run deliberately, since a non-zero exit under `set -e` would skip
  collecting exactly the artifacts worth having. `results.json` is additionally
  published on *every* run so the flake rate stays measurable; the full
  trace/video tree stays failure-only.

## Checklist

- [ ] It is a canary, not a PR test
- [ ] Journey checked against the property README's ranked table (and its deferred list)
- [ ] Role/label locators; web-first assertions; no `waitForTimeout`; no `.only`
- [ ] Content references derived where possible; otherwise in `fixtures.ts` **and** the README table
- [ ] `npm run typecheck` clean
- [ ] Ran against the real target **twice**, and once in WebKit
- [ ] Flow B only: `CanaryParams` in `pipeline.py` **and** name in `canary_names` in `meta.py`
- [ ] Flow B only: credential added via `sops_secret`; account logged in by hand once
- [ ] Flow B only: `pipeline.py <property>` and `meta.py` both render
- [ ] No new npm dependency, no hardcoded image tag, no alerting added
