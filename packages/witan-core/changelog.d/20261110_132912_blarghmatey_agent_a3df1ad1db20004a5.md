### Fixed

- `witan_core.omnigraph`, `witan_core.remote.oidc` and `witan_core.remote.journal` import on Windows. Their file locks go through a new `witan_core._filelock` helper (`fcntl.flock` on POSIX, unchanged; `msvcrt.locking` on Windows), and the operator-config temp dir check no longer calls `os.getuid` there. Both CLIs previously failed at import on Windows.
- A `file:///C:/...` store URI resolves to `C:\...` on Windows instead of a path under the current drive's root.

### Changed

- `acquire_store_flock` returns the locked file descriptor (`int`) instead of an open file object. Callers that only pass it back to `release_store_flock` are unaffected.
- On Windows the token-cache write retries `os.replace` for up to two seconds, because Windows refuses to replace a file another process has open for reading.
