### Added

- An `ExternalUse` node in the bridge schema, one row per use of a name imported from another repo's package namespace (repo, file, line, enclosing symbol, full dotted target, language), with `bridge.gq` queries to insert one, purge a file's rows by (repo, file), and look rows up by a list of targets. Nothing writes or reads it yet. The `code-bridge` graph has to take this schema before any witan-code release writes the node, because a load carrying a type the graph lacks fails the whole bridge write.
