### Added

- `witan lsp` answers `witan.listProjectTasks` (`project`, a `wp-` slug): the
  project's open tasks in dependency waves, each with its wave, open blockers
  (in and outside the project), cycle flag, place on the longest chain,
  downstream count and whether `task_ready` returned it. A project names no
  repo, so a folder with no git remote lists it too. The wave rule is
  `witan.waves`, the language server's twin of the web UI's waves view; both
  test suites run the cases in `ui/src/views/waves.cases.json`. The editor
  guide's Neovim `:WitanWaves` and Emacs `witan-waves` list it with a Wave
  column.
