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
        --username admin@ol.test --password localdev123 \
        --verify https://app.example.test/api/v0/users/me/

Nothing is written unless every --verify URL answers as the user who signed
in: a context missing a session, or signed in as someone else, doesn't error,
it renders a plausible page that is wrong.

Form selectors default to Keycloak's; pass the three --*-selector flags for
another IdP.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# Keycloak's, and generic enough for many plain login forms.
SUBMIT = '#kc-login, input[type="submit"], button[type="submit"]'
PASSWORD = 'input[name="password"]'
USERNAME = 'input[name="username"]'


def log(message):
    print(message, file=sys.stderr, flush=True)


def sign_in(page, login_url, username, password, selectors):
    """Fill the login form, whether it is one page or username-first."""
    username_sel, password_sel, submit_sel = selectors
    response = page.goto(login_url)
    # Checked, or a wrong login URL surfaces as a selector timeout.
    if response and not response.ok:
        raise RuntimeError(f"{login_url} answered HTTP {response.status}")
    page.wait_for_selector(username_sel)
    page.fill(username_sel, username)

    # A short wait, not count(): a password field that renders a tick late
    # would otherwise send us down the two-page branch.
    try:
        page.wait_for_selector(password_sel, timeout=2000)
        one_page = True
    except PlaywrightTimeoutError:
        one_page = False

    if not one_page:
        page.click(submit_sel)
        page.wait_for_selector(password_sel)
    page.fill(password_sel, password)
    form_url = page.url
    page.click(submit_sel)

    # Until the form page is gone and the redirect chain has loaded. A load-state
    # wait can return on the form page itself, and the --verify navigation
    # that follows then cuts off the app's login callback before it sets the
    # session.
    page.wait_for_url(lambda url: url != form_url)


def signed_in_as(page, url):
    """Who `url` answers as, or None if anonymous. Tolerates DRF's HTML view."""
    page.goto(url, wait_until="domcontentloaded")
    text = page.inner_text("body")
    start = text.find("{")
    if start == -1:
        return None
    try:
        # raw_decode stops at the end of the object; markup may follow it.
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

    An app may answer with a bare username for an email login, so one side may
    drop its domain -- but not both: `admin@ol.test` and `admin@example.test`
    are different accounts.
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
    parser.add_argument("--username", default="admin@ol.test")
    parser.add_argument("--password", default="localdev123")
    parser.add_argument(
        "--password-env",
        metavar="VAR",
        help="Read the password from this environment variable; prefer it "
        "when scripting, since argv is visible in `ps`.",
    )
    parser.add_argument(
        "--verify",
        action="append",
        required=True,
        metavar="URL",
        help="A URL answering with the current user. Repeat once per session "
        "the pages depend on; visiting each one also mints its session.",
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

    # Up front, so a failed login can't leave an old context for the next
    # capture to use.
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
        page.set_default_timeout(args.timeout)
        try:
            log(f"Logging in as {args.username} …")
            sign_in(
                page,
                args.login_url,
                args.username,
                password,
                (
                    args.username_selector,
                    args.password_selector,
                    args.submit_selector,
                ),
            )
            # Before --verify navigates away: a rejected password or an IdP
            # step such as a required profile update shows up here.
            landed = page.url

            for url in args.verify:
                who = signed_in_as(page, url)
                if not is_same_user(who, args.username):
                    answered = who or "an anonymous user"
                    raise RuntimeError(
                        f"{url} answered as {answered}, expected "
                        f"{args.username}; no context written "
                        f"(the login landed on {landed})"
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
