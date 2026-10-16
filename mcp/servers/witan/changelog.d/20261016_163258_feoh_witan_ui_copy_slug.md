### Added

- The web UI puts a copy button beside project and task names (the project list, a project's header, Ready list and task table, board cards, search results, the task detail panel, and the `task_ready`, `task_list` and `workflow_project_status` widgets). It copies the slug, not the title, for pasting into the `witan` CLI. A tick shows when the copy lands and a cross when it fails, and both are announced to screen readers. Where the Clipboard API is unavailable or refused (a non-secure context, or a widget iframe without `clipboard-write`) it falls back to the `copy` command.
