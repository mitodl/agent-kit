### Added

- **The UI's Waves tab: how many serialized rounds of work a project has
  left.** One project's open tasks are laid out by depth in the `Blocks` graph
  rather than by date, since tasks carry no estimate or due date: wave 0 has no
  open blocker, and each later task sits one wave past its deepest one. The
  longest chain is drawn heavier, each task shows how many tasks wait on it,
  and the page names the wave-0 task that unblocks the most. Blockers from
  other projects are read with `task_get` and drawn as dashed stubs; closed or
  deleted ones hold nothing back, as in `task_ready`. A cycle in the `Blocks`
  edges is drawn and named rather than failing the layout. `task_ready` is read
  alongside and the page lists any task where it and wave 0 disagree, instead
  of trusting its own copy of the readiness rule.
