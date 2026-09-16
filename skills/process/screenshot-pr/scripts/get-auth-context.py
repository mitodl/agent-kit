#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.61.0"]
# ///
"""Log in through a gateway/IdP and save a Playwright storage-state context.

Usage:
    uv run get-auth-context.py <login_url> <output_path> [--username U]
        [--password P | --password-env VAR] --verify URL...

Example:
    uv run get-auth-context.py https://app.example.test/login /tmp/auth.json \
        --username admin@odl.local --password localdev123 \
        --verify https://app.example.test/api/v0/users/me/

The form selectors default to Keycloak's, which is what APISIX fronts on the
ODL stacks. A different IdP -- a hosted Auth0 or Okta page, a plain Django
login -- needs --username-selector, --password-selector and --submit-selector.

Nothing is written unless every --verify URL answers as the user who signed
in. A context that authenticates only some of the sessions a page depends on
is the failure worth guarding: it does not error, it renders the anonymous
page, which on most apps is indistinguishable by eye from a valid signed-in
one. So is a context minted under the wrong credentials, which renders a
perfectly plausible page belonging to somebody else.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# Keycloak's, and generic enough to hit a plain login form by luck. Anything
# else is passed in; see the module docstring.
SUBMIT = '#kc-login, input[type="submit"], button[type="submit"]'
PASSWORD = 'input[name="password"]'
USERNAME = 'input[name="username"]'


def log(message):
    print(message, file=sys.stderr, flush=True)


def sign_in(page, login_url, username, password, timeout, selectors):
    """Fill the login form, whether it is one page or two.

    Newer Keycloak themes ask for the username first and only render the
    password field after that form is submitted, so the password field cannot
    be filled before the first submit.
    """
    username_sel, password_sel, submit_sel = selectors
    page.goto(login_url, timeout=timeout)
    page.wait_for_selector(username_sel, timeout=timeout)
    page.fill(username_sel, username)

    # A short bounded wait, not a count(): on a one-page form whose password
    # field renders a tick late, a snapshot count takes the two-page branch,
    # submits the username alone and lands on "Invalid username or password".
    try:
        page.wait_for_selector(password_sel, timeout=2000)
        one_page = True
    except PlaywrightTimeoutError:
        one_page = False

    if one_page:
        page.fill(password_sel, password)
        page.click(submit_sel)
    else:
        page.click(submit_sel)
        page.wait_for_selector(password_sel, timeout=timeout)
        page.fill(password_sel, password)
        page.click(submit_sel)

    page.wait_for_load_state("domcontentloaded", timeout=timeout)


def signed_in_as(page, url, timeout):
    """Who `url` answers as, or None when the response is anonymous.

    Reads JSON out of the response body, tolerating DRF's browsable-API
    rendering, which wraps the payload in markup rather than serving it raw.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    text = page.inner_text("body")
    start = text.find("{")
    if start == -1:
        return None
    try:
        # raw_decode, not a regex: a greedy match runs past the payload into
        # whatever markup the browsable API renders after it, and a good
        # session then reads as anonymous.
        body, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    if body.get("is_authenticated") is False or body.get("is_anonymous") is True:
        return None
    return body.get("email") or body.get("username") or None


def is_same_user(who, username):
    """Whether `who` names the account we signed in as.

    Apps answer with whichever of email or username they consider canonical,
    so an email in and a bare username out is a match, not a mismatch. Only
    one side may drop a domain, though: `admin@odl.local` and
    `admin@example.test` share a local part and are different accounts, and a
    check that accepts one for the other cannot catch the wrong-realm context
    it exists to catch.
    """
    if not who:
        return False
    who, username = who.casefold(), username.casefold()
    if who == username:
        return True
    if "@" not in who:
        return who == username.split("@")[0]
    if "@" not in username:
        return who.split("@")[0] == username
    return False


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("login_url", help="Full /login URL behind the gateway")
    parser.add_argument("output", help="Path to write the storage-state JSON")
    parser.add_argument("--username", default="admin@odl.local")
    parser.add_argument("--password", default="localdev123")
    parser.add_argument(
        "--password-env",
        metavar="VAR",
        help="Read the password from this environment variable instead of "
        "--password. Prefer it when a script calls this one: a command line "
        "is visible in `ps` and survives in tracebacks.",
    )
    parser.add_argument(
        "--verify",
        action="append",
        required=True,
        metavar="URL",
        help="A URL answering with the current user. Repeat once per session "
        "the captured pages depend on; an app proxying another service's API "
        "through its own host holds a separate session per upstream, and "
        "logging in mints only the first. Visiting the second URL is what "
        "mints its session, so verifying is also how you get it.",
    )
    parser.add_argument(
        "--timeout", type=int, default=30000, help="Per-step timeout in ms"
    )
    parser.add_argument("--username-selector", default=USERNAME)
    parser.add_argument("--password-selector", default=PASSWORD)
    parser.add_argument(
        "--submit-selector",
        default=SUBMIT,
        help="Defaults suit Keycloak; override all three for another IdP",
    )
    args = parser.parse_args()

    # Before the attempt, not after a failure: otherwise a run that cannot
    # log in leaves the previous context sitting at the advertised path, and
    # the next capture consumes it and renders every page anonymous.
    Path(args.output).unlink(missing_ok=True)

    password = args.password
    if args.password_env:
        password = os.environ.get(args.password_env)
        if not password:
            raise SystemExit(f"${args.password_env} is unset or empty")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        try:
            log(f"Logging in as {args.username} …")
            sign_in(
                page,
                args.login_url,
                args.username,
                password,
                args.timeout,
                (
                    args.username_selector,
                    args.password_selector,
                    args.submit_selector,
                ),
            )

            for url in args.verify:
                who = signed_in_as(page, url, args.timeout)
                if not is_same_user(who, args.username):
                    answered = who or "an anonymous user"
                    raise RuntimeError(
                        f"{url} answered as {answered}, expected "
                        f"{args.username}; no context written "
                        f"(landed on {page.url})"
                    )
                log(f"  session verified at {url}: {who}")

            context.storage_state(path=args.output)
            log(f"  auth context → {args.output}")
        except PlaywrightTimeoutError as exc:
            log(f"Timed out at {page.url}: {exc}")
            sys.exit(1)
        except Exception as exc:  # noqa: BLE001 - the caller only needs the reason
            log(f"Login failed: {exc}")
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
