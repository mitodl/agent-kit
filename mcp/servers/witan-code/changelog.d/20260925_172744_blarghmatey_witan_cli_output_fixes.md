### Fixed

- **`witan code` listings emit `rows: []` on an empty result under
  `--output-format`.** `symbols`, `stitch`, `stitch --unresolved` and `repos`
  each returned early with a Rich sentence on stdout before the format was
  consulted, so an empty result was not JSON/TOML/YAML. `_render_table` now
  takes the empty message and prints it in `txt` mode only. `branches` never
  consulted the format at all and now dumps `{repo, views, error}` rows, with
  `--prune`'s progress lines on stderr under a structured format; the `repos`
  unreadable-store warning moves to stderr there too.
- **Table titles and cells are escaped for Rich.** A symbol, path or title
  containing `[...]` was read as markup and lost from the rendered table.
