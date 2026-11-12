### Fixed

- The optimize and cleanup throttle stamps
  (`witan-code-{optimize,cleanup}-<digest>.json`) moved from the bare temp dir
  into witan-code's private per-user state dir, where the reindex queue already
  lives (0700 under `$TMPDIR` on POSIX, `%LOCALAPPDATA%\witan-code\state` on
  Windows). A predictable name in a shared `TMPDIR` could be created first by
  another user. The first Stop hook after upgrading finds neither stamp, so it
  runs one optimize and one `--cleanup-older-than 2d` cleanup that the old
  stamps might have deferred (the cleanup by up to its 7-day window).
