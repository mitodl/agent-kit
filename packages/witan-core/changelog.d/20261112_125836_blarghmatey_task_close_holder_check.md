### Added

- `RemoteMCPProxy._replay_result(entry, plan, result)`, called with a replayed
  write's result before the replay reports it applied. A binding whose tool
  refuses by returning an answer rather than raising returns a substitute
  entry, which is sent in the write's place and reported `substituted`. A
  retryable write whose substitute cannot be sent stays queued. The default
  returns `None`, so replay behaves as before.
