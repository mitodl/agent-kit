### Added

- `RemoteMCPProxy` can queue a write it could not send in a per-deployment
  journal (`witan_core.remote.journal`, under `$XDG_DATA_HOME/witan/journal/`)
  and returns a `{"buffered": true, ...}` result instead of raising. The
  journal replays oldest first at the start of the next call whose connection
  opens, under an `fcntl` lock so two processes never send the same entry.
  A write that is unsafe to apply twice replays at most once: it leaves the
  journal before it is sent. Which tools queue is a binding's choice
  (`_buffers`); the base proxy queues nothing, and `buffer_writes=False`
  turns queueing off for one proxy. `replay_journal()` replays on demand.
  The journal file is created `0600`.
- `RemoteNotSent`, a `RemoteUnreachable` raised only when the connection never
  opened, so the call provably did not reach the deployment.
