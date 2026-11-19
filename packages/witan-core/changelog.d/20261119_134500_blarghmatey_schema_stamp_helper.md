### Added

- `witan_core.omnigraph.stamp_schema_applied(schema_file, store)` records a schema as applied to a local store, the stamp `schema_apply_if_changed` compares against. `schema_apply` writes its stamp through it. It is for a caller that knows the store is current without running the apply, e.g. witan-code when `schema plan` reports no steps, so both paths write the same stamp. Like `schema_apply`, it never raises on a directory it cannot write.
