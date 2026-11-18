### Added

- `memory_stale_refs` takes `include_superseded` (default `False`) to check
  superseded memories' `symbol_refs` as well. The default call still checks
  current memories only.
- The web UI's Memory view passes `include_superseded` where its list can hold
  superseded memories (a topic page, or "Show superseded"), so a superseded
  memory citing a missing symbol is marked stale there instead of being
  labelled unchecked. The memory panel shows its missing refs the same way.
