### Added

- `code_callers` takes `direction="callees"` to list the symbols a function calls, following the same `Calls` edges outward. The default, `direction="callers"`, is unchanged. Like the callers query it reads the checkout's branch view for the current repo.
