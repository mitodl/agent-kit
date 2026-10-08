### Fixed

- `reported_internal_schema()` reads omnigraph 0.13's version line,
  `internal-schema 14 (serves v14 to v14)`. It took the last token as the
  number and raised `ValueError` on it.
- A failed CLI call's error is read from stdout when stderr is empty. On
  omnigraph 0.13 a server-backed read made with `--format json` prints its
  failure as JSON on stdout, so those failures were classified as fatal and
  raised with no message.
- The 0.13 CLI's "server discovery connection failed … this data request was
  not sent" is recognised as an unreachable server and waited out, as
  `tcp connect error` was.
- A `503 graph_unavailable` for a blocked graph ("an operator must apply an
  explicit correction or restart") fails at once. Over HTTP it was retried
  for the whole restart budget, and a blocked graph stays blocked until the
  next restart. The loading and transitioning 503s are still waited for, now
  over the CLI as well.
- CLI exit code 75, and a read that fails with the `too_many_requests` code,
  are classified as the admission cap.
