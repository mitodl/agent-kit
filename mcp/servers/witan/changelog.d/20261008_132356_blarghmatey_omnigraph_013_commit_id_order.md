### Fixed

- `task_claim`'s post-write verification no longer compares graph commit ids
  as strings. From omnigraph 0.12 an id is `hb1.<block>.<slot>.<nonce>` with a
  decimal slot, so from the tenth write in a block a newer commit sorted as
  older and the catch-up check retried to its cap, or trusted a stale read.
  The ids are parsed: slot order within a block, the commits' own ULIDs
  otherwise.
