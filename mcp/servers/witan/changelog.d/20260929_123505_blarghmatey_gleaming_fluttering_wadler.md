### Added

- `witan setup --force` replaces a symlink or non-directory occupying an install destination (e.g. a stale symlink under `~/.pi/agent/extensions`). Without it, `witan setup` now exits 2 with the conflicting path instead of a `FileNotFoundError` traceback.
