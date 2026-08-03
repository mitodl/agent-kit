"""GitHub App installation tokens, so the CI indexer can clone private repos.

The indexer sweeps every repo in the deployment's managed list and clones each
one (``docker/witan-ci-index.sh``). Public repos need no credential; a private
one needs something that can read it, and the choice of *what* is a security
decision, not a plumbing one:

- A **deploy key** is scoped to one repository, so a sweep over N repos needs N
  keys, N GitHub-side registrations, and per-repo key selection in the loop.
  The key material grows with the fleet.
- A **user PAT** is one credential, but long-lived and scoped to everything its
  owner can read — far more than the indexer needs, and rotation is manual.
- A **GitHub App** issues installation tokens that expire in an hour, and its
  *installation* is itself the list of repositories it can reach. That list is
  managed by an org admin in GitHub rather than by this deployment's config, so
  a private repo's code cannot be pulled into a shared graph by a Pulumi change
  alone — two independently-controlled things have to agree first.

Hence the App. The permission it needs is exactly ``contents: read``.

WHY A TOKEN PER REPO, NOT PER SWEEP

An installation token is valid for one hour. A cold sweep — every repo's first
index, parsing each from scratch — can outlast that, and the deployed CronJob
allows three hours for it. Minting once at startup would pass every test worth
writing and then fail partway through the first real run, with the repos early
in the list indexed and the rest 401ing. Minting before each clone costs one
API call per repo, which against the cost of cloning and parsing one is
nothing.

CONFIGURATION

Three environment variables, all or none:

    WITAN_CODE_GITHUB_APP_ID              the App's id
    WITAN_CODE_GITHUB_APP_INSTALLATION_ID the installation on the org
    WITAN_CODE_GITHUB_APP_KEY_FILE        path to the App's PEM private key

A partial set is an error rather than a fall back to unauthenticated cloning:
the symptom of the latter is every private repo failing to clone while the
public ones succeed, which reads like a permissions problem on GitHub's side
rather than a missing mount here.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # httpx2 stays a runtime import inside the App path only
    import httpx2

# Module name for the usage string. Not `__spec__.name`: `__spec__` is None
# when this file is run as a script rather than with `-m`, which would turn
# the bad-argument path into an AttributeError instead of the usage message.
_MODULE = "witan_code.github_app"

__all__ = [
    "AppCredentials",
    "GitHubAppError",
    "app_jwt",
    "from_env",
    "installation_token",
]

GITHUB_API_URL = "https://api.github.com"

APP_ID_ENV_VAR = "WITAN_CODE_GITHUB_APP_ID"
INSTALLATION_ID_ENV_VAR = "WITAN_CODE_GITHUB_APP_INSTALLATION_ID"
KEY_FILE_ENV_VAR = "WITAN_CODE_GITHUB_APP_KEY_FILE"
_ENV_VARS = (APP_ID_ENV_VAR, INSTALLATION_ID_ENV_VAR, KEY_FILE_ENV_VAR)

# Not part of the all-or-none set above: unset means github.com, which is
# right for every repo indexed today. It exists so the entrypoint's App path
# can be exercised end-to-end against a stub API — the credential-helper
# plumbing is the part most likely to be subtly wrong, and it cannot be tested
# at all if the API host is a constant. A GitHub Enterprise host would use it
# for real.
API_URL_ENV_VAR = "WITAN_CODE_GITHUB_API_URL"

# GitHub rejects an App JWT whose lifetime exceeds 10 minutes. Nine leaves room
# for the clock skew backdate below without crossing that line.
_JWT_LIFETIME_SECONDS = 9 * 60
# GitHub's own recommendation: backdate `iat` to tolerate a fast local clock,
# which it would otherwise reject as issued in the future.
_JWT_BACKDATE_SECONDS = 60

_HTTP_TIMEOUT = 15
_HTTP_CREATED = 201
# Enough of an error body to identify the failure, bounded so a stray HTML
# error page does not land in the job log in full.
_ERROR_BODY_LIMIT = 500


class GitHubAppError(RuntimeError):
    """Minting an installation token failed."""


@dataclass(frozen=True)
class AppCredentials:
    """What identifies this App and proves it. ``private_key`` is PEM text."""

    app_id: str
    installation_id: str
    private_key: str

    def __repr__(self) -> str:
        # The default dataclass repr would put the PEM in any traceback that
        # happens to carry these — including ones this module raises itself.
        return (
            f"AppCredentials(app_id={self.app_id!r}, "
            f"installation_id={self.installation_id!r}, private_key=<redacted>)"
        )


def from_env(env: dict[str, str] | None = None) -> AppCredentials | None:
    """Read credentials from the environment. ``None`` when none are set.

    ``None`` means "no App configured", which is the correct state for a
    deployment whose repos are all public — the caller clones unauthenticated.
    A *partial* set raises instead: see the module docstring for why that must
    not degrade to the same thing as none.
    """
    env = os.environ if env is None else env
    present = {name: env.get(name) for name in _ENV_VARS}
    set_names = [name for name, value in present.items() if value]

    if not set_names:
        return None
    if len(set_names) != len(_ENV_VARS):
        missing = ", ".join(name for name in _ENV_VARS if not present[name])
        raise GitHubAppError(
            f"Incomplete GitHub App configuration: {', '.join(set_names)} set "
            f"but {missing} missing. Set all of {', '.join(_ENV_VARS)} or none "
            "of them — a partial set would silently clone as nobody, and every "
            "private repo would fail in a way that looks like a GitHub-side "
            "permissions problem."
        )

    key_file = Path(present[KEY_FILE_ENV_VAR])  # type: ignore[arg-type]
    try:
        # UnicodeError alongside OSError: a PEM is ASCII, so non-UTF-8 bytes
        # mean the mount is not the file this expects — a truncated or binary
        # Secret. That deserves the same explicit message as an unreadable
        # path rather than an unhandled traceback out of a CronJob.
        private_key = key_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise GitHubAppError(
            f"Cannot read the GitHub App private key at {key_file}: {exc}"
        ) from exc
    if not private_key.strip():
        raise GitHubAppError(
            f"The GitHub App private key at {key_file} is empty. A Vault-synced "
            "Secret that has not been fulfilled yet looks exactly like this."
        )

    return AppCredentials(
        app_id=present[APP_ID_ENV_VAR],  # type: ignore[arg-type]
        installation_id=present[INSTALLATION_ID_ENV_VAR],  # type: ignore[arg-type]
        private_key=private_key,
    )


def app_jwt(credentials: AppCredentials, *, now: int | None = None) -> str:
    """A short-lived RS256 JWT authenticating as the App itself.

    This authenticates as the *App*, which can do almost nothing on its own —
    its only use is exchanging itself for an installation token below.
    """
    # Imported here rather than at module scope: this module is imported by
    # the entrypoint on every sweep, including the `--check` that decides there
    # is no App at all, and neither joserfc nor httpx2 below is worth paying
    # for on that path.
    from joserfc import jwt
    from joserfc.jwk import RSAKey

    issued = (int(time.time()) if now is None else now) - _JWT_BACKDATE_SECONDS
    try:
        key = RSAKey.import_key(credentials.private_key)
    except Exception as exc:
        raise GitHubAppError(
            f"The GitHub App private key is not a usable RSA PEM: {exc}"
        ) from exc

    return jwt.encode(
        {"alg": "RS256"},
        {
            "iat": issued,
            "exp": issued + _JWT_LIFETIME_SECONDS,
            "iss": credentials.app_id,
        },
        key,
    )


def installation_token(
    credentials: AppCredentials,
    *,
    client: httpx2.Client | None = None,
    api_url: str = GITHUB_API_URL,
    now: int | None = None,
) -> str:
    """Exchange the App JWT for an installation token (valid one hour).

    ``client`` takes an ``httpx2.Client`` when a caller has one to reuse or a
    test has one to fake; otherwise one is built per call, which is what the
    once-per-repo caller wants anyway.

    A client this function created is closed before returning, and one passed
    in is left alone — the ``owns`` convention ``witan_core.remote.oidc``
    uses. The entrypoint mints one token per process, so nothing leaks there
    either way, but a long-lived caller would accumulate open connections.
    """
    import httpx2

    owns = client is None
    http = client or httpx2.Client(timeout=_HTTP_TIMEOUT)
    url = (
        f"{api_url.rstrip('/')}/app/installations/"
        f"{credentials.installation_id}/access_tokens"
    )
    try:
        response = http.post(
            url,
            headers={
                "Authorization": f"Bearer {app_jwt(credentials, now=now)}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    except httpx2.HTTPError as exc:
        raise GitHubAppError(f"Could not reach {url}: {exc}") from exc
    finally:
        if owns:
            http.close()

    if response.status_code != _HTTP_CREATED:
        raise GitHubAppError(
            f"GitHub refused an installation token for installation "
            f"{credentials.installation_id} (HTTP {response.status_code}): "
            f"{response.text[:_ERROR_BODY_LIMIT]}"
        )

    try:
        token = response.json()["token"]
    except (ValueError, KeyError, TypeError) as exc:
        raise GitHubAppError(
            f"GitHub returned no token in its response to {url}"
        ) from exc
    if not isinstance(token, str) or not token:
        raise GitHubAppError(f"GitHub returned an empty token in response to {url}")
    return token


# Exit codes, which the entrypoint branches on — see docker/witan-ci-index.sh.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_CONFIGURED = 2


def main(argv: list[str] | None = None) -> int:
    """``python -m witan_code.github_app [--check]``.

    Default: print a fresh installation token on stdout. ``--check`` resolves
    the configuration and prints nothing, so a caller can decide once whether
    to take the authenticated path without minting a token it will not use.

    Exits ``EXIT_NOT_CONFIGURED`` when no App is configured — distinct from
    ``EXIT_ERROR`` because "this deployment has only public repos" and
    "the credentials are broken" call for opposite responses from the caller.
    """
    argv = sys.argv[1:] if argv is None else argv
    check_only = argv == ["--check"]
    if argv and not check_only:
        print(f"usage: python -m {_MODULE} [--check]", file=sys.stderr)
        return EXIT_ERROR

    try:
        credentials = from_env()
        if credentials is None:
            return EXIT_NOT_CONFIGURED
        if not check_only:
            api_url = os.environ.get(API_URL_ENV_VAR) or GITHUB_API_URL
            print(installation_token(credentials, api_url=api_url))
    except GitHubAppError as exc:
        print(f"witan-code github-app: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
