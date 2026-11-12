### Added

- `witan code why <path>` says whether a file is in the code graph, in the view the `code_*` tools read from its checkout, and if not, which indexer rule left it out (unsupported language, skipped directory, nested checkout, gitignored) with the fix. An empty `code_*` result reads the same for an unused symbol and for a file the indexer never wrote; this tells the two apart. The rules are the indexer's own filter, now one function both the git-listing and disk-walk paths call, so the explanation cannot drift from what gets indexed.
