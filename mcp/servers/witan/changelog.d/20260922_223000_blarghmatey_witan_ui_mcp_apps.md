### Added

- **MCP Apps widgets for `task_ready`, `workflow_project_status`, `recall` and
  `task_list`, so Claude Desktop and claude.ai draw those results inline.**
  Each tool names a `ui://witan/<tool>.html` resource in `_meta.ui.resourceUri`;
  a host that renders MCP Apps puts it in a sandboxed iframe and hands it the
  tool's result. `task_ready` draws the Ready column with each claim's age,
  `workflow_project_status` the rollup with its ready list and last session,
  `recall` the ranked memories with any contradicting pair called out, and
  `task_list` the rows grouped by status. The widgets reuse the web UI's own
  renderers and call nothing back to the server.

  The tool results are unchanged. Claude Code renders no widgets and shows the
  text result, which stays byte-identical with and without the binding. A tool
  is bound only when its widget was built, so a source install that skipped
  the frontend build registers the four tools exactly as before.
