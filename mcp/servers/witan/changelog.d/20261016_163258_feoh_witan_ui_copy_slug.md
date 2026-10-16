### Added

- The web UI puts a copy button beside project and task names (the project list, a project's header, Ready list and task table, board cards, the task detail panel, and the `workflow_project_status` widget). It copies the slug, not the title, for pasting into the `witan` CLI, and shows a tick when the copy lands. Where the Clipboard API is unavailable (a non-secure context, or a widget iframe without `clipboard-write`) it falls back to the `copy` command.
