### Added

- `task_ready` and `task_list` take `include_unfiled` (default true). `false`
  keeps a repo-scoped read to that repo's own tasks, leaving out the tasks
  filed under no repo.
- `witan lsp`'s `witan.listReadyTasks`, `witan.listProjects` and
  `witan.listMyWork` take `scope` (`repo`, `repo+unfiled` or `all`) or
  `project` (a `wp-` slug, in any repo), and answer with the scope they
  applied. A folder with no git remote answers the `all` and `project`
  scopes (with no tasks for its branch) and refuses the repo scopes. The
  editor guide's Neovim commands take it as an argument, and its Emacs
  commands ask for it under a prefix argument.

### Changed

- `witan lsp`'s work lists default to the `repo` scope, so a task filed under
  no repo no longer shows among a repo's ready tasks unless the `repo+unfiled`
  scope is asked for. A deployment that predates `include_unfiled` refuses
  the `repo` scope, so against one the lists fall back to `repo+unfiled` and
  answer that scope.
