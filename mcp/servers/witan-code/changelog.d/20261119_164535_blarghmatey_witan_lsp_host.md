### Added

- `witan-code lsp` starts the same witan language server as `witan lsp` (ADR 0012), so with only witan-code installed an editor still gets the code features once they land. It needs the new `lsp` extra (`uv tool install 'witan-code[lsp]'`). witan-code registers a plugin under `witan.lsp.plugins` that provides no features yet; it routes each workspace folder to a `RemoteServerProxy` for a deployed target, or points the in-process server at that folder's local stores.
- `config.load` and `load_remote_config` take `project_path=`, which routes by that checkout instead of the process's.

### Changed

- Requires witan-core 0.59.0.
