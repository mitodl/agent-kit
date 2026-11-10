### Fixed

- A full-text index that needs rebuilding is classified as
  `FULL_TEXT_REBUILD_REQUIRED` from 0.15's message too. 0.15 names the remedy
  `omnigraph index rebuild --kind full-text` (upstream #1150), which neither
  the CLI nor the HTTP prose markers matched, so on the CLI path the refusal
  was classified as a generic fatal error.
