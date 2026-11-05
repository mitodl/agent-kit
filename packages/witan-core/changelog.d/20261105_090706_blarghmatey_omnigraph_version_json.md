### Added

- `omnigraph_install.reported_build()` reads a binary's version, commit, HTTP
  contract and storage formats from `omnigraph version --json`, returning an
  `OmnigraphBuild`. A pre-0.14 binary has no `--json`, so it raises
  `RuntimeError` naming the stale install rather than being read.

### Changed

- `bin/check_omnigraph_format.py` reads the storage format and HTTP contract
  from `omnigraph version --json` instead of regex-parsing the text output.

### Removed

- `reported_internal_schema()` and `reported_internal_schema_range()`. Use
  `reported_build().writes` and `reported_build().serves`.
