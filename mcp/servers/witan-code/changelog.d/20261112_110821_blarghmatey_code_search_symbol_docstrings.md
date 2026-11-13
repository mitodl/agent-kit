### Added

- `code_search_symbol` matches symbol docstrings as well as qualified names, so a query like "rate limiting" finds a symbol whose docstring describes it even when its name does not. A second BM25 run over `Symbol.docstring` is appended after the name run, keeping only the symbols the name run did not return, so a docstring-only match never outranks a name match, across a cross-repo fan-out too. Each run returns up to 50 symbols per store.

### Notes

- `Symbol.docstring` gains `@index` in `code-schema.pg`. On omnigraph 0.15 both `schema plan` (a local store) and `cluster plan` (a cluster) report it as one supported `add_constraint` step, and the apply indexes the docstrings already stored, so no code graph needs a rebuild. A local store gains the index on its next index, through the schema apply that already runs when the schema file changes. A cluster's `code-<repo>` graphs gain it when an omnigraph-server image built with this schema boots and runs `cluster apply`. Until a store has the index, omnigraph 0.15 refuses the docstring run (`T27`). A client reading through a deployed MCP tier (`code_transport = "mcp"`) older than this release is refused too, because the tier looks the query up in its own bundled `code_read.gq`. In both cases `code_search_symbol` returns name matches only and logs `witan.code.search.docstring_search_unavailable`.
