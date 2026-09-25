### Fixed

- **`witan-workflow` and `witan-task` no longer require Claude's
  `AskUserQuestion`.** Both skills told the agent to build an
  `AskUserQuestion` call and to use its "Other" option for free text, which Pi
  (and any agent without that tool) cannot do. Each skill now has an "Asking
  the user" section: use a structured question tool when one is actually
  available, otherwise ask the same question in a normal message and wait for
  the reply. Every choice, limit and confirmation gate is kept, and the skills
  now state outright that nothing is claimed, created or started before the
  user answers. A test keeps `AskUserQuestion` out of the bundled skills except
  inside that capability-scoped section.
