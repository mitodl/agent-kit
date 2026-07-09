# Write-path content scanning

witan can scan every free-text value written to the graph — memory bodies,
task/project descriptions, session summaries, trace outcomes — for secrets
and PII, before it is embedded or persisted. Design rationale and the
alternatives considered live in [ADR 0001](adr/0001-write-path-content-scanning.md);
this doc is the operator + developer guide for using it.

Ships **enabled by default** — it's opt-out, not opt-in. Turn it off with:

```bash
export WITAN_SCAN_ENABLED=false
```

or in `config.toml`:

```toml
[scan]
enabled = false
```

By default, `OmnigraphClient.change()` scans the free-text fields of every
`insert_*`/`update_*` mutation (see `FIELD_MAP` in `witan/scan/enforce.py`) —
this covers memories, topics, workflow projects/sessions/traces, and tasks by
construction; no per-tool wiring is needed for new node types beyond adding a
`FIELD_MAP` entry.

## Config surface

All settings resolve `WITAN_SCAN_*` env > `[scan]` in `config.toml` > the
defaults below (see `ScanConfig` in `witan/config.py`).

| Setting | Env var | Default | Description |
|---|---|---|---|
| `enabled` | `WITAN_SCAN_ENABLED` | `true` | Master switch — opt-out |
| `secret_action` | `WITAN_SCAN_SECRET_ACTION` | `block` | Enforcement for `secret` findings |
| `pii_action` | `WITAN_SCAN_PII_ACTION` | `redact` | Enforcement for `pii` findings |
| `enabled_detectors` | `WITAN_SCAN_ENABLED_DETECTORS` | `[]` (all) | Explicit allowlist of detector names |
| `disabled_detectors` | `WITAN_SCAN_DISABLED_DETECTORS` | `[]` | Detector names to switch off; always wins over `enabled_detectors` |
| `plugins` | `WITAN_SCAN_PLUGINS` | `[]` | Dotted `module:Attr` paths to extra scanners (see below) |
| `allowlist` | `WITAN_SCAN_ALLOWLIST` | `[]` | Regexes tested against each finding's own matched span (`re.fullmatch`) — a hit downgrades that finding to audit-only |
| `allowlist_hashes` | `WITAN_SCAN_ALLOWLIST_HASHES` | `[]` | Salted SHA-256 digests of specific approved values — a hit downgrades to audit-only, same as `allowlist`, without the plaintext ever appearing in config |
| `allowlist_salt` | `WITAN_SCAN_ALLOWLIST_SALT` | `""` | Salt for `allowlist_hashes`. Empty means the hash allowlist is inert |
| `on_scanner_error` | `WITAN_SCAN_ON_ERROR` | `block` | `block` (fail-closed) or `warn` if a scanner itself raises |

List-shaped env vars accept a comma-separated string
(`WITAN_SCAN_DISABLED_DETECTORS=phone,high_entropy_string`); list-shaped TOML
values accept a TOML array or a bare string.

```toml
[scan]
enabled = true
secret_action = "block"
pii_action = "redact"
disabled_detectors = ["phone"]          # noisy for this org
plugins = ["acme_scanners:EmployeeIdScanner"]
```

## Enforcement modes

Every finding resolves to one of three actions — a `Finding` can also carry
an explicit `action` that overrides its category's configured default:

- **`block`** — the write is rejected with a `WriteBlocked` error before it
  reaches the store. The error names the field and detector and includes a
  secret-free preview; it never includes the matched text.
- **`redact`** — the matched span is replaced in place with
  `«redacted:<detector>»` and the write proceeds. The node is also tagged
  `scan:redacted` (via its `tags` list, where the mutation has one) so
  redacted content is discoverable later.
- **`warn`** — an audit event is emitted and the write proceeds unchanged.
  Useful for rolling out a new detector or policy change without blocking
  anyone yet.

Defaults are asymmetric on purpose: **secrets block** (a leaked credential
must never land), **PII redacts** (mask the span, keep the surrounding text
useful). If a scanner itself raises, the default is fail-closed
(`on_scanner_error = "block"`) so a broken detector can't silently let
everything through.

## Built-in detectors

Zero-dependency regex + entropy rules, each independently addressable by name
in `enabled_detectors`/`disabled_detectors`:

- **Secrets:** `aws_access_key`, `github_token`, `slack_token`,
  `google_api_key`, `private_key_block`, `jwt`, `secret_assignment` (generic
  `password=`/`api_key=`/`token=` patterns), `high_entropy_string` (Shannon
  entropy over long base64/hex-looking tokens).
- **PII:** `email`, `phone`, `us_ssn`, `credit_card` (Luhn-validated).

Run `witan scan rules` to see exactly what's active in your environment (see
below) rather than trusting this list to stay in sync — detectors can be
added, and third-party plugins add more.

## False-positive suppression (allowlisting)

Three independent mechanisms downgrade a finding to **audit-only** — the
value is written unchanged (never blocked or redacted) and the finding still
emits exactly one audit event, with `outcome = "suppressed"` and
`suppressed_by` naming which mechanism fired:

1. **Regex allowlist** (`[scan] allowlist`) — each pattern is matched with
   `re.fullmatch` against the finding's own matched span (not the whole
   field), so a pattern for one known value can't suppress an unrelated,
   longer secret that happens to contain it:

   ```toml
   [scan]
   allowlist = ["AKIA[A-Z0-9]{16}EXAMPLE"]   # a documented fixture key
   ```

2. **Inline pragma** — a trailing marker in the authored text itself:
   `witan: allow-secret` suppresses every finding in that value;
   `witan: allow-secret:<detector>` scopes it to one detector. Use this when
   an author knows a specific write is fine and wants to say so inline rather
   than editing config:

   ```
   Run with API_KEY=AKIAIOSFODNN7EXAMPLE (docs fixture) witan: allow-secret:aws_access_key
   ```

3. **Hash allowlist** (`[scan] allowlist_hashes` + `allowlist_salt`) —
   approve a specific value by its salted digest instead of its plaintext
   pattern, so the approved value never appears in config:

   ```bash
   python3 -c "import hashlib; print(hashlib.sha256(('mysalt' + 'the-approved-value').encode()).hexdigest())"
   ```

   ```toml
   [scan]
   allowlist_salt = "mysalt"
   allowlist_hashes = ["<digest from above>"]
   ```

`witan scan test` shows a `suppressed` column and a summary line so you can
validate an allowlist entry before relying on it in production.

## `witan scan` — dry-run and introspection

Validate policy or debug a false positive without writing anything:

```bash
$ witan scan test "my email is a@b.com, key AKIAIOSFODNN7EXAMPLE"  # pragma: allowlist secret gitleaks:allow
                                    Findings
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┓
┃ detector       ┃ category ┃ severity ┃ span  ┃ action ┃ preview              ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━┩
│ aws_access_key │ secret   │ high     │ 22-42 │ block  │ <aws_access_key: 20 chars> │
│ email          │ pii      │ medium   │ 12-19 │ redact │ <email: 7 chars>     │
└────────────────┴──────────┴──────────┴───────┴────────┴──────────────────────┘

Redacted preview: my email is «redacted:email», key «redacted:aws_access_key»

1 finding(s) would block this write.

$ witan scan rules
Scanning: enabled  (on_scanner_error=block)
...detector | category | mode | source...
```

`witan scan test` runs the exact same `ScannerRegistry` the write path uses
(it works even when `WITAN_SCAN_ENABLED=false`, so you can validate a policy
before turning it on). `witan scan rules` lists every active detector, its
category, its resolved enforcement mode, and its source (`built-in`,
`entry-point:<name>`, or `config:<dotted.path>`).

## Audit trail

Every finding — blocked, redacted, warned, or suppressed by an allowlist —
emits one structured log line via the standard `logging` module, on the
`witan.scan.audit` logger (`witan/scan/audit.py`). Fields: `query_name`,
`node_type`, `field`, `slug` (when the mutation has one), `detector`,
`category`, `severity`, `action`, `outcome` (`blocked` | `redacted` |
`warned` | `suppressed`), `suppressed_by` (`regex` | `pragma` | `hash`, or
`None`), and `preview` — the matched value is never included, by construction
(this is a hard invariant of `Finding.preview`, not an after-the-fact scrub).
Point your log aggregator (Loki, CloudWatch, journald) at this logger to
build dashboards or alerts on scan activity — e.g. a spike in `suppressed`
events is a signal an allowlist entry may be too broad; there is deliberately
no separate graph node or metrics sink for this yet, to avoid adding new
sensitive-adjacent state to secure and retention-manage.

## Writing a custom scanner plugin

Other organizations extend detection without forking witan. A scanner is
anything with:

```python
class MyScanner:
    name: str = "acme_employee_id"       # stable, unique detector id
    category: Literal["secret", "pii"] = "pii"

    def scan(self, text: str, field: str, node_type: str) -> list[Finding]:
        ...  # return zero or more Finding objects; never echo the match
```

`witan.scan.Scanner` is a `runtime_checkable` `Protocol` (structural typing —
no base class to inherit). See `witan/scan/models.py` for `Finding`'s exact
shape and `witan/scan/detectors.py` for worked examples (`RegexScanner`,
`EntropyScanner`).

**Never put the matched value in `Finding.preview`** — it ends up in log
lines and, for a blocked write, in the rejection error surfaced to the agent.
Use `witan.scan.masked_preview(detector, value)` to build a safe one.

Two ways to register a plugin, both read by `ScannerRegistry`:

1. **Entry point** — declare it in the plugin package's `pyproject.toml`:

   ```toml
   [project.entry-points."witan.scanners"]
   acme_employee_id = "acme_scanners:EmployeeIdScanner"
   ```

   Once the package is installed alongside witan, it's discovered
   automatically — no config change needed. This is the primary mechanism for
   a published, shareable plugin.

2. **Dotted config path** — for a scanner that isn't packaged, point
   `plugins` (or `WITAN_SCAN_PLUGINS`) at it directly:

   ```toml
   [scan]
   plugins = ["acme_scanners:EmployeeIdScanner"]
   ```

Either way, `enabled_detectors`/`disabled_detectors` then select or silence it
like any built-in rule, and a load failure (bad import path, missing
attribute, wrong shape) raises loudly when the registry is built rather than
silently starting with a detector missing.

A complete, runnable example package lives at
[`examples/example-scanner-plugin`](../examples/example-scanner-plugin) —
copy it as a starting point.

## Multi-tenant / deployed-server mode

In a shared, multi-user deployment, `WITAN_SCAN_*` env vars and client-supplied
TOML are not trustworthy — a client must not be able to weaken policy it is
itself subject to. Authoritative, admin-owned policy sourcing and per-tenant
overlays are a separate piece of work
(`tk-multi-tenant-policy-control-admin-owned-scan-pol-1338d2`), tracked
alongside the multi-user deployment project. Everything in this doc applies
as-is to local, single-user witan today.
