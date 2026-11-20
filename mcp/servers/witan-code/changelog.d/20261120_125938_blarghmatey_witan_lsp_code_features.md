### Added

- The code features of the witan language server (ADR 0012): a callers lens on each function, method and class and a cross-repo impact lens on each symbol with interface bindings (both resolved lazily, with `witan.listCallers` and `witan.listCrossRepoImpact` returning the rows behind them); hover on an env var, endpoint, package or service binding listing its providers and consumers; definition across indexed repos and references within the definition's repo, with a location in a repo no workspace folder has checked out named in the hover instead; and one Information diagnostic when the folder's repo or branch has no index. It also resolves a position to a symbol id for witan-council's symbol-scoped features. Every read passes the folder's repo and current checkout branch, and a folder on the repo's default branch reads the main view. A definition not in the folder's own repo is looked up in the other open repos at their checkout branches, and at main only in repos no folder has open.
- `code_interface_bindings_in_file(path, repo, branch)` lists one file's interface bindings with their kind, key and line.
- `code_find_references` takes `branch`, like `code_callers`.
- `server.use_config(..., follow_checkout=False)` detaches the in-process server from its cwd checkout: a read that names no repo fans out, one that names no branch reads main, and a path is relativised against its own checkout rather than the cwd's.

### Changed

- Requires witan-core 0.60.0.
