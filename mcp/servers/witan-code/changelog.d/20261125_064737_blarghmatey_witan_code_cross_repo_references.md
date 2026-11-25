### Added

- The indexer records each use of a name imported from a shared package namespace (today `mitol.*`, the namespaces package consumers are extracted for) whose module is not a file of the repo, as `ExternalUse` rows in the bridge: every load of the name (a call, a base class, a constant), with its line and enclosing symbol. A reindex replaces a re-parsed file's uses, and a full-repo index drops the uses of a file that left the repo.
- `code_find_references` on a definition in a shared package namespace also returns its uses in other indexed repos (at most 200), matched by its module path and by the parent package that re-exports it (`mitol.common.utils.now_in_utc` as well as `mitol.common.utils.datetime.now_in_utc`). The language server's references therefore cross repos, and hovering a definition's own line names its uses in repos no workspace folder has open. This replaces the 0.34.0 limit of references to the definition's own repo.

### Changed

- `INDEX_VERSION` is 3, so each repo's next index of its default branch re-parses every file and writes its uses. The `code-bridge` graph has to carry the `ExternalUse` schema (0.35.0) before this release indexes into it, because a load carrying a type the graph lacks fails the whole bridge write; a deployed witan has to run this release before a branch index through it writes uses, since the writer reads the new `external_use_files` query.
