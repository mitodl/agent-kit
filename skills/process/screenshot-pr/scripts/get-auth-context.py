#!/usr/bin/env python3
"""Log in via APISIX/Keycloak and save a Playwright storage-state auth context.

Usage:
    python get-auth-context.py <login_url> <output_path> [--username U] [--password P]

Example:
    python get-auth-context.py http://mitxonline.odl.local:9080/login auth.json
"""

import argparse
import sys

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("login_url", help="Full /login URL behind APISIX, e.g. http://mitxonline.odl.local:9080/login")
    parser.add_argument("output", help="Path to write the auth context JSON, e.g. /tmp/auth.json")
    parser.add_argument("--username", default="admin@odl.local", help="Keycloak username (default: admin@odl.local)")
    parser.add_argument("--password", default="admin", help="Keycloak password (default: admin)")
    parser.add_argument("--timeout", type=int, default=20000, help="Timeout per step in ms (default: 20000)")
    args = parser.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        try:
            print(f"Navigating to {args.login_url} …", file=sys.stderr)
            page.goto(args.login_url, timeout=args.timeout)

            # Wait for the Keycloak username field
            page.wait_for_selector('input[name="username"]', timeout=args.timeout)
            page.fill('input[name="username"]', args.username)
            page.fill('input[name="password"]', args.password)

            # Keycloak submit button is #kc-login or input[type="submit"]
            page.click('#kc-login, input[type="submit"]')

            # Wait until we leave the Keycloak host (redirect back to the app)
            page.wait_for_load_state("networkidle", timeout=args.timeout)

            final_url = page.url
            if "kc." in final_url or "/login" in final_url:
                # Probably landed on an error page
                raise RuntimeError(f"Still on login page after submit — check credentials. Current URL: {final_url}")

            context.storage_state(path=args.output)
            print(f"Auth context saved → {args.output}", file=sys.stderr)

        except PlaywrightTimeoutError as exc:
            print(f"Timed out during login: {exc}", file=sys.stderr)
            sys.exit(1)
        except Exception as exc:
            print(f"Login failed: {exc}", file=sys.stderr)
            sys.exit(1)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
