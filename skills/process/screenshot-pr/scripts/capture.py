#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.61.0"]
# ///
"""Template: a re-runnable, self-checking screenshot capture for a PR.

Copy this and get-auth-context.py (which it calls) next to the work it
documents, fill in the marked block, and run it:

    uv run capture.py                           # everything
    uv run capture.py --only hero hero-tablet   # re-shoot a subset
    uv run capture.py --list                    # the plan, no browser

Every shot asserts on page text, so a gateway 5xx, an expired login, a stale
build or the wrong fixture fails the run instead of saving a plausible PNG.

Each Playwright version pins its own Chromium revision. On a new machine,
install this one once:

    uv run --with "playwright==1.61.0" playwright install chromium
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect as expect_locator
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
AUTH_SCRIPT = HERE / "get-auth-context.py"

# --- fill these in ------------------------------------------------------- #

BASE = "https://app.example.test"
LOGIN_URL = f"{BASE}/login"
# URLs answering with the current user; at least one. List one per session the
# pages depend on -- an app proxying another service's API holds a session per
# upstream, and a missing one renders as signed out rather than erroring.
VERIFY_URLS = [f"{BASE}/api/v0/users/me/"]

# Key -> (username, password), the local-dev defaults. SHOTS refer to the keys;
# user=None captures signed out.
USERS = {
    "admin": ("admin@odl.local", "localdev123"),
    "learner": ("learner@odl.local", "localdev123"),
}

# One flat directory, ready to drag into a GitHub comment.
OUT = HERE / "shots"

# The region under review: both the crop and the scope of every presence
# check, so a wrong value crops to the wrong element and still passes. Look at
# the page before choosing it.
TARGET = "main"
# Popovers, tooltips and modals portalled outside TARGET. Broad on purpose;
# `overlay_expect` picks the right one.
OVERLAY = "[data-popper-placement], [role='dialog']"

DESKTOP = (1440, 1100)
TABLET = (768, 1024)
MOBILE = (390, 844)

# ------------------------------------------------------------------------- #

ATTEMPTS = 3
RETRY_WAIT_SECONDS = 60
TIMEOUT_MS = 30000
# Let late layout shift land before the shutter.
SETTLE_MS = 1500
# Passes the password to get-auth-context.py; argv is visible in `ps`.
PASSWORD_ENV = "CAPTURE_PASSWORD"


@dataclass
class Shot:
    name: str
    url: str
    #: A key of USERS, or None to capture this shot signed out.
    user: str | None
    #: Text that must appear inside TARGET, before and after the settle, as
    #: rendered (CSS-uppercased copy is matched uppercase). Required: a shot
    #: that asserts nothing cannot fail.
    expect: list[str]
    #: Text that must not appear anywhere on the page, for telling apart
    #: states that render alike ("no data" vs "logged out"). Whole page, not
    #: TARGET, because the giveaway is usually a header "Sign in".
    absent: list[str] = field(default_factory=list)
    viewport: tuple[int, int] = DESKTOP
    #: Selectors clicked in order before the assertions, so `expect` can name
    #: what the click revealed.
    click: list[str] = field(default_factory=list)
    #: Scroll this into view first. Not needed for framing -- TARGET is shot at
    #: full height regardless. For lazy content, and for `opens_overlay` shots,
    #: whose crop is bounded by the viewport.
    scroll_to: str = ""
    #: Click this, then crop to it and whatever overlay it opened.
    opens_overlay: str = ""
    #: Text the overlay must show; also how it is picked out of OVERLAY's
    #: matches. Required with `opens_overlay`.
    overlay_expect: list[str] = field(default_factory=list)

    def __post_init__(self):
        for label in ("expect", "absent", "overlay_expect", "click"):
            value = getattr(self, label)
            if isinstance(value, str):
                # Iterating a string asserts "W", then "e", then "l": always
                # true.
                raise TypeError(
                    f"{self.name}: {label} must be a list of strings, not a string"
                )
            if any(not isinstance(item, str) or not item.strip() for item in value):
                # Every page contains "", and " " normalizes to it.
                raise ValueError(
                    f"{self.name}: every {label} entry must be a non-empty string"
                )
        if not self.expect:
            raise ValueError(f"{self.name}: expect must name at least one string")
        if self.user is not None and self.user not in USERS:
            raise ValueError(f"{self.name}: unknown user {self.user!r}")
        if bool(self.opens_overlay) != bool(self.overlay_expect):
            raise ValueError(
                f"{self.name}: opens_overlay and overlay_expect go together; "
                f"an overlay shot that asserts nothing crops to whatever "
                f"{OVERLAY!r} matched first"
            )


SHOTS = [
    Shot("hero", f"{BASE}/dashboard", "admin", ["Welcome back"], ["Sign in"]),
    Shot(
        "hero-tablet", f"{BASE}/dashboard", "admin", ["Welcome back"], viewport=TABLET
    ),
    Shot(
        "empty-state",
        f"{BASE}/dashboard",
        "learner",
        ["Nothing here yet"],
        ["Welcome back"],
    ),
    Shot(
        "help-popover",
        f"{BASE}/dashboard",
        "admin",
        ["Welcome back"],
        opens_overlay="button[aria-label='About this number']",
        overlay_expect=["Counts every session since signup"],
    ),
    Shot(
        "runs-expanded",
        f"{BASE}/dashboard",
        "admin",
        ["Starts 3 June"],
        click=["button:has-text('Upcoming runs')"],
    ),
    # Signed out. `expect` is scoped to TARGET, so not the header's "Sign in".
    Shot("signed-out", f"{BASE}/dashboard", None, ["Sign in to continue"]),
]


class Transient(Exception):
    """The app has not answered yet, so the next attempt may find it up."""


def log(message):
    print(message, file=sys.stderr, flush=True)


def mint_auth(user_key, directory):
    """Log in as `user_key` and return the storage-state path.

    Fresh every run: an expired context doesn't error, it renders every page
    signed out. `directory` is temporary because the file holds live cookies.
    """
    username, password = USERS[user_key]
    path = Path(directory) / f"auth-{user_key}.json"
    command = [
        sys.executable,
        str(AUTH_SCRIPT),
        LOGIN_URL,
        str(path),
        "--username",
        username,
        "--password-env",
        PASSWORD_ENV,
    ]
    for url in VERIFY_URLS:
        command += ["--verify", url]
    try:
        subprocess.run(command, check=True, env={**os.environ, PASSWORD_ENV: password})
    except subprocess.CalledProcessError:
        raise SystemExit(f"could not sign in as {username} ({user_key})") from None
    return str(path)


def squash(text):
    """Collapse whitespace runs, as Playwright's text assertions do."""
    return " ".join(text.split())


def assert_text(shot, region_text, page_text):
    region_text, page_text = squash(region_text), squash(page_text)
    for needle in shot.expect:
        if squash(needle) not in region_text:
            raise AssertionError(
                f"{shot.name}: {TARGET} does not contain {needle!r}, which this "
                f"state must show"
            )
    for needle in shot.absent:
        if squash(needle) in page_text:
            raise AssertionError(
                f"{shot.name}: page contains {needle!r}, which this state must not show"
            )


def png_size(path):
    """The PNG's pixel dimensions, from its IHDR chunk."""
    header = path.read_bytes()[16:24]
    return int.from_bytes(header[:4], "big"), int.from_bytes(header[4:], "big")


def union_clip(page, shot, boxes, pad=16):
    """The smallest viewport-bounded rectangle holding every box, padded."""
    if any(box is None for box in boxes):
        raise AssertionError(
            f"{shot.name}: {TARGET} or {OVERLAY} has no bounding box (not visible)"
        )
    viewport = page.viewport_size
    x0 = max(min(b["x"] for b in boxes) - pad, 0)
    y0 = max(min(b["y"] for b in boxes) - pad, 0)
    x1 = min(max(b["x"] + b["width"] for b in boxes) + pad, viewport["width"])
    y1 = min(max(b["y"] + b["height"] for b in boxes) + pad, viewport["height"])
    if x1 <= x0 or y1 <= y0:
        raise AssertionError(
            f"{shot.name}: the union of {TARGET} and {OVERLAY} is outside the "
            f"viewport; scroll it into view before the shot"
        )
    return {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}


def take(browser, shot, auth, dest, reached):
    width, height = shot.viewport
    context = browser.new_context(
        storage_state=auth.get(shot.user),
        ignore_https_errors=True,
        viewport={"width": width, "height": height},
        device_scale_factor=2,
    )
    page = context.new_page()
    page.set_default_timeout(TIMEOUT_MS)
    try:
        try:
            response = page.goto(shot.url)
        except PlaywrightError as exc:
            # A refused connection, or a timeout while a dev server compiles.
            raise Transient(f"{shot.name}: {exc}") from exc
        # Checked, or an error page fails as "TARGET not found".
        if response and not response.ok:
            trouble = f"{shot.name}: HTTP {response.status} for {response.url}"
            if response.status >= 500:
                # The gateway answers 5xx while the app restarts: retry, and
                # don't count it as the app answering.
                raise Transient(trouble)
            # A 4xx won't change on retry, but the app did answer.
            reached.add(shot.name)
            raise AssertionError(trouble)
        # The app served a page, so publish() replaces the old images even if
        # this shot fails below. A set, not a return value: failures raise.
        reached.add(shot.name)
        region = page.locator(TARGET)
        region.wait_for(state="visible")

        for selector in shot.click:
            page.click(selector)
        if shot.click:
            # Otherwise the last control photographs hovered. `opens_overlay`
            # skips this, since moving off can close what it opened.
            page.mouse.move(0, 0)
        if shot.scroll_to:
            page.locator(shot.scroll_to).scroll_into_view_if_needed()

        for needle in shot.expect:
            # Re-queries each poll, unlike a handle, which goes stale when the
            # node is replaced. innerText, like assert_text: rendered text,
            # not textContent, which includes hidden nodes and ignores CSS
            # text-transform.
            expect_locator(region).to_contain_text(
                needle, use_inner_text=True, timeout=TIMEOUT_MS
            )

        # Again after the settle: a late render can replace what passed.
        page.wait_for_timeout(SETTLE_MS)
        body = page.locator("body")
        assert_text(shot, region.inner_text(), body.inner_text())

        target = dest / f"{shot.name}.png"
        if shot.opens_overlay:
            # page.click, not an injected element.click(), which leaves
            # Chrome's input modality unset so self-focusing controls show a
            # :focus-visible ring.
            page.click(shot.opens_overlay)
            # Filtered by text and visibility: the first OVERLAY match is often
            # another portal, or a closed one kept mounted.
            overlay = (
                page.locator(OVERLAY)
                .filter(has_text=shot.overlay_expect[0], visible=True)
                .first
            )
            overlay.wait_for(state="visible")
            page.wait_for_timeout(800)
            for needle in shot.overlay_expect:
                expect_locator(overlay).to_contain_text(
                    needle, use_inner_text=True, timeout=TIMEOUT_MS
                )
            # And the page behind it, since the click can change that too.
            assert_text(shot, region.inner_text(), body.inner_text())
            page.screenshot(
                path=target,
                clip=union_clip(
                    page, shot, [region.bounding_box(), overlay.bounding_box()]
                ),
            )
        else:
            # Element, not viewport: tens of KB instead of megabytes at 2x.
            region.screenshot(path=target)
        shot_width, shot_height = png_size(target)
        log(
            f"  {shot.name}.png  {shot_width}x{shot_height}px "
            f"({target.stat().st_size // 1024}KB, {width}x{height} viewport)"
        )
    finally:
        context.close()


def capture_all(selected, auth, dest):
    failures = []
    reached = set()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for shot in selected:
                # Sequential and retried: a watch-mode dev server gets
                # OOM-killed mid-run and takes about a minute to recompile.
                # Parallel captures make that worse.
                error = None
                for attempt in range(1, ATTEMPTS + 1):
                    try:
                        take(browser, shot, auth, dest, reached)
                        error = None
                        break
                    except Transient as exc:
                        error = exc
                        if attempt < ATTEMPTS:
                            log(f"  {shot.name}: attempt {attempt} failed ({exc})")
                            time.sleep(RETRY_WAIT_SECONDS)
                    except (AssertionError, PlaywrightError, OSError) as exc:
                        # The app answered, so a retry gets the same answer.
                        error = exc
                        break
                if error:
                    # take() can write the PNG before a later step raises.
                    (dest / f"{shot.name}.png").unlink(missing_ok=True)
                    failures.append(shot.name)
                    log(f"  {shot.name}: FAILED {type(error).__name__}: {error}")
        finally:
            browser.close()
    return failures, bool(reached)


def publish(selected, staged, full_run):
    """Move staged PNGs into OUT, replacing what they supersede.

    Called only once the app has answered, so a stack that's down leaves the
    old set alone. After that a failed shot must leave no image: the exit code
    goes unread when the directory is dragged into a comment. A full run
    clears every PNG, so renamed shots leave nothing behind.
    """
    for shot in selected:
        (OUT / f"{shot.name}.png").unlink(missing_ok=True)
    if full_run:
        for stale in OUT.glob("*.png"):
            stale.unlink()
    for png in sorted(staged.glob("*.png")):
        # shutil, not Path.replace: temp dir and OUT may be different disks.
        shutil.move(str(png), OUT / png.name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="+", metavar="NAME")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    if not SHOTS:
        raise SystemExit("SHOTS is empty; there is nothing to capture")
    duplicates = {s.name for s in SHOTS if [x.name for x in SHOTS].count(s.name) > 1}
    if duplicates:
        raise SystemExit(f"duplicate shot name(s): {', '.join(sorted(duplicates))}")

    if args.list:
        for shot in SHOTS:
            print(f"{shot.name:40} {shot.user or '(signed out)':12} {shot.url}")
        return

    if not AUTH_SCRIPT.exists():
        raise SystemExit(
            f"{AUTH_SCRIPT} not found -- copy it out of the skill's scripts/ "
            f"directory alongside this file"
        )

    selected = SHOTS
    if args.only:
        wanted = set(args.only)
        unknown = wanted - {shot.name for shot in SHOTS}
        if unknown:
            log(f"unknown shot(s): {', '.join(sorted(unknown))}")
            sys.exit(2)
        selected = [shot for shot in SHOTS if shot.name in wanted]

    OUT.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="capture-") as work:
        work = Path(work)
        staged = work / "shots"
        staged.mkdir()
        auth = {
            key: mint_auth(key, work)
            for key in sorted({s.user for s in selected if s.user})
        }
        failures, answered = capture_all(selected, auth, staged)
        if answered:
            publish(selected, staged, full_run=not args.only)

    log(f"\n{len(selected) - len(failures)}/{len(selected)} captured -> {OUT}")
    if failures:
        log(f"failed: {', '.join(failures)}")
        if not answered:
            log("nothing reached the app, so the previous images are untouched")
        sys.exit(1)


if __name__ == "__main__":
    main()
