### Added

- `witan task run --emit-spec` and `witan project run --emit-spec` print a JSON
  launch spec (`version`, `kind`, `slug`, `launchable`, `argv`, `cwd`, `env`,
  `unset_env`, `handoff`, `prompt`, `agent`, `model`, `claim`) instead of
  launching, for an editor or other launcher to run (ADR 0012, "3. The launch
  contract"). A refusal (`not_found`, `no_workspace`, or a refused claim
  passed through as `claim`) prints a spec with `launchable: false` and no
  `argv`, and exits 1. `build_launch_spec` in `witan.cli.run_helpers` returns
  the same spec as data, with no console output, for the language server.
- `task_release` takes `if_claimed_at`: it releases only when the task is still
  `in_progress` under the claim taken at that instant, and otherwise returns
  `reason: "claim_changed"`. `force` does not lift it. A launcher uses it to
  give back the claim of a launch that failed without releasing a claim the
  launched agent has since renewed. `witan task release --if-claimed-at` passes
  it from the command line.

### Changed

- `witan task run` claims bare. It hides every agent-session id variable
  (`CLAUDE_SESSION_ID`, `PI_SESSION_ID`, `CODEX_SESSION_ID`) from its own
  environment while claiming, so run from inside an agent session it no
  longer claims as that session; the launched agent's first renewal records
  its own. The launched agent, including the one the picker's consolidated
  mode starts, no longer inherits those variables, `CLAUDE_PROJECT_DIR` or
  `WITAN_REPO`, and gets `WITAN_TARGET` pinned to the target the claim was
  taken in (or unset, for the local store).
- `witan task run` and `witan project run` refuse when the current directory
  is not a checkout of the task's repo (or one of the project's repos), before
  claiming. A task or project with no repo runs anywhere. `--dry-run` skips
  the check.
- `witan task run` no longer offers to steal a held task at the terminal; it
  reports the holder and `--force` takes the claim. The question would have
  landed on stdout ahead of `--emit-spec`'s JSON.
- The open-blockers warning `witan task run` prints goes to stderr.
