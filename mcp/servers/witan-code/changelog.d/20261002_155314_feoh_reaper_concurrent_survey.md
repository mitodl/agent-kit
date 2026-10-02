### Fixed

- `reap-views` ages a graph's views concurrently, `WITAN_CODE_VIEW_SURVEY_WORKERS` at a time (default 8; `1` is serial). Ageing a view is one `commit list --branch` that returns its whole reachable history, and serially production's 309-view bridge graph took 327s on its own, pushing the reaper CronJob past its 600s deadline; 8 workers take it to 50s. Each graph now also prints a `surveying views` line before its survey, so a job killed mid-sweep shows which graph it was on.
