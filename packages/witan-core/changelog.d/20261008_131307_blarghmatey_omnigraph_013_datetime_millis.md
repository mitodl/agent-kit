### Fixed

- `now_iso()` emits millisecond precision. omnigraph 0.12 and later refuse a
  `DateTime` with non-zero fractional digits past the third, which every
  timestamp witan wrote carried. 0.11 already truncated them on write, so
  stored values do not change.
