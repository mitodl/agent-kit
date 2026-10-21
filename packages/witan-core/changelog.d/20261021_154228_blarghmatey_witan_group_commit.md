### Performance

- Concurrent remote writes from one actor to one graph now share a commit
  (`witan_core.group_commit`). A write on an idle graph commits at once and
  alone, as before. Writes that arrive while a commit is in flight wait for it,
  and the oldest of them then commits everything queued as one composed
  mutation (at most 64 statements and about 256 KiB of params). On omnigraph
  0.13 commits to one branch serialize, so this is what lets a graph take more
  than about one write a second. Against a local omnigraph-server 0.13.0, 240
  single-row writes from 16 threads went from 240 commits at 27 writes/s to 30
  commits at 244 writes/s. A single thread is unchanged.

  Each caller still gets its own outcome. A group holds one actor (omnigraph
  authorizes a request once and records one actor on its commit), never mixes
  deletes with inserts or updates, and carries at most one fenced
  (`if_commit`) write. That write goes first and the commit is fenced on its
  head. A refused fence fails only that caller, with the error it would have
  had alone, and the writes queued with it go into the next commit. A queued
  fenced write whose head a commit from this process has already replaced is
  refused without a round trip. A group the server rejects with 400 or 413
  before any effect, or that is missing a parameter, is replayed one write at
  a time. A 400 carrying a DataFusion error (`query: ...`) is excluded,
  because omnigraph marks that one completion-uncertain. A write whose
  `WITAN_REMOTE_CALL_BUDGET_SECONDS` runs out while it queues is refused
  unsent with `WriteQueueFull`. Any other failure, including one whose outcome
  is unknown, reaches every member, so every member of that group re-reads
  before retrying.

  Only remote writes over the HTTP transport are grouped. Set
  `WITAN_GROUP_COMMIT=0` to turn grouping off.
