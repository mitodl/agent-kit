### Added

- `witan trace mine <slug>` prints a completed trace's material to mine, and
  with `--proposals <file.json>` creates the pattern and lesson memories.
  `witan trace annotate <slug> --lesson … --pattern …` records memories already
  written. Both accept the `wp-` or `wt-` slug.

### Changed

- `claim_authorship`, `store_merge`, `workflow_trace_list`,
  `workflow_trace_get`, `workflow_trace_annotate` and `workflow_trace_mine` are
  no longer in an agent's `tools/list`, over stdio or on the deployment. Agents
  called them a handful of times in 30 days, but ToolSearch loaded
  `claim_authorship` in 83 sessions and `store_merge` in 57. They stay callable
  by name and are listed to witan's own remote proxy (by its `clientInfo`),
  because that is how `witan migrate merge`, `witan migrate claim-authorship`,
  `witan traces` and `witan trace` reach a deployed graph. `witan serve`
  against a deployment no longer republishes them. A CLI on a witan-core older
  than 0.53.0 does not identify itself as the proxy, so against an upgraded
  deployment those commands report that the tool does not exist until the CLI
  is upgraded.
