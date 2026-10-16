### Fixed

- `apply_all`, and `agent-kit apply` with or without `--prune`, now remove the
  copies of a skill that an earlier run left in `~/.pi/agent/skills` once
  Codex installs the same skill to `~/.agents/skills`. Before this, only
  `apply --prune` with a state file removed them. `witan setup --agent all`
  never prunes, so every run after Codex was installed left Pi with a
  "skill collision" warning for each witan skill. A copy is removed only after
  Codex's copy exists. Only the files the skill itself ships are deleted, and
  nothing is deleted through a symlinked skill directory or a Pi skills
  directory that points at `~/.agents/skills`. The removals are listed in
  `InstallResult.removed`.
