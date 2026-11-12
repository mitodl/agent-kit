### Added

- `witan_core.timeutil.parse_timestamp` parses an ISO-8601 timestamp into an
  aware datetime, reading a naive one (what omnigraph returns for a
  `DateTime`) as UTC and keeping an offset that is present. It raises on a
  value that won't parse, so each caller decides what that means. witan
  had eight copies of this parse; they now all call this one.
