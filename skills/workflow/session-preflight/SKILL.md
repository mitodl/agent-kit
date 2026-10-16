---
name: session-preflight
description: >
  Check at session start whether the credentials and services the work depends
  on are usable: gh auth, a Vault token, kubectl auth for the current context,
  the witan deployment's health, Docker (in repos that use it), and how far the
  checkout is behind origin's default branch. Runs automatically as a
  session-start hook on Claude Code and Codex; run it yourself with
  `/session-preflight` or `python3 scripts/preflight.py` elsewhere, or when a
  command fails with a 401, an expired token, "connection refused", or "Cannot
  connect to the Docker daemon". Use it to read the preflight lines in your
  context and relay each failure to the user with its fix command. Never use
  `op` to check 1Password sign-in.
license: BSD-3-Clause
metadata:
  category: workflow
---

# Session preflight

`scripts/preflight.py` runs a few read-only checks concurrently and prints one
line per failure, each ending in `Run: <command>`. It prints nothing when every
check passes. It never prompts, never repairs anything, and is cut off after 5
seconds.

| Check | Runs only when | Fails when |
| ----- | -------------- | ---------- |
| `gh auth status --active` | `gh` is on PATH | the active account is not logged in, or its token is invalid |
| `vault token lookup` | `vault` is on PATH and `VAULT_ADDR` is set | no token, or it expired |
| `kubectl auth can-i get pods` | `kubectl` is on PATH and a current context is set | the context cannot authenticate (a "no" answer is fine) |
| witan `/health` | a remote witan is configured for this checkout | the deployment does not answer within 2s |
| `docker info` | the repo root has a Dockerfile, `*.Dockerfile`, compose file or Tiltfile, or `docker/` holds a Dockerfile | the daemon is not reachable |
| `git rev-list HEAD...origin/HEAD` | the checkout has an `origin/HEAD` | more than 50 commits behind, as of the last fetch |

The witan check resolves the remote the way witan does: `WITAN_REMOTE_URL`,
then the `remote_url` of the `[targets.*]` block in `~/.config/witan/config.toml`
(or `WITAN_CONFIG`) that `WITAN_TARGET` names or that matches this checkout,
then the global `remote_url`. A
checkout with no remote uses a local store, so the check is skipped. It needs
Python 3.11 or later (for `tomllib`); on an older `python3` it is skipped.

## Reading the output

On Claude Code and Codex the lines arrive in your context at session start.
Otherwise, run the script from the skill directory:

```bash
python3 scripts/preflight.py
```

- Relay each failure line to the user once, with its `Run:` command. Don't
  repeat it every turn.
- Don't try to repair auth yourself (no `gh auth login`, `vault login`, cloud
  SSO logins or `docker` restarts on the user's behalf). These open browsers or
  need the user's credentials.
- Don't start, or keep retrying, work that depends on a failed check. Work that
  doesn't depend on it can go ahead.
- A `git:` line means the checkout is far behind the default branch. Confirm
  with the user that this is the worktree they meant before editing in it.
- A check that "did not answer" or "did not finish" timed out. That is usually
  the network or VPN, not the credential. Say so rather than calling the
  credential expired.
- Silence means every check that applied passed. A tool that isn't installed
  is skipped, not reported.

## 1Password

Don't run `op` (e.g. `op whoami`) to check whether the user is signed in to
1Password. In a noninteractive agent shell `op` fails even when the user's
desktop app is unlocked, so the failure says nothing about their session. Don't
record a 1Password state in notes, handoffs or memory unless the user confirmed
it.

## Installing the hook

`agent-config.toml` registers this skill in the `universal` profile with a
`session_start` hook running `python3 "{skill_dir}/scripts/preflight.py"`, so
`agent-kit apply` installs both. Codex runs the hook only after you trust it
with `/hooks` in the Codex TUI; `agent-kit apply` prints that reminder.
