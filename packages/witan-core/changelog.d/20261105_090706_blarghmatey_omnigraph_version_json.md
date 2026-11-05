### Added

- `omnigraph_install.reported_build()` reads a binary's version, commit, HTTP
  contract and storage formats from `omnigraph version --json`, returning an
  `OmnigraphBuild`.

### Changed

- `reported_internal_schema()` and `bin/check_omnigraph_format.py` read the
  storage format and HTTP contract from `omnigraph version --json` instead of
  regex-parsing the text output. A pre-0.14 binary has no `--json`, so it now
  raises `RuntimeError` naming the stale install rather than being read.

### Removed

- `reported_internal_schema_range()`. Use `reported_build().serves`.
