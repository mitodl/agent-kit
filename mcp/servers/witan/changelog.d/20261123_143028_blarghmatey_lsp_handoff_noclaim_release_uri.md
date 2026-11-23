### Added

- `witan.runTask` takes `claim: false`, which returns the unclaimed task spec a
  chat-panel handoff needs. Its prompt has the agent claim the task first.
- A launch spec from `witan.runTask` that took a claim carries `release`, the
  `slug`, `claimed_at` and `uri` to pass `witan.releaseTask` unchanged.

### Changed

- `witan.releaseTask` requires `uri` and no longer defaults to the first
  workspace folder. The release has to go through the folder the launch was
  invoked from, because the spec's `cwd` can be another folder that routes to
  a different graph.
- A launch spec that took a claim always has `handoff: false`, from the CLI's
  `--emit-spec` as well as the language server.
