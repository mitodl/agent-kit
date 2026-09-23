### Fixed

- **`memory_list` no longer comes back short when superseded memories sit
  inside its 100-row cap.** The listing read the 100 newest memories and then
  dropped the superseded ones, so a repo with more than 100 memories, some
  superseded, got fewer than 100 rows with no sign that more current memories
  existed. The default read now excludes superseded memories in the query
  (`list_current_memories*`), so fewer than 100 rows is the whole listing and
  exactly 100 means there may be more. The Witan UI's Memory tab says so when
  its browse list is at the cap.
