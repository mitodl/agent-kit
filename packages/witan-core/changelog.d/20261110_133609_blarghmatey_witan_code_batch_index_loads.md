### Changed

- `chunking.chunk_records` packs nodes and edges into one sequence of
  batches (every node still before any edge) instead of batching the two
  groups apart. The batch holding the last nodes now takes the first edges,
  so a load that fits the budget is one batch, and one commit, instead of
  two.
