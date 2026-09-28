### Fixed

- **Endpoint bindings form cross-repo edges again.** Every endpoint consumer
  scored 0.0 because the extractor only matches relative paths and the
  `relative_url` heuristic subtracted 0.5 from the 0.5 baseline, so
  `code_repo_dependencies` and the other heuristic-tier views filtered every
  one out at their 0.5 cutoff. The `relative_url` penalty, its unreachable
  `explicit_hostname` boost, and the `generated_file` penalty are removed. A
  generated client for a repo's own API is still suppressed by
  `self_provided_key`. Stored confidences are computed at index time, so
  existing graphs need `witan-code reindex` (not `index`, which skips
  unchanged files) on the consumer repos before these edges appear.

### Added

- **Python package providers.** A `pyproject.toml` whose directory holds a
  `mitol/<pkg>/__init__.py` namespace child now provides `mitol.<pkg>`, so
  `mitol.*` imports join to ol-django. Previously only `@mitodl/*` npm
  packages had a provider extractor and no Python import could form an edge.
  An existing graph gains these providers on ol-django's next full-repo
  `witan-code index` (the provider files are not source files, so the
  unchanged-file skip does not apply to them); `witan-code reindex` on it
  works too.

- **Repo-level provider files that stop producing bindings lose their old
  rows.** A `pyproject.toml`, `package.json`, Pulumi config or OpenAPI spec
  that emitted nothing on this run was never purged, so on a run that could
  not purge by the collected file list (e.g. indexing outside a repository
  root) its previous bindings stayed. Every such file scanned is now purged
  before its fresh bindings are written.
