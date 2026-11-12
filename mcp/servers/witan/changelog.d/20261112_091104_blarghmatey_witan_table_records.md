### Changed

- Under `--output-format json|toml|yaml`, the table commands print the records
  behind the table instead of its display cells, still inside the
  `{title, rows}` envelope. This breaks scripts that parse the old shape.
  Empty values are `null` in JSON and YAML and omitted in TOML, where they
  used to be `""`, and every row carries the fields the table does not show.
  The fields whose type or name changes:
  - `tasks`: `repo` is the full URI (`null` when unscoped) rather than
    `org/name`; `blocked_by` is a list (`null` when none) rather than a
    comma-joined string. Rows for `witan tasks <query>` come from
    `task_search` and carry no `blocked_by`, as before.
  - `projects`: `repos` is a list of full URIs rather than a comma-joined
    string of `org/name`.
  - `traces`: `repos` likewise; `sessions` is `session_count`, and `mined`
    (`"2p/1l"`) is the `patterns_slug` and `lessons_slug` lists (`null`
    when empty).
  - `memory`: `repo` is `null` rather than `""` for an unscoped memory. A
    listing outside a repo with no `--all-repos` gets `memory_list`'s slim
    rows, which carry only `slug`, `kind`, `title` and `tags`.
  - `target list`: `cur` (`"*"`) is a boolean `current`, and `matches` is the
    `match_orgs`, `match_repos`, `match_hosts` and `match_paths` lists. Each
    row carries every key in the target's table, including witan-code's
    (`code_server`, `code_transport`, `index_role`, ...), except `token` and
    `code_token`.
  - `scan test`: `span` (`"4-24"`) is the integers `start` and `end`, and
    `suppressed` is `null` rather than `""` when a finding is not allowlisted.
  - `journal list`: `args` is an object rather than a JSON-encoded string, and
    each row adds `context`.

  `scan rules` and `journal replay` already printed their data and are
  unchanged. `witan code repos|symbols|stitch` still print display cells.
