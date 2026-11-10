### Fixed

- `witan_core.omnigraph`, `witan_core.remote.oidc` and `witan_core.remote.journal` import on Windows. Their file locks go through a new `witan_core._filelock` helper (`fcntl.flock` on POSIX, unchanged; `msvcrt.locking` on Windows), and the operator-config temp dir check no longer calls `os.getuid` there. Both CLIs previously failed at import on Windows.
