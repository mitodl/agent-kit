### Added

- `code_callers` accepts an optional `branch`, with the semantics `code_impact` and `code_find_definition` already have. A deployed server has no checkout to follow, so a remote caller could only read the default view; `branch="feature/x"` reads that branch's callers or callees. Omitted, the tool follows the checkout's branch view for the current repo as before.
