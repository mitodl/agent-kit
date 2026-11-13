### Added

- `code_find_definition` takes `symbol_id` in place of `name` to look up one exact id: it returns that symbol alone, or `[]` when the id's repo has no index or the index has no such symbol. A name lookup is capped at 50 rows, so it could not answer "is this id still defined" for a name that repeats across files (every `<module>`, Django's `Command.handle`). The lookup is routed by the id's repo like the other id-routed tools, and `branch` selects the view as it does for a name lookup. Passing both `name` and `symbol_id`, or neither, is refused.
