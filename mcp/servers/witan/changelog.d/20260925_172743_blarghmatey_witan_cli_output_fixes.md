### Added

- **`witan projects --status all` lists every status.** The server already read
  `status=None` as every status, but the CLI had no way to send it: `--status ''`
  reached the server as an empty string and failed its `Literal` validation.
  `--status` now takes `all` and maps it to `None`.

### Fixed

- **Structured output no longer carries Rich-escaped titles.** `witan tasks` and
  `witan projects` escaped the query in their table title before handing it to
  `render_table`, so `witan --output-format json tasks '[wip] grafana'` printed a
  title containing `\[wip]`. `render_table` now takes a plain title and escapes
  it itself, in `txt` mode only. That also fixes `witan memory QUERY`, whose
  title was never escaped and lost any bracketed text in `txt` mode.
