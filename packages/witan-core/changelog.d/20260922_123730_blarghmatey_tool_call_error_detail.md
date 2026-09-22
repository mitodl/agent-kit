### Changed

- **A failed `mcp.tool_call` line now says why it failed**, carrying
  `error_type`, `error` (the message, truncated at 500 characters) and
  `refused`. fastmcp logs a `FastMCPError` as `Error calling tool '<name>'`
  with `exc_info=False` and never renders `str(exc)`, so a refusal reached the
  log with no text at all: Production spent two days showing `code_store_views`
  erroring on 27% of calls with the reason (`ClusterGraphMissing`, a repo that
  has no cluster graph) readable only in the Tempo span's status message.
  `refused` is log-only and the metric is untouched --
  `witan_tool_calls_total` still counts a refusal under `outcome="error"`,
  because the error-ratio alert's headline case, a quarantined graph answering
  every request with "not served", is itself a refusal.

  Two limits are deliberate. A BAD ARGUMENT is summarised, never quoted: the
  line carries `error_withheld: true` plus `error_count` and `error_types` in
  place of the message. pydantic renders a rejected field as
  `input_value=<what the caller sent>`, and fastmcp's `ValidationError` is
  built from that string, so on that arm the message is the caller's data by
  construction, and a structured parameter puts a whole payload in it.
  `error_types` is allowlisted against pydantic's own `ErrorType` literals,
  anything else becoming `custom_error`, because a validator may raise
  `PydanticCustomError` with a `type` built from the value it just rejected --
  the same substitution fastmcp makes, and for the same reason. The summary is
  computed here rather than left to fastmcp's equivalent log line, because the
  `fastmcp` logger does not propagate and keeps its own handler, so its version
  lands beside our JSON as unparsed text instead of a queryable field.

  A model failing inside a tool BODY arrives as a bare
  `pydantic.ValidationError` and KEEPS its message, because in these servers
  that is a bug in one of our own models rather than a bad call: neither server
  parses external data through pydantic (no `model_validate`, `TypeAdapter` or
  `parse_obj` in either), witan-core defines no models at all, and the three
  that exist are built from detector names, `re` match offsets, enum members
  and a `masked_preview` that carries no character of the value it describes.
  The message is rendered from `errors(include_input=False)`, so it names the
  model and the fields that failed and never the values. Parsing anything
  external through pydantic inside a tool body would invalidate that reasoning;
  `_validation_fields` says so at the point where it would matter.

  And `error_type` is the real exception class only for a `FastMCPError`:
  anything else is re-raised as `ToolError` by `FastMCP.call_tool` before the
  middleware sees it, so the class is lost and only the message survives. That
  message can contain caller arguments, since a tool body is free to
  interpolate them (witan's `workflow_trace_mine` does). It is logged anyway
  because fastmcp's generic arm already prints the same message with a full
  traceback through `logger.exception`, so the field restates what the pod log
  holds rather than adding to it.

  A failure that breaks the describer itself logs `error_undescribable: true`,
  which is a different thing from a message deliberately withheld.
