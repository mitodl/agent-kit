### Fixed

- **`memory_list` no longer comes back short when superseded memories sit
  inside its 100-row cap.** The listing read the 100 newest memories and then
  dropped the superseded ones, so a repo with more than 100 memories, some
  superseded, got fewer than 100 rows with no sign that more current memories
  existed. The default read now excludes superseded memories in the query
  (`list_current_memories*`), so fewer than 100 rows is the whole listing and
  exactly 100 means there may be more. `language` filtered after the cap in
  the same way; a language-filtered listing now reads every memory, filters,
  and then takes the 100 newest. The Witan UI's Memory tab says when its
  browse list is at the cap.
