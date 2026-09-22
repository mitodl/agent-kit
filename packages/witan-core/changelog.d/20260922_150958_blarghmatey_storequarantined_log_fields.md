### Added

- **`Refusal.log_safe_attributes`: a refusal can log an identifier without
  logging its message.** `log_safe_message` is all-or-nothing on free text, so
  `StoreQuarantined`, withheld because its raise site appends the transport's
  raw error, also dropped the sidecar operation id an operator needs to
  quarantine the right file. A type now declares `{field: attribute}` pairs,
  and a failed `mcp.tool_call` line carries each non-`None` one as its own
  field. `StoreQuarantined` declares `sidecar_operation_id`, so a quarantine's
  line reads `error_withheld: true, sidecar_operation_id: <id>` with none of the
  prose. A field name the line or the logging chain already uses (`tool`,
  `error_type`, `exc_info`, `trace_id`, ...) or one with a leading underscore
  is a `TypeError` when the class is defined, and the audit tables cover the
  declarations as they cover the opt-ins.

### Changed

- **`sidecar_operation_id` parses only an identifier-shaped id.** The first
  quoted id must be `[0-9A-Za-z_-]{1,64}` or it is not parsed, because it now
  reaches the log and the text around it can be a non-JSON HTTP body passed
  through verbatim. An unparsed id still leaves a `StoreQuarantined`, only
  without a named sidecar, as before.
