### Added

- `OmnigraphClient.change_many` takes `if_commit`, fencing the whole batch the way it fences `change`: the commit applies only while that branch head is current, over HTTP (`/mutate/if-graph-commit`) and the CLI (`--if-commit`), and a refusal means no step was applied. It cannot be combined with `chunk_size`, since chunks are separate commits.
