### Added

- `witan lock run <name> -- <cmd>` runs a command under a host-local named
  lock, so parallel workers on one machine take turns at a shared resource
  (e.g. a repo's Docker test stack). The lock is an exclusive file lock under
  `${XDG_STATE_HOME:-~/.local/state}/witan/locks`, released by the kernel when
  its holders exit however they exit. On POSIX the command inherits the lock,
  as with `flock(1)`, so killing only witan, or a command exiting while a
  process it started keeps running, leaves the lock held until that process
  exits. On Windows the command does not inherit it, so killing only witan
  frees the lock while the command may still be running. The command's exit
  code passes through, and SIGINT, SIGTERM and SIGHUP are forwarded to it,
  except that while witan is in the terminal's foreground process group a
  SIGINT is taken as a Ctrl-C the command already got and is not forwarded, so
  a runner should stop it with SIGTERM. `--timeout` gives up after that many
  seconds with exit code 75 without running the command.
