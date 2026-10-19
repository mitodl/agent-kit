### Changed

- `report_install` now prints the paths in `InstallResult.removed`, so
  `witan setup` and `witan-code setup` show the duplicate Pi skill copies they
  remove ("would remove" under `--dry-run`).
