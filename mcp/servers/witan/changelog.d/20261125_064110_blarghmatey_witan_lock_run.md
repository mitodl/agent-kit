### Added

- `witan lock run <name> -- <cmd>` runs a command under a host-local named
  lock, so parallel workers on one machine take turns at a shared resource
  (e.g. a repo's Docker test stack). The lock is an exclusive file lock under
  `${XDG_STATE_HOME:-~/.local/state}/witan/locks`, released by the kernel when
  its holders exit however they exit. The command inherits the lock, as with
  `flock(1)`, so killing only witan, or a command exiting while a process it
  started keeps running, leaves the lock held until that process exits. The command's exit code passes through,
  SIGINT, SIGTERM and SIGHUP are forwarded to it, and `--timeout` gives up
  after that many seconds with exit code 75 without running the command.
