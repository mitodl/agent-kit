### Added

- Workflow projects carry an autonomy envelope: `preauthorized` (actions an
  agent may take on the project without asking) and `reserved_decisions`
  (decisions kept for a human). `workflow_project_create` and
  `workflow_project_update` take both, with replace-list semantics and `[]` to
  clear; `workflow_project_get`, `workflow_project_status`,
  `workflow_project_list` and `workflow_project_search` return them. The CLI
  takes `--allow` / `--reserve` (repeatable) on `project create` and
  `project update`, and shows both in `project status` and `project <slug>`.
- `workflow_project_update` writes an envelope list only when it is passed,
  through its own mutation, so an unrelated edit can't restore a grant or drop
  a reservation another session changed after it read the row.
- The secret/PII scanner checks each envelope item on create and update, with
  the configured block/redact/warn action applied per item.
  `workflow_project_update`'s title and description edits are now scanned too;
  they went through a mutation the scanner had no entry for.
- Each envelope list records who last set it (`preauthorized_set_by`,
  `reserved_decisions_set_by`), resolved server-side the way `author` is and
  never taken from the caller. Writing a list makes the caller its setter for
  every item in it.
- The context hook renders a project's envelope under its bullet, naming the
  setter, capped at 5 items of 160 characters each. A `preauthorized` list set
  by the project's author reads as `Pre-authorized by the project author`;
  one set by anyone else reads as a suggestion to confirm with the user.
  `witan project run`, `project status` and `project <slug>` show the full
  lists the same way.
- The witan-project-tracker skill has an "Autonomy Envelope" section: treat
  pre-authorized items as standing approval only when the author or the
  current user set them, batch reserved ones into one list, and read
  historical notes as past state rather than standing rules.

### Notes

- `schema.pg` adds four nullable properties to `WorkflowProject` (two
  `[String]?` lists and their `String?` setters).
  It is an additive migration (`omnigraph schema plan` reports it supported),
  and a local store picks it up on its own. A deployed graph needs the schema
  applied before the witan image that writes the fields rolls out.
