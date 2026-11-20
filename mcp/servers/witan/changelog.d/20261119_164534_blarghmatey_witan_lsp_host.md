### Added

- `witan lsp` runs the witan language server on stdio for an editor to start (ADR 0012). It needs the new `lsp` extra (`uv tool install 'witan-council[lsp]'`). witan-council registers a plugin under `witan.lsp.plugins` that provides no features yet; it routes each workspace folder through the CLI's own dispatch, a `RemoteServerProxy` for a deployed target and the local store behind `local_server`'s guard otherwise.
- `config.load`, `load_remote_config` and `diagnose_local_dispatch` take `project_path=`, which routes by that checkout instead of the process's.

### Changed

- Requires witan-core 0.59.0.
