### Changed

- Pins omnigraph 0.14.0 (`v0.14.0`, digests from the release), which writes
  storage format 16 and reads 14 to 16. `_OMNIGRAPH_INTERNAL_SCHEMA` moves
  14 to 16 and `HTTP_API_CONTRACT` to `0.14`. A 0.13 server and this client
  refuse each other, and the first write a 0.14 binary makes to a format-14
  graph stamps it 16, which a 0.13 binary then refuses. Move the data tier
  before the clients, in the cutover window; there is no way back once a
  0.14 server has written.
- A CLI failure's JSON error object on stdout is classified even when stderr
  is not empty. 0.14 prints the resolved target on stderr for every call, so
  a read's failure (a recovery barrier, a conflict, an unreachable server)
  was otherwise classified from that line alone and reported as fatal.
- The 0.14 CLI's exits are read: 2 (partial or unknown effects) is never
  retried and a write raises `WriteIndeterminate`, 64 (a refused command
  line) is fatal, and 75 from a 503 gets the unavailable-server budget
  instead of the admission cap's. Adds the `OUTCOME_UNKNOWN` outcome kind.
