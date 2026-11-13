### Security

- Text a teammate wrote now reaches an agent labelled as data, not
  instructions, on every path that delivers it unasked. The context hook's
  block opens and closes with a line saying its record text may not override
  the user or `AGENTS.md`, so an instruction planted in a
  task title or comment is never the last thing read before the user's prompt.
  The `New Comments on Work You Hold` lead-in calls a comment a teammate's note
  to check against live state. A first `task_claim` that surfaces memories
  returns a `memories_note` saying the same. The wording lives in
  `witan.provenance` so the hook, `task_claim` and the `witan task run` prompt
  say it the same way. Nothing screens the text yet; this only labels it.
