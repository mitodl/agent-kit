### Added

- `witan code why <path>` says whether a file is in the code graph, in the view the `code_*` tools read from its checkout, and if not, which indexer rule left it out (unsupported language, skipped directory, nested checkout, symlinked directory, gitignored) with the fix. An empty `code_*` result reads the same for an unused symbol and for a file the indexer never wrote; this tells the two apart. The rules are the indexer's own filter, now one function both the git-listing and disk-walk paths call, so the explanation cannot drift from what gets indexed. It reads the `--target`'s stores, and refuses (naming `code_transport`) when a remote URL sends the `code_*` reads to a deployment while the code-graph stores are local, since its answer would describe a graph those reads never use.

### Fixed

- `witan --target <name> code doctor` reads that target's code graphs. `doctor` used the config loaded when the server module was imported, which never sees a forwarded `--target`.
