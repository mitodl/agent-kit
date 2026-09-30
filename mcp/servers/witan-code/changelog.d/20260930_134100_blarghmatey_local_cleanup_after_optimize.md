### Fixed

- `witan-code checkpoint`'s background `optimize` now also runs `cleanup --older-than 30d` at most once per `WITAN_CODE_CLEANUP_INTERVAL` (default weekly; `0` disables). On omnigraph 0.11 `optimize` no longer reclaims the storage of branches deleted by `branches --prune` or `reap-views --apply`, and nothing ran `cleanup` unless done by hand. `witan-code optimize` gains `--cleanup-older-than` to run both in one process.
