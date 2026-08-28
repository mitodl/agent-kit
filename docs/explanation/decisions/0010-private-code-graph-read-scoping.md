<!--
  MIRRORED FILE — DO NOT EDIT HERE.
  Edit mcp/servers/witan/docs/adr/0010-private-code-graph-read-scoping.md instead; `just docs-gen` copies it into the site.
-->

!!! info "This page lives with the code"

    The authoritative copy is
    [`mcp/servers/witan/docs/adr/0010-private-code-graph-read-scoping.md`](https://github.com/mitodl/agent-kit/blob/main/mcp/servers/witan/docs/adr/0010-private-code-graph-read-scoping.md).

# 10. Code-graph read scoping: refuse private repos until an actor↔GitHub binding exists

- Status: Accepted (D1 implemented; D2–D5 decided, not implemented)
- Date: 2026-08-28
- Deciders: witan platform owners
- Tracking: task `tk-per-repo-read-scoping-on-code-graphs-via-github--371b4d`, project `wp-witan-multi-user-service-deployment-dcf6ee`
- Supersedes: —
- Related: `docs/adr/0002-witan-cedar-authorization-bundle.md` (the Cedar bundle
  this would feed); `docs/adr/0004-keycloak-jwt-per-user-actor-mapping.md`
  (where a request's identity comes from); `witan_code/github_app.py` (the App
  and its installation)

## Context

A `code-<repo>` graph holds a repo's file paths, symbol names, and call
structure, and every witan user can read every one of them. While every indexed
repo is public that costs nothing, which is why it has never been fixed.

★ The mechanism is not the one the tracking task recorded, and the difference
matters to the design. The task said "`cluster.yaml` declares no `policy:`
block". It does: `policy/code-graph.policy.yaml` is a real Cedar bundle,
templated by ol-infrastructure over the graph ids of every managed repo. What
makes reads unscoped is *what the bundle says*, not its absence —

- `applies_to` is the full list of per-repo graph ids, so **one bundle governs
  all of them** and there is no per-repo distinction to make;
- its `users-read-any-branch` rule grants `read`/`export` to `witan-users` on
  any branch;
- `witan-users` is rendered at boot from the live actor-token map
  (`policy/render_groups.py`), i.e. **every actor holding a bearer token**.

That is a better starting point than "no policy at all", because it means
per-repo read scoping is expressible in the plane that already exists rather
than needing a new one. See D3.

The originating idea (2026-08-03) was to close it with the same GitHub App the
CI indexer clones as: ask GitHub whether the calling actor can see repo R, and
make that answer the read authorization for `code-R`. Attractive because it
makes GitHub the single source of truth for who may see what — the same place
the clone credential is scoped — instead of a second access list in Cedar that
would drift from it.

### What was checked, 2026-08-28

**The exposure is not live, and the escalation that said it was read the wrong
graph.** This task was raised p2 → p1 on 2026-08-24 on the finding that three
private repos (`BoundlessNotions/{podgraph,card_advantage,podagent}`) appear in
`code_indexed_repos`. They are private — but they are indexed in the
maintainer's **local** stores under `~/.local/share/witan/code/`, which is what
`code_indexed_repos` lists over local stdio. The deployed graphs are exactly
`omnigraph:managed_repos`, and CI, QA and Production all carry the same 14
mitodl repos, every one of them public (`gh api repos/<r> --jq .private` →
`false`, all 14). `witan code deps` against production returns those same 14.
`git log -S BoundlessNotions` over ol-infrastructure's `src/` is empty: no
private repo has ever been in the list.

This is the same shape as the `witan serve` defect
(`les-witan-serve-silently-used-the-local-store-when-t-b24602`): a local tool
result read as a statement about the deployment.

**The App's installation is not a narrowing control.** `GET
/orgs/mitodl/installations` reports the `witan-agent-graph` App with
`repository_selection: "all"` and `permissions: {contents: read, metadata:
read}`. Every mitodl repo, private ones included, is one `managed_repos` entry
away from a shared graph. The premise "two independently-controlled things have
to agree first" (`github_app.py`) is true of the App's *existence*, not of its
scope as installed.

**The idea's identity half does not work as described, and this is the finding
that reshapes the design.** Asking GitHub "can actor A read repo R" needs A as
a GitHub login. witan actors are `act-<Keycloak sub>` (ADR-0004 D2), and
`preferred_username` in this realm is an MIT email address. Nothing maps one to
the other:

- mitodl is on the GitHub **Team** plan. `organization.samlIdentityProvider` is
  `null`, so there are no `externalIdentities` and no SCIM API — both are
  Enterprise Cloud features. The authoritative email↔login mapping other orgs
  would use does not exist here and cannot be turned on without a plan change.
- The `ol-platform-engineering` Keycloak realm has no GitHub identity provider,
  so no token carries a GitHub login today.
- Email-to-login search (`GET /search/users?q=<email>`) only finds users who
  made an email public, and is a guess, not an authorization input.

So the mapping cannot be *looked up*. It has to be *created*, by the user
proving their GitHub identity once, before any of the repo-access API calls in
the original idea can be made at all. That is the hard part, exactly as the
task's own design notes suspected — and it is a prerequisite, not a detail.

## Decision

### D1 — The write path refuses private repos, and that is what ships now

`docker/witan-ci-index.sh` asks `python -m witan_code.github_app --visibility
<repo>` before each clone and refuses to index a private repo into a shared
graph, counting the refusal as a failure so it lands in the job summary and in
Loki. `WITAN_CODE_CI_ALLOW_PRIVATE_REPOS=1` waives it.

The guard sits on the **write** path rather than on `managed_repos` review
because this is where a repo actually becomes readable by everyone, whatever
put it in the list. It is only meaningful on the App path: cloning anonymously,
a private repo fails at `git clone` anyway.

An unanswerable question (GitHub 404s, or is unreachable) refuses too. A 404 is
genuinely ambiguous between "private and out of installation scope" and "does
not exist", and the failure mode of guessing is the one the guard exists to
prevent.

The override exists so that accepting the exposure has to be written into the
deployment and reviewed, rather than being the default. It is not the way to
lift this: D2–D5 are.

This makes the task a **hard blocker on indexing the first private repo**,
which is what the task itself proposed, enforced rather than remembered.

### D2 — The actor↔GitHub-login binding comes from Keycloak, via a GitHub identity provider

Add GitHub as an identity provider on the `ol-platform-engineering` realm and
have users link their GitHub account to their existing Keycloak account. A
protocol mapper then puts the linked GitHub login into the token, and witan
reads it exactly the way it already reads `sub` — off a validated JWT, per
request, with no second store to drift.

Rejected: a witan-side `witan link-github` device flow writing an
`act-<sub> → login` record into the graph. It has the same trust chain (GitHub
OAuth asserting the login) and needs no Keycloak change, but it puts an
identity table in witan that ADR-0004 deliberately kept out of it, and
unlinking in GitHub would not propagate. Keep it as the fallback if the realm
change is blocked; do not build both.

A user who has not linked has no GitHub identity, and therefore reads no
private repo's graph. That is the correct default and needs no special case.

### D3 — Enforce in the Cedar bundle at render time, not per request in the MCP tier

A private repo gets **its own bundle**, whose reader group is not "every actor
with a token" but the actors GitHub says can read that repo.
`policy/render_groups.py` already computes group membership at boot from the
live actor-token map, and `ol-infrastructure` already templates `applies_to`
per graph; this is the same two mechanisms with a narrower input, not a new
plane.

This is the decision the corrected Context above makes available, and it is
strictly better than the per-request check this ADR would otherwise have
specified:

- **Nothing on the read path.** No GitHub round trip per `code_*` call, so no
  TTL cache, no added latency, and no new failure mode in a read.
- **It binds at the data tier**, so it also covers readers that do not go
  through the MCP tier at all — the in-cluster CI indexer reaches
  omnigraph-server directly.
- **Revocation latency is already-existing behaviour**, not a new parameter:
  the `actor-tokens` VaultStaticSecret declares
  `rolloutRestartTargets: [omnigraph-server]`, so the server re-renders its
  groups on exactly the event that changes the actor set, hourly at worst.

Rejected: resolving `actor can read repo R` per request in the MCP tier and
feeding it to Cedar as an entity attribute. It is the obvious design when you
believe there is no policy bundle, and it buys prompt revocation — at the cost
of a cache whose TTL *is* the revocation latency anyway, a GitHub dependency in
the read path, and an enforcement point that the direct-store reader bypasses.

### D4 — The membership question is asked as the user, not as the App

To render that group, ask GitHub, once per user per repo per render, whether
the user can read the repo. With a linked account (D2), Keycloak holds the
user's GitHub token and can hand it back over its broker endpoint; ask
`GET /repos/{owner}/{repo}` with it — 200 means this user can read this repo,
404 means they cannot.

Rejected: `GET /repos/{o}/{r}/collaborators/{u}` as the App. It asks the
question indirectly (collaborator status is not readability — org-level and
team-level grants are not collaborations), it is scoped to the App's
installation so it cannot answer for other orgs, and the fine-grained
permission it requires is above `metadata: read` and was not established.

★ Unverified before implementing: that the realm's broker `read-token` role can
be granted to the witan client, and what GitHub OAuth token lifetime and
revocation behaviour that implies. If the brokered token is unavailable, D4
falls back to the App-side collaborator check, and the permission bump it needs
becomes its own review.

### D5 — Absent means denied, and a render that could not ask must say so

A user who has not linked a GitHub account, or whom GitHub does not confirm, is
not in the group and reads nothing. `render_groups.py` already drops a group
with no members along with the rules referencing it — which denies rather than
granting, and is the behaviour wanted here.

The failure that needs care is a render that could not reach GitHub. It must
not silently produce a narrower group that reads as a revocation, nor reuse a
stale one that reads as a grant: it should fail the render loudly, leaving the
previous applied bundle in place, and log which repos it could not resolve.
Group sizes are already logged per render, so the signal has somewhere to go.

## Consequences

- The exposure this ADR is about cannot arise silently any more: adding a
  private repo to `managed_repos` produces a loud refusal naming the tracking
  task, not a shared graph nobody knew was readable.
- Nothing changes for the 14 public repos indexed today. The guard costs one
  `GET /repos/{o}/{r}` per repo per sweep (14 per 4 hours).
- D2 puts a prerequisite on ol-infrastructure (the realm's GitHub identity
  provider) and a one-time action on every user who needs private-repo reads.
  That cost is real and is the reason this is not built speculatively — build
  it when a private repo actually needs indexing, and let the D1 refusal be
  what makes that moment visible.
- The 2026-08-24 escalation stands corrected in the tracking task. The lesson
  worth carrying: `code_indexed_repos` from a local stdio server describes the
  laptop, and a claim about the deployment needs `managed_repos` or a query
  that provably went to the cluster.
- Two premises this ADR inherited from the tracking task were wrong, and both
  were wrong in the direction of making the problem look simpler than it is.
  "No `policy:` block" hid an existing enforcement plane that turns out to be
  the right place to enforce; "the App can answer the read question" hid a
  missing identity mapping that has to be built first. Neither survived being
  checked, and checking cost minutes.
