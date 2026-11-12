### Removed

- Breaking: `task_unlink` is gone. Pass `remove=True` to `task_link`, with the
  same `from_slug`, `to_slug` and `kind`, to remove a link. The result and the
  `blocked_by`/`parent_slug` resync are unchanged. `witan task unlink` still
  works and calls it.
- Breaking: `workflow_project_search` is gone. Pass `query` to
  `workflow_project_list` for the same BM25 search over title and description,
  ranked and capped at 20. Its `repo`, `status`, `phase` and `ready` filters
  apply to the matches.
