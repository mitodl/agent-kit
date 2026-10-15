### Added

- `task_search` takes `project_slug` and `assignee` (`"@me"` included), the
  filters `witan tasks QUERY --project/--assignee` already offered. Agents
  asked for "the next task in project X" reached for them and got a schema
  rejection. A project search spans every repo. With either filter the
  20-hit cap applies after filtering, so a match ranked below the unfiltered
  query's top 20 is still found.

### Changed

- `witan tasks QUERY` passes `--project` and `--assignee` to `task_search`
  instead of filtering its 20 hits client-side. Against a deployment older
  than this release, a search with either flag fails until the deployment is
  upgraded.
