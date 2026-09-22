### Added

- **The Witan UI's Memory tab.** It opens on a contradictions inbox: every
  unresolved Contradicts pair in scope, both bodies side by side with their
  authors, timestamps and the link's provenance, plus the two `memory_link`
  calls that resolve the pair. Below it, memories can be browsed
  (`memory_list`), recalled (`recall`, with recall's own contradiction pairs
  marked) or plain-searched (`memory_search`), narrowed by kind, language,
  category, severity, tag and author, and listed by topic (`topic_get`). A
  memory opens in the panel with its topics and every neighbour grouped by
  edge kind, each showing the link's confidence, role and author. Asserted and
  inferred links are drawn differently, since recall weights them
  differently. Superseded memories are hidden unless "Show superseded" is on.
- **`memory_neighbors` reports `superseded_by`**, the inbound side of
  `supersedes`: the memories that replaced this one. A reader who lands on a
  superseded memory had no way to find what to read instead.

### Changed

- **`memory_list` hides superseded memories by default**, as `memory_search`
  and `recall` already did. `include_superseded=True` keeps them.
- **`memory_contradictions` drops a pair once either side is superseded**,
  since superseding is how a contradiction is resolved, and `recall` never
  reported such a pair anyway. `include_superseded=True` keeps them.
