#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright==1.61.0"]
# ///
"""Template: a re-runnable, self-checking screenshot capture for a PR.

Copy this next to the work it documents, fill in the marked block, and run it.
Copy get-auth-context.py alongside it; this script invokes it as a sibling.

    uv run capture.py                           # everything
    uv run capture.py --only hero hero-tablet   # re-shoot a subset
    uv run capture.py --list                    # the plan, no browser

Playwright downloads a Chromium revision per version, so the pin above is what
keeps a run from fetching a fresh 350MB browser. On a machine that has never
run this, install that revision once:

    uv run --with "playwright==1.61.0" playwright install chromium

Every shot asserts on the text the page must and must not show, so a gateway
5xx, an expired login, a stale dev-server build or the wrong fixture fails
the run instead of being saved as a plausible-looking PNG. Assertions about
copy the branch introduces double as a deploy check on the server under test.
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
# URLs that answer with the current user. One per session the pages depend on:
# an app that proxies another service's API through its own host holds a
# separate session per upstream, and a context missing one silently renders
# the anonymous state rather than erroring.
VERIFY_URLS = [f"{BASE}/api/v0/users/me/"]

# Key -> (username, password). Keys are what SHOTS refer to; a shot with
# user=None is captured signed out. These are the local-dev defaults; a state
# that needs a second account needs a second entry.
USERS = {
    "admin": ("admin@odl.local", "localdev123"),
    "learner": ("learner@odl.local", "localdev123"),
}

# Where the PNGs land. One flat directory: the next step is a drag-and-drop
# into a GitHub comment.
OUT = HERE / "shots"

# The region under review. An element screenshot of this is the default shot,
# so the image is not padded by the rest of the page. Look at the page and
# choose it: TARGET is both the crop and the scope of every presence
# assertion, so a wrong value crops every shot to the wrong element without
# failing anything.
TARGET = "main"
# An overlay rendered in a portal outside TARGET -- a popover, tooltip or
# modal. A shot naming `opens_overlay` crops to the union of the two boxes.
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
# How capture.py hands the password to get-auth-context.py. Not on the command
# line: that is world-readable in `ps` and lands in any traceback.
PASSWORD_ENV = "CAPTURE_PASSWORD"


@dataclass
class Shot:
    name: str
    url: str
    #: A key of USERS, or None to capture this shot signed out.
    user: str | None
    #: Text that must appear inside TARGET, checked both before and after the
    #: settle. Also the settle condition, and required: a shot that asserts
    #: nothing cannot fail, which is the whole failure this template exists to
    #: prevent.
    expect: list[str]
    #: Text that must NOT appear anywhere on the page -- what separates "no
    #: data for this user" from "this user is logged out", which often render
    #: identically. Checked against the whole document, not TARGET, because
    #: the copy that gives a logged-out state away is usually in the header.
    absent: list[str] = field(default_factory=list)
    viewport: tuple[int, int] = DESKTOP
    #: Selectors clicked in order before the shot -- an accordion to expand, a
    #: "Show all" to press. Real clicks, so a control that focuses itself does
    #: not photograph with a :focus-visible ring it never shows in use.
    click: list[str] = field(default_factory=list)
    #: Scroll this into view before the shot. Not for framing -- an element
    #: screenshot of TARGET captures its full height whatever is scrolled into
    #: view. It is for content that does not render until it is visible
    #: (lazy images, anything behind an IntersectionObserver), and for
    #: `opens_overlay` shots, whose crop is bounded by the viewport.
    scroll_to: str = ""
    #: Click this, then crop to it and whatever overlay it opened.
    opens_overlay: str = ""
    #: Text the overlay itself must show. A broad OVERLAY selector will
    #: happily match some other portalled element that was already on the
    #: page, so name what this one says. Required with `opens_overlay`.
    overlay_expect: list[str] = field(default_factory=list)

    def __post_init__(self):
        for label in ("expect", "absent", "overlay_expect", "click"):
            value = getattr(self, label)
            if isinstance(value, str):
                # Silently iterating a string's characters would assert that
                # the page contains "W", then "e", then "l" -- true of any
                # page with prose, and a shot that cannot fail.
                raise TypeError(
                    f"{self.name}: {label} must be a list of strings, not a string"
                )
            if any(not isinstance(item, str) or not item for item in value):
                # Every page contains "", so an empty needle asserts nothing
                # in `expect`, holds nowhere in `absent`, and matches the
                # first broad portal in `overlay_expect`. An empty `click`
                # selector matches nothing and times out instead.
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
    # An interaction, and `expect` naming what it reveals -- so the click is
    # also asserted, not assumed.
    Shot(
        "runs-expanded",
        f"{BASE}/dashboard",
        "admin",
        ["Starts 3 June"],
        click=["button:has-text('Upcoming runs')"],
    ),
    # Signed out: `expect` is scoped to TARGET, so name copy the page body
    # actually shows anonymously rather than a header sign-in link.
    Shot("signed-out", f"{BASE}/dashboard", None, ["Sign in to continue"]),
]


class Transient(Exception):
    """A failure the next attempt has a real chance of not seeing."""


def log(message):
    print(message, file=sys.stderr, flush=True)


def mint_auth(user_key, directory):
    """Write a fresh storage state for `user_key` into `directory`.

    Fresh every run, never cached: these contexts expire with the SSO session
    behind them and an expired one does not error, it renders every page
    anonymous. The directory is temporary and deleted afterwards -- a storage
    state holds live session cookies and has no business in a work tree.
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
        # The child already explained itself on stderr; repeating its argv
        # here would only add noise.
        raise SystemExit(f"could not sign in as {username} ({user_key})") from None
    return str(path)


def assert_text(shot, region_text, page_text):
    for needle in shot.expect:
        if needle not in region_text:
            raise AssertionError(
                f"{shot.name}: {TARGET} does not contain {needle!r}, which this "
                f"state must show"
            )
    for needle in shot.absent:
        if needle in page_text:
            raise AssertionError(
                f"{shot.name}: page contains {needle!r}, which this state must not show"
            )


def png_size(path):
    """The PNG's own pixel dimensions, out of its IHDR chunk.

    Not the viewport: every shot here is cropped, and device_scale_factor=2
    doubles what lands in the file, so the viewport describes no image the
    run produces.
    """
    header = path.read_bytes()[16:24]
    return int.from_bytes(header[:4], "big"), int.from_bytes(header[4:], "big")


def union_clip(page, shot, boxes, pad=16):
    """The smallest viewport-bounded rectangle holding every box, padded.

    Boxes are viewport-relative, so an element scrolled out of view yields
    nothing usable; say which selector rather than raising a bare TypeError
    three retries deep.
    """
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


def take(browser, shot, auth, dest):
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
        # Checked, because otherwise the error page simply has no TARGET and
        # the run spends three timeouts reporting that a locator never
        # appeared: true, and the wrong cause. goto() returns None only for a
        # same-document navigation, which the first one on a fresh page never
        # is.
        response = page.goto(shot.url)
        if response and not response.ok:
            trouble = f"{shot.name}: HTTP {response.status} for {response.url}"
            if response.status >= 500:
                # The transient case retry exists for: the gateway stays up
                # and answers 502/503/504 while the upstream recompiles after
                # an OOM kill. A 4xx is the route being wrong, and the next
                # attempt answers it identically.
                raise Transient(trouble)
            raise AssertionError(trouble)
        region = page.locator(TARGET)
        region.wait_for(state="visible")

        for selector in shot.click:
            # Real clicks, before the assertions: an interaction is often what
            # brings the state under review onto the page, and `expect` is the
            # condition the run waits on.
            page.click(selector)
        if shot.click:
            # Playwright leaves the pointer where it last clicked, so the
            # control photographs hovered. Nothing here is opened by hover --
            # `opens_overlay` keeps its pointer for exactly that reason.
            page.mouse.move(0, 0)
        if shot.scroll_to:
            page.locator(shot.scroll_to).scroll_into_view_if_needed()

        for needle in shot.expect:
            # A web-first assertion, which re-queries the selector on every
            # poll: a handle grabbed once keeps reporting the text of a node
            # the page has already replaced.
            expect_locator(region).to_contain_text(needle, timeout=TIMEOUT_MS)

        # Re-assert after the settle, not just before it: a late render can
        # replace the state that satisfied the wait, or bring in the very text
        # this state is defined by not having.
        page.wait_for_timeout(SETTLE_MS)
        assert_text(shot, region.inner_text(), page.locator("body").inner_text())

        target = dest / f"{shot.name}.png"
        if shot.opens_overlay:
            # A real click: element.click() from injected JavaScript leaves
            # Chrome's interaction modality unset, so a control that focuses
            # itself photographs with a :focus-visible ring it never shows.
            page.click(shot.opens_overlay)
            # Filtered by what this overlay says, not `.first`: OVERLAY is
            # deliberately broad, and the earliest match in DOM order is some
            # other portal as often as it is the one the click opened.
            overlay = page.locator(OVERLAY).filter(has_text=shot.overlay_expect[0])
            overlay.first.wait_for(state="visible")
            overlay = overlay.first
            page.wait_for_timeout(800)
            for needle in shot.overlay_expect:
                expect_locator(overlay).to_contain_text(needle, timeout=TIMEOUT_MS)
            page.screenshot(
                path=target,
                clip=union_clip(
                    page, shot, [region.bounding_box(), overlay.bounding_box()]
                ),
            )
        else:
            # Element, not viewport: at device_scale_factor=2 a full viewport
            # runs to megabytes where the region under review is tens of KB.
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
    answered = False
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for shot in selected:
                # Sequential, and retried: a watch-mode dev server holds the
                # whole compiled app in memory and gets OOM-killed partway
                # through a run this size. The orchestrator restarts it and the
                # next request recompiles, which costs about a minute. Not
                # retrying costs the shot. Never run these in parallel against
                # such a server.
                for attempt in range(1, ATTEMPTS + 1):
                    try:
                        take(browser, shot, auth, dest)
                        answered = True
                        break
                    except AssertionError as exc:
                        # The page loaded and said the wrong thing. A second
                        # attempt says it again -- and the app answering at
                        # all is what licenses replacing the published set.
                        answered = True
                        failures.append(shot.name)
                        log(f"  {shot.name}: FAILED {exc}")
                        break
                    except (Transient, PlaywrightError, OSError) as exc:
                        reason = f"{type(exc).__name__}: {exc}"
                        if attempt == ATTEMPTS:
                            failures.append(shot.name)
                            log(f"  {shot.name}: FAILED {reason}")
                        else:
                            log(f"  {shot.name}: attempt {attempt} failed ({reason})")
                            time.sleep(RETRY_WAIT_SECONDS)
        finally:
            browser.close()
    return failures, answered


def publish(selected, staged, full_run):
    """Move this run's PNGs into OUT, replacing whatever they supersede.

    Captured to a staging directory and published at the end, so a run that
    never reached the app cannot delete a set it has no way of retaking --
    including a signed-out-only `--only`, where no login gets the chance to
    fail first. Within a run that did reach it, replacement is unconditional:
    a shot that failed must leave no image behind, because the command exits
    non-zero but nobody re-reads the log before dragging the directory into a
    comment. A full run clears every PNG, so a renamed shot leaves none.
    """
    for shot in selected:
        (OUT / f"{shot.name}.png").unlink(missing_ok=True)
    if full_run:
        for stale in OUT.glob("*.png"):
            stale.unlink()
    for png in sorted(staged.glob("*.png")):
        # shutil, not Path.replace: the staging directory is a system temp
        # directory and OUT is wherever the work tree is, which need not be
        # the same filesystem.
        shutil.move(str(png), OUT / png.name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="+", metavar="NAME")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    duplicates = {s.name for s in SHOTS if [x.name for x in SHOTS].count(s.name) > 1}
    if duplicates:
        # Two shots of one name overwrite each other's PNG and both answer to
        # --only, so the survivor is whichever ran last.
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
