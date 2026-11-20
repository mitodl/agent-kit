### Added

- The code features of the witan language server (ADR 0012): a callers lens on each function, method and class and a cross-repo impact lens on each symbol with interface bindings (both resolved lazily, with `witan.listCallers` and `witan.listCrossRepoImpact` returning the rows behind them); hover on an env var, endpoint, package or service binding listing its providers and consumers; definition and references across indexed repos, with a location in a repo no workspace folder has checked out named in the hover instead; and one Information diagnostic when the folder's repo or branch has no index. It also resolves a position to a symbol id for witan-council's symbol-scoped features. Every read passes the folder's repo and checkout branch.
- `code_interface_bindings_in_file(path, repo, branch)` lists one file's interface bindings with their kind, key and line.
- `code_find_references` takes `branch`, like `code_callers`.

### Changed

- Requires witan-core 0.60.0.
