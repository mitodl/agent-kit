### Added

- `witan memory import --map=<project-dir>=<repo-uri>` gives a project its
  repo in place of resolving it from disk, so memories of a deleted or moved
  checkout can be imported. `<project-dir>` is the project as the unresolved
  report names it (a Claude Code project directory, a Codex cwd, a Copilot
  workspace folder) or the absolute path it stood for. The repo is
  canonicalised like any explicit `repo` and must be a remote URI; a `--map`
  that names no project, or gives one project two repos, is refused.
