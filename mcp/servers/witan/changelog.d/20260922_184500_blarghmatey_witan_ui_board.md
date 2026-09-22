### Added

- **The UI's Board tab: Ready, In progress, Blocked and Closed columns, with
  who holds each claim and for how long.** Ready is `task_ready`'s own result
  for the current scope, in its order, so the column cannot disagree with what
  an agent is told is ready. In progress cards show the holder and the lease
  age, measured from `claimed_at` (or `updated_at` for a legacy claim, as the
  server's lease rule does), and a card is marked "claim lapsed" exactly when
  the server's `lease_expired` says so. That includes a lapsed claim whose task
  also has an open blocker, which never appears in Ready. Blocked cards list
  each open blocker as a link with its status, including blockers in another
  repo, which is why the board reads the non-closed tasks across every repo
  and narrows the columns in the browser by the tools' own scoping rule.
  Closed shows the newest 50 by `closed_at` and appears only when the Closed
  filter is ticked.

  Every read passes `limit`, because the unscoped `task_list` reads are
  otherwise capped at 50 rows without saying so, and because `task_ready`
  sorts before it truncates, so a smaller limit would push ready tasks into
  Blocked.
