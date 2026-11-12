### Fixed

- The optimize and cleanup throttle stamps
  (`witan-code-{optimize,cleanup}-<digest>.json`) and the context hook's
  cached bridge verdict (`codegraph-bridge-<digest>.probe`) moved from the bare
  temp dir into witan-code's private per-user state dir, where the reindex
  queue already lives (0700 under `$TMPDIR` on POSIX,
  `%LOCALAPPDATA%\witan-code\state` on Windows). Another user could create a
  predictable name in a shared `TMPDIR` first, as a file pinning a wrong
  bridge verdict or as a symlink the write would follow. If the state dir is
  refused, the hook probes the bridge uncached instead of dropping its block.
  The first Stop hook after upgrading finds neither stamp, so it runs one
  optimize and one `--cleanup-older-than 2d` cleanup that the old stamps might
  have deferred (the cleanup by up to its 7-day window).
