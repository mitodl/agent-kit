### Changed

- The `witan-code` skill points at `recall(symbol_id=…)` for the memories and
  tasks attached to a symbol, since witan folded `symbol_context` into
  `recall`.
- `witan code diff-impact` reads each changed symbol's tasks and memories
  through `recall(symbol_id=…, hops=0, include_superseded=True)`. It still
  called the removed `symbol_context`, so it failed on every changed non-test
  symbol when witan was installed.
