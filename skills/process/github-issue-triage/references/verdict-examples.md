# Verdict Examples

Annotated examples from an audit of mitodl/ol-infrastructure (June 2026, 59 issues).

---

## LIKELY_OUTDATED examples

### #4504 — Mailgun under Pulumi management
**Verdict: LIKELY_OUTDATED — Close**

Evidence found:
```bash
ls src/ol_infrastructure/applications/mailgun/
# __main__.py  Pulumi.yaml  Pulumi.applications.mailgun.applications.CI.yaml ...

git log --oneline --grep="mailgun" | head -3
# ec5abd05c Mailgun management under Pulumi (#4505)
# 852f1492f Imported CI/QA mailgun domains
```

The Pulumi mailgun application stack exists with all environment configs.
Two commits confirm a full import landed in April 2026.

---

### #3119 — Keycloak metrics (replace aerogear SPI)
**Verdict: LIKELY_OUTDATED — Close**

Evidence found:
```bash
rg "aerogear|keycloak-metrics-spi" src/ -l
# (no output)

grep -n "metrics-enabled" src/ol_infrastructure/applications/keycloak/__main__.py
# 507:  {"name": "metrics-enabled", "value": "true"}

git log --oneline --grep="keycloak.*metric" -i | head -3
# ee7819e6c Fix Keycloak metrics (#4358)
# 2594f1bdc fix(keycloak): migrate to v2beta1 API and remove duplicate ServiceMonitor
```

The aerogear SPI is gone; native Keycloak metrics are enabled with a
ServiceMonitor wired to Grafana Alloy. The issue's specific ask is done.

---

### #2030 — Prevent pre-release pre-commit upgrades
**Verdict: LIKELY_OUTDATED — Close**

Evidence found:
```bash
grep "prettier" .pre-commit-config.yaml
# (no output)

grep "ruff" .pre-commit-config.yaml
# - id: ruff-format
# - id: ruff-check
```

Prettier was removed from `.pre-commit-config.yaml` and replaced with ruff.
The specific problem (Renovate bumping prettier to alpha versions) cannot recur
for a tool no longer present.

---

## POSSIBLY_OUTDATED examples

### #3984 — Epic: Sentry Noise Reduction
**Verdict: POSSIBLY_OUTDATED — Verify then close**

Evidence found:
```bash
git log --oneline --grep="sentry.*sample\|sentry.*rate\|sentry.*noise" -i | head -5
# a00282d45 Lower Sentry sample rates
# 086ec732e Lower Sentry traces sample rate (#4757)
# 7979c5fd3 Add Sentry collector exclusions for spammy exceptions
```

Partial progress: sample rates reduced across `mit_learn`, `learn_ai`,
`mitxonline`; exception exclusions added. However the issue is framed as an
epic — child issues may still be open. **Recommended action**: check all child
issues; close this epic if they are resolved.

---

### #4340 — Remove kubewatch from #product-learn-ai / fix Slack posting
**Verdict: POSSIBLY_OUTDATED — Verify then close**

Evidence found:
```bash
git log --oneline --grep="kubewatch" | head -5
# b960d0b39 fix: Remove product specific kubewatch channels
# 3fd32aca8 fix: kubewatch slack posting
# 8d5779cc4 Revert "fix: kubewatch slack posting"
# 19fc8b546 fix: kubewatch slack posting (second attempt)
```

Channel removal is confirmed. Slack posting went through fix → revert → re-fix.
**Recommended action**: verify Slack delivery is working in production before
closing; the code path is correct but operational confirmation is missing.

---

## STILL_RELEVANT examples

### #1745 — Require IMDSv2 on EC2 instances
**Verdict: STILL_RELEVANT — Quick win**

Evidence found:
```bash
grep -n "http_tokens" src/ol_infrastructure/components/aws/auto_scale_group.py
# 447:  http_tokens="optional",

git log --oneline --grep="imdsv2\|http_tokens" -i
# (no output)
```

The requested one-line change (`"optional"` → `"required"` at
`auto_scale_group.py:447`) has not been made. No related commits since the
issue was opened in September 2023.

---

### #3694 — Specify OpenAPI Generator version in pipeline config
**Verdict: STILL_RELEVANT — Quick win**

Evidence found:
```bash
grep -n "openapi_generator_tag" \
  src/ol_concourse/pipelines/libraries/api_clients_pipeline.py
# 59:  openapi_generator_tag = "v7.2.0"
```

The issue asks to update from v7.2.0 to v7.16.0. The value is unchanged.
This is a one-line edit.

---

### #4485 — Slack Release Bot: replace Doof with a stateless Slack Bolt K8s service
**Verdict: STILL_RELEVANT — Active work in flight (do not close)**

Evidence found:
```bash
git branch -r | grep release-bot
# origin/feat/slack-release-bot

ls src/ol_infrastructure/applications/release_bot/
# __init__.py  __main__.py  bot.py  concourse_client.py  Dockerfile  ...

git log origin/feat/slack-release-bot --oneline | head -3
# (21 commits ahead of main, last touched 2026-06-18)
```

Full implementation exists on `feat/slack-release-bot` but is not merged to
main. Closing this issue would be premature.
