### Added

- `CodeFile.index_version: I32?` in `code-schema.pg`, for recording which indexer version wrote a file's rows. Nothing writes or reads it yet. It ships ahead of the indexer change that uses it so that every store has the field before any client writes it. On omnigraph 0.15 `schema plan` and `cluster plan` both report it as one supported `add_property` step.

### Fixed

- A local store with branch views now takes a schema change. omnigraph 0.15 refuses `schema apply` on a graph with any branch but `main` ("schema apply requires a graph with only main") while its plan still reports the change as supported. The apply failed quietly on every index, so such a store never gained a new field or index (e.g. the `Symbol.docstring` index of 0.31.0). When the plan has steps and the apply fails with that refusal, the store's views are deleted and the apply runs again. Any other failure leaves them. A plan with no steps (a reinstall rewrites the schema file's mtime but not its content) marks the store current without an apply, which omnigraph would refuse too. A view is a cache, and the next full index of its branch rebuilds it from `main`. The bridge store follows the same rule, and when it drops its views every local code store drops its views too, so the next index of each branch parses the files it changed and writes their bindings again. If the apply still fails once the views are gone, they are not dropped again until the schema file changes, so a broken store does not lose its views on every index.

### Changed

- Requires witan-core 0.58.0, whose `stamp_schema_applied` writes the stamp for a plan with no steps, so that path and `schema_apply` record the same thing.

### Notes

- Deploying an omnigraph-server image built with this schema deletes every branch view on every `code-<repo>` graph. The plan gate does this through the serving pod (#663), as it did for the `Symbol.docstring` index.
