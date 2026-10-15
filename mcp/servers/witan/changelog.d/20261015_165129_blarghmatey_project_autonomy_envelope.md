### Added

- Workflow projects carry an autonomy envelope: `preauthorized` (actions an
  agent may take on the project without asking) and `reserved_decisions`
  (decisions kept for a human). `workflow_project_create` and
  `workflow_project_update` take both, with replace-list semantics and `[]` to
  clear; `workflow_project_get`, `workflow_project_status`,
  `workflow_project_list` and `workflow_project_search` return them. The CLI
  takes `--allow` / `--reserve` (repeatable) on `project create` and
  `project update`, and shows both in `project status` and `project <slug>`.
- The context hook renders a project's envelope under its bullet as
  `Pre-authorized:` and `Reserved for a human:`, capped at 5 items of 160
  characters each. `witan project run` puts the full lists in its prompt.
- The witan-project-tracker skill has an "Autonomy Envelope" section: treat
  pre-authorized items as standing approval, batch reserved ones into one list,
  and read historical notes as past state rather than standing rules.

### Notes

- `schema.pg` adds two nullable `[String]?` properties to `WorkflowProject`.
  It is an additive migration (`omnigraph schema plan` reports it supported),
  and a local store picks it up on its own. A deployed graph needs the schema
  applied before the witan image that writes the fields rolls out.
