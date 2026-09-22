### Added

- **The UI's Timeline tab: where the last two weeks went, per project.** Each
  task is a lead-time bar from `created_at` to `closed_at` (or to the read, if
  still open), with the stretch since `first_claimed_at` drawn solid inside it.
  `claimed_at` is not used as a work start, because every lease renewal
  rewrites it; it appears only as a "current lease since" mark on in-progress
  tasks, orange when the server says the lease lapsed. Tasks claimed before
  `first_claimed_at` existed are drawn hatched rather than given a guessed
  start. Each project's sessions sit in lanes under its tasks.

  Two things are left out on purpose, and the page says how many. An open task
  nobody ever claimed has no start but its filing date, so its bar would look
  like work in flight; against a real graph that was most of the chart. And a
  session that was never ended is drawn as a start mark only, since nothing
  makes an agent end one and a bar to now would claim weeks that may never
  have been worked.

  The window is 7, 14 (default), 30 or 90 days, in the URL. Sessions are read
  with `workflow_session_list(since=...)`; the view re-reads on focus and on
  Refresh rather than every 30 seconds, since it plots elapsed time. Bars are
  SVG positioned in percentages, because the page's CSP blocks inline styles.
