### Changed

- An omnigraph 0.14 error is classified from its `code` and
  `disposition` (`classify_disposition`) instead of its status and message
  markers, on the pooled transport and for a served JSON error from the CLI.
  The markers stay for a body without a disposition (a 0.13 server or a
  relay), and for the two conditions no code names: a graph that failed to
  open and a drifted dataset. `Outcome` carries the parsed `Disposition`, and
  `error_disposition` replaces `disposition_effects`.
- Group commit replays a failed group one write at a time when a 0.14
  server's disposition says `effects: none`, or `unknown` under
  `bad_request` or `payload_too_large`. 0.14 reports `unknown` for a refusal
  inside a write such as a missing edge endpoint, which upstream's mutation
  path raises before its only publication; a write it could not settle
  answers `500 outcome_unknown` and is never replayed.

### Fixed

- A 0.14 `recover` refusal for a full-text index that has to be built first
  (`full_text_index_required`) is terminal. As a 409 it was retried to
  exhaustion.
