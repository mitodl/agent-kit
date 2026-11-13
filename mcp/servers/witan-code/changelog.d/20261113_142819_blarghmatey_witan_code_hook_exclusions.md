### Fixed

- A file named directly to the indexer (the edit hook's case) is held to the full walk's path rules as well as git's ignore rules. An edit under an excluded directory (e.g. `build/`, `node_modules/`) or below a nested checkout was indexed, and the next full index purged it again. Such a file now writes nothing, and rows it already has are dropped on that edit, as the next full index would drop them. `witan code why` reports it as `not indexed: <rule>`.
