### Fixed

- The `Stop` hook's background `witan optimize` now also runs `cleanup --older-than 30d` at most once per `WITAN_CLEANUP_INTERVAL` (default weekly; `0` disables). On omnigraph 0.11 `optimize` no longer reclaims deleted branches' storage and only `cleanup` does, so a local store otherwise grew without bound. `witan optimize` gains `--cleanup-older-than` to run both in one process.
