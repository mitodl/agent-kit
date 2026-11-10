### Changed

- With witan-core's packed `chunk_records`, an index whose records fit one
  load batch sends its nodes and edges as one `code_store_load` instead of
  two, so a paced PostToolUse flush to a deployed graph is three commits on
  the branch view (delete, load, content-hash load) instead of four.
- Through an `OmnigraphClient` (a local store, omnigraph over HTTP, or an
  in-cluster index), a load that fits one batch carries the files' content
  hashes in the same commit instead of a second load, so the flush is two
  commits. One batch commits atomically, so a failed load still leaves the
  files without a hash and the next run re-indexes them. A
  `RemoteStoreClient` keeps the content-hash load, because the store tier
  re-chunks what it receives, and one running an older witan-core splits
  nodes from edges into separate commits.
