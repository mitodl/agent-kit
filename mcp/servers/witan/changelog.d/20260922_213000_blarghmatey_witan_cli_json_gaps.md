### Added

- **`witan task <slug>` has a structured mode, and a `show` alias.** Under
  `--output-format json|toml|yaml` it prints `task_get`'s record whole (every
  field, comments, `blocks`, `children`, `branches`). It used to be
  `console.print` end to end, so the flag had nothing to act on. TOML omits a
  null field rather than blanking it, so an unset `closed_at` does not parse
  as present.
- `witan project tasks` and `witan session list` honour `--output-format`,
  printing the tool's rows. `project tasks --detail` adds each row's
  `dependents`.

### Fixed

- **An empty list under `--output-format` prints `rows: []`, not a
  sentence.** `tasks`, `projects`, `memory`, `traces`, `scan test`, `scan
  rules` and `target list` each returned early on an empty result with prose
  like `No tasks.` on stdout and exit 0, before the format was consulted, so
  every JSON consumer threw on an empty list. The empty message is now a
  `render_table` argument, and the `txt` view is unchanged. The mounted
  `witan code` tables have the same bug and are not covered here.
- `witan project status` honours the global `--output-format`. Its own
  `--json` flag used to win and the global flag had no effect; `--json` is
  now shorthand for `--output-format json`, and the global flag wins when
  both are given.
- Under a structured format, stdout holds one document and nothing else. The
  notices `scan test`, `scan rules` and `target list` print alongside their
  table go to stderr there. stderr also carries log lines on success, so
  consumers should branch on the exit code.
- `witan task <slug>`, `witan project <slug>` and `witan project
  status|tasks` on a missing slug exit 1 with the error on stderr. `task
  <slug>` and `project <slug>` used to print it on stdout and exit 0. The
  `target list` config-parse error also moves to stderr.
