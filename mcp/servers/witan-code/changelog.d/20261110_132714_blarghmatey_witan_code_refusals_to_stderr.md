### Fixed

- `witan-code symbols` with no detectable repo now prints its refusal to stderr and exits 1. It printed to stdout and exited 0, so a consumer parsing `--output-format json|toml|yaml` got a sentence instead of a document and could not tell the refusal from success.
- `witan-code branches --prune` against a remote deployment, a misconfigured remote (e.g. `WITAN_REMOTE_URL` without `WITAN_OIDC_ISSUER`), and a remote failure on any command (deployment unreachable, credential rejected, tool refused) print to stderr instead of stdout. All of them already exited 1. The umbrella `witan code ...` path still prints remote failures to stdout through `witan`'s own handler.
