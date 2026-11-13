### Added

- The web UI's Memory view marks a memory whose `symbol_refs` cite a symbol
  the code graph no longer has (`memory_stale_refs`), with a "Stale refs only"
  filter, and the memory panel shows each ref as missing (with same-named
  candidates) or unchecked (with the reason). A ref that could not be checked,
  e.g. in a repo nobody indexed, is counted above the list and never marked
  stale. `memory_stale_refs` joins the UI's bound read set, so the deployed
  page's read-only token may call it (ADR 0011, 2026-11-13 amendment).
