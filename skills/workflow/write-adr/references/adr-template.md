# ADR template

The shape [`write-adr`](../SKILL.md) writes, in the ol-infrastructure flavor.
For the witan flavor, see [`repo-flavors.md`](repo-flavors.md).

**Copy the newest ADRs, not `template.md`.** ol-infrastructure's 13 vary widely
— `Options Considered` is an H2 in five, an H3 in five, absent in three; the
Review History footer is missing from three; 0012 uses `## Decisions` plural.
The newest and best-written (0012 and 0013) are the *least* conformant to the
checked-in template: they dropped `## Implementation Notes`, `## Notes`, effort
estimates and risk levels, and reference code by path and symbol instead of
pasting it. **ADR 0013 is the shape to copy**, with the two additions this
skill makes.

## Structure

| Section | Required | Notes |
|---|---|---|
| YAML frontmatter, `applies_to` | yes | The globs this decision governs |
| `# NNNN. Title` | yes | Number duplicated from the filename |
| `**Status:**` `**Date:**` `**Deciders:**` `**Technical Story:**` | yes | Four bolded lines, no blank lines between |
| `## Context` | yes | Prose. Add H3s only when the decision needs them |
| `### Options considered` | when there was a choice | H3 under Context, as 0013 does. Mark the chosen one |
| `## Decision` | yes | Plainly. Name files and symbols; name enforcing tests here |
| `## Consequences` | yes | `### Positive` / `### Negative` / `### Neutral` |
| `## Verify` | only when nothing else can enforce it | See the SKILL's enforcement order |
| `## Related Decisions` | when there are any | Relative links to other ADRs |
| `## References` | when there are any | Incidents, PRs, external material |
| Review History + `**Last Updated:**` | yes | The approval trail |

That is ADR 0013's exact shape. **Both forms are in live use** and either is
acceptable — the older cohort promotes options to `## Options Considered` at H2
and spells out `### Positive Consequences`, and breaks Context into
`### Current Situation`, `### Problem Statement`, `### Business/Technical
Drivers`, `### Constraints`, `### Assumptions`. Use the long form when the
decision genuinely has that much context to carry (ADR-0009 does); use 0013's
short form otherwise, and match whichever the directory's newest files use.

Omit a section with nothing in it. Absent options say there was one real
mechanism; an empty section says nobody looked.

## Writing rules

- **Past and present tense, never future.** "APISIX leaves bearer tokens to the
  applications", not "we will configure APISIX to". The record describes a
  decision, not a plan.
- **Be honest about the negative consequences.** An ADR with only positive ones
  is marketing. The accepted downside is the most valuable line in the file.
- **Name the condition that would flip each rejected option.** "Rejected
  because slower" is a verdict. "Would have won if the applications could not
  validate for themselves" is reviewable, and is what someone re-opening the
  question six months later actually needs.
- **Reference code by path and symbol**, as the newest ADRs do, rather than
  pasting snippets that go stale.
- Hard-wrapping prose is fine here — this is a repo file, not a GitHub comment.

## Template

````markdown
---
applies_to:
  - "<glob>/**"
---

# NNNN. Title Case Title

**Status:** Proposed
**Date:** YYYY-MM-DD
**Deciders:** <a human's name or handle>
**Technical Story:** <issue URL, incident ID, and/or witan task/project slugs>

## Context

<How the system behaves today in the area being decided, with the measured
baseline from scoping: where the behavior lives, what calls it, counts. What is
wrong or missing. What was tried, if anything, and what happened. Facts, not
plans. Break into H3s only when there is enough to need them.>

### Options considered

1. **<Mechanism> (CHOSEN)**
   - Pros: <concrete>
   - Cons: <concrete>
2. **<Mechanism>**
   - Pros: <concrete>
   - Cons: <concrete>. Would have won if <the condition that flips it>.

## Decision

<The chosen mechanism, stated plainly and without hedging. Name the files and
symbols it touches. If a test enforces it, name the test here.>

## Consequences

### Positive

- <What gets easier.>

### Negative

- <What gets harder. Required — there is always one.>

### Neutral

- <Follow-on work, or a condition under which this is revisited.>

## Related Decisions

- `NNNN-other-decision.md`

## References

- <Incident, PR, external material.>

---

**Review History:**

| Date | Reviewer | Decision | Notes |
|------|----------|----------|-------|
| YYYY-MM-DD | _Pending_ | _Pending_ | Created by AI agent, pending human approval |

**Last Updated:** YYYY-MM-DD
````

## Worked example

Modelled on ol-infrastructure ADR-0013. Its `applies_to` globs and its Verify
command were both checked against the real tree before being written here — the
first drafts of both were wrong, which is why the SKILL requires the check.

````markdown
---
applies_to:
  - "local-dev/apps/*/apisix-routes.yaml"
  - "src/bridge/lib/version_pins/APISIX_CHART_VERSION"
---

# 0014. APISIX Leaves Bearer Tokens to MITx Online and MIT Learn

**Status:** Proposed
**Date:** 2026-10-14
**Deciders:** tmacey
**Technical Story:** Rootly INC-10, ol-infrastructure#4810 (reverted),
`tk-apisix-eliminate-oidc-state-mismatch-failures-78-e1dfb4`

## Context

APISIX fronts both MITx Online and MIT Learn. Its OIDC plugin can validate
bearer tokens itself, and four plugin options turn that on. All four are off —
by accident rather than by decision, so each incident review re-opens the
question.

INC-10 was caused by APISIX and the application disagreeing about who owned
token validation. #4810 enabled gateway-side validation and was reverted the
same day: the gateway rejected tokens the applications accept, because the
applications allow a grace window the gateway does not. Both applications
already validate bearer tokens and will continue to, so gateway-side validation
would have to agree with both of their rules exactly — grace window included —
or it rejects valid traffic.

### Options considered

1. **Leave all four plugin options off (CHOSEN)**
   - Pros: one authority per token; matches current behavior; no new failure mode.
   - Cons: the gateway cannot reject a bad token early, so invalid requests
     reach the application.
2. **Enable gateway-side validation and align the grace window**
   - Pros: invalid tokens die at the edge.
   - Cons: two places to keep in sync forever, and INC-10 is what that costs
     when they drift. Would have won if either application were unable to
     validate for itself.

## Decision

APISIX does not validate bearer tokens. The four OIDC plugin options
(`bearer_only`, `introspection_endpoint`, `set_access_token_header`,
`access_token_in_authorization_header`) stay off, and the applications remain
the single authority.

## Consequences

### Positive

- One authority per token. A grace-window change in either application needs no
  corresponding gateway change.

### Negative

- Requests carrying an invalid bearer token reach the application before being
  rejected, costing one upstream round trip.

### Neutral

- If a third application is fronted that cannot validate for itself, this is
  revisited rather than worked around.

## Verify

```bash
# Asserts the violating value, not the keyword: `bearer_only: false` is
# compliance, and `introspection_endpoint_auth_method` is a different setting.
# Nothing runs this automatically.
grep -rnE 'bearer_only:[[:space:]]*true' local-dev/apps/*/apisix-routes.yaml \
  && echo "FAIL: gateway-side bearer validation enabled" \
  || echo "OK"
```

## Related Decisions

- `0009-deploy-witan-as-shared-multi-tenant-mcp-service.md`

## References

- Rootly INC-10

---

**Review History:**

| Date | Reviewer | Decision | Notes |
|------|----------|----------|-------|
| 2026-10-14 | _Pending_ | _Pending_ | Created by AI agent, pending human approval |

**Last Updated:** 2026-10-14
````

Note what the example does that the template cannot enforce: the rejected
option carries the condition that would flip it, the negative consequence is a
real cost rather than a hedge, and the Verify command's comment explains why
the obvious version of the check is wrong.
