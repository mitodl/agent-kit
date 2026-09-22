### Added

- **`Refusal.log_safe_message`: a refusal must opt in before its message is
  logged.** Off by default. The assumption it replaces was that a refusal's
  message is safe because it is written for the caller, and that does not hold:
  `witan_code.ingest.IngestRefused` is raised from `mutate_many` as
  `f"Every step needs a non-empty {field!r}; got {value!r}."` over the caller's
  own step. Set it `True` only where the message is built from identifiers (a
  graph, a slug, an actor id, a count) or is masked by that type's own
  contract, as `scan.enforce.WriteBlocked` documents.

  Five of the nine opted in. The four that did not are `IngestRefused`, whose
  message is the caller's own step value, and `WriteIndeterminate`,
  `AdmissionCapExceeded` and `StoreQuarantined`, whose raise sites all append
  `\n{err.strip()}` -- and `err` is whatever the transport produced, a non-JSON
  HTTP body passed through verbatim or the CLI subprocess's whole stderr.
  `StoreQuarantined` keeps the sidecar operation id an operator needs on its
  `operation_id` attribute, so that survives as a structured value even though
  the prose does not reach the log.

### Changed

- **A failed `mcp.tool_call` line now says why it failed**, carrying
  `error_type`, `refused`, and `error` where the message is safe to log.
  fastmcp logs a `FastMCPError` as `Error calling tool '<name>'` with
  `exc_info=False` and never renders `str(exc)`, so a refusal reached the log
  with no text at all: Production spent two days showing `code_store_views`
  erroring on 27% of calls with the reason (`ClusterGraphMissing`, a repo that
  has no cluster graph) readable only in the Tempo span's status message.
  `error_type` alone answers that and is always present.

  `refused` is log-only and the metric is untouched --
  `witan_tool_calls_total` still counts a refusal under `outcome="error"`,
  because the error-ratio alert's headline case, a quarantined graph answering
  every request with "not served", is itself a refusal.

  A VALIDATION failure never carries its message, on either arm: the line gets
  `error_withheld: true` plus `error_count` and `error_types`, allowlisted
  against pydantic's own `ErrorType` literals. Three separate routes put
  somebody's data in that message, each confirmed by running it. `msg` is not
  covered by `include_input=False`, and for the BUILTIN codes `value_error` and
  `assertion_error` pydantic renders the raised exception's own text, so a
  validator doing `raise ValueError(f"rejected {value}")` yields
  `"Value error, rejected <the value>"` -- allowlisting the code does not help,
  because those codes are on the allowlist. A `PydanticCustomError` controls
  both its `type` and its `msg`. And `loc` names the offending key for
  `extra_forbidden` or a dict-key failure.

  This covers a model failing inside a tool BODY as well as bad arguments. An
  earlier revision kept the body message on the reasoning that such a failure
  is always one of our own models; that was wrong.
  `witan_code.config._Target` is a `BaseModel` built straight from TOML by
  `_parse_targets` with no `except ValidationError` in that module, and
  `cfg_module.load()` runs on tool paths.

  A PLAIN exception keeps its message, because fastmcp's `except Exception` arm
  has already written the same text to the log in full, with a traceback,
  through `logger.exception`. That reasoning does not extend to a
  `FastMCPError` which is not a `Refusal` -- it takes the `exc_info=False` arm
  like a refusal does -- but the only one reaching here today is fastmcp's own
  `NotFoundError("Unknown tool: ...")`, whose payload is already the `tool`
  field.

  A failure that breaks the describer itself logs `error_undescribable: true`,
  which is a different thing from a message deliberately withheld.
