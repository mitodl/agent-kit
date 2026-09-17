# mitodl conventions checklist

Extra checks for diffs in `mitodl` repositories, on top of the four dimensions in
[dimensions.md](dimensions.md). They come from review comments that recur across
years of mitodl infrastructure and data-platform reviews. A repo's own `AGENTS.md`
or `docs/context/` guides take precedence where they cover the same ground.

Each check is a prompt to look, not an automatic finding. A hit still has to pass
the [verification pass](../SKILL.md#verification-pass) with a concrete failure
scenario or cost, and slots into the dimension shown.

Check them in this order; earlier ones cost more when missed.

| # | Check | Dimension | What makes it a finding |
|---|---|---|---|
| 1 | Secrets in plaintext: credentials in code, userdata, pipeline definitions, or unencrypted YAML instead of SOPS files, Vault, or Pulumi `secure:` values | Correctness | Name the value and where it would be exposed. If it's a real credential already pushed, say it needs rotation, not only removal |
| 2 | IAM or Vault policy broader than the change needs (`*` resources, a suppressed `RESOURCE_STAR` lint, a shared credential used for a new purpose) | Correctness | Name the action/resource the code actually uses and the wider grant |
| 3 | Environment-specific values hardcoded: ARNs, account or VPC IDs, bucket names per environment, hostnames | Correctness | Show the environment where the literal is wrong (the value is valid for QA but the stack also deploys to Production). Values belong in stack config (`Pulumi.<project>.<Env>.yaml`) |
| 4 | Versions hardcoded where the repo centralizes them (ol-infrastructure: `src/bridge/lib/versions.py`, which carries Renovate annotations) | Reuse | Cite the existing constant; the cost is a version Renovate can't bump |
| 5 | Required configuration read with `.get()` / `config.get()` so a missing value becomes `None` | Correctness | The input that's missing and what then deploys or runs with `None`. Use `mapping["key"]`, `os.environ["X"]`, or `config.require()` |
| 6 | A Pulumi logical resource name, Helm release name, or component name changed on an existing resource without `aliases` | Correctness | `pulumi preview` shows a delete/replace of a live resource. Name it |
| 7 | New code re-implements an existing component or helper (`OLAmazonDB`, `OLVPC`, `parse_stack`, shared security groups exported by the network stack) | Reuse | Cite the existing implementation's file:line |
| 8 | Structured data rendered by string templating (hand-built JSON or YAML in f-strings or triple-quoted strings) | Correctness or Simplification | The escaping or quoting input that breaks it, or the duplication with the data it mirrors. `json.dumps`, pydantic models, or `\|tojson` instead |
| 9 | Open edX deploy ordering: CMS migrations able to run before LMS migrations, or both concurrently | Correctness | The two share models; name the job ordering that allows it |
| 10 | A component resource exposes its input config model instead of the resources it created | Simplification | The consumer that has to reach through config to find a resource |
| 11 | Magic numbers without a named, unit-bearing constant (`ONE_MONTH_SECONDS`), or a constant named after its first consumer | Simplification | Only when the number is reused or its unit is ambiguous at the call site |
| 12 | Dev/test tools added to the main dependency group | Efficiency | The runtime image or install that now carries them |
| 13 | dbt models: `union all` of per-platform sources that yields duplicate entities; join keys tied to one source database's IDs instead of domain keys; no rule for which source wins when several feed one record; `not_null` tests on columns populated by a left join | Correctness | The duplicate or failing row, or the record whose value flips between runs |

## Not findings on their own

- Formatting and import order. Pre-commit (ruff, yamlfmt, prettier) owns them.
- Missing tests or docstrings in general. Raise one only when a specific untested
  path or a docstring that contradicts the code is part of a real finding.
- A single hardcoded value in a workflow file that already exists once per
  environment. That's the repo's chosen layering, not a leak across environments.
