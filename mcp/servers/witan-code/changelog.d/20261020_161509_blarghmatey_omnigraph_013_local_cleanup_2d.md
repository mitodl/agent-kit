### Changed

- The cleanup that rides a background optimize now keeps 2 days of versions
  instead of 30, matching the deployed nightly cleanup. Nothing reads older
  history, and on omnigraph 0.13 a cleanup's cost grows with every version
  still present ([ModernRelay/omnigraph#1095](https://github.com/ModernRelay/omnigraph/issues/1095)).
- `witan-code cleanup --yes` with neither `--keep` nor `--older-than` now keeps 2 days
  as well, since it shares the default. Pass `--older-than 30d` to keep the
  old window.
