### Added

- The witan-council language-server plugin provides its features (ADR 0012's feature map). A memory whose `symbol_refs` names a symbol the code graph no longer has gets an Information diagnostic on the first line of that file, listing the memory and its rename candidates. Hover on a symbol lists the tasks and memories whose `symbol_refs` name it, with their titles as escaped plain text; it needs a plugin that resolves positions to symbols, and is off without one. Commands: `witan.listStaleRefs` (every ref that is not live, including those whose file is gone), `witan.listReadyTasks`, `witan.listProjects`, `witan.listMyWork`, `witan.runTask` and `witan.runProject` (the launch spec, built in-process, with `handoff` always false because `serve()` has already dropped the overrides the check looks for; the server never starts the agent), and `witan.releaseTask` (fenced on the spec's `claim.claimed_at`, never forced). Each takes one object argument, with `uri` naming the workspace folder (or a file in it) it applies to.

### Changed

- The CLI's local-store guard lets `task_for_branch` read the local store, as it already did `task_ready`.
- The local-store guard refuses a write with `LocalWriteRefused`, a `SystemExit` that carries its explanation as `message`, so the language server can fail the editor's request with that text instead of an exit code. The CLI still exits 1.
