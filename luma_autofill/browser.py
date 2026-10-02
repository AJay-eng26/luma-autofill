"""Start the user's real browser so Luma's bot check sees an ordinary visitor.

Luma's "Verifying Your Browser" check stalls when Playwright launches the browser
(automation flags, --no-sandbox, etc.). So we start Chrome/Edge/Brave ourselves as a
normal process:

* ``login`` opens it with nothing attached at all; the user signs in and closes it.
* ``fill`` opens the same profile with a local DevTools port and Playwright connects to it.

If no installed browser is found, we fall back to Playwright's bundled Chromium.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Playwright

BROWSER_CHOICES = ("auto", "chrome", "msedge", "brave", "chromium")


def _candidates(name: str) -> list[str]:
    if sys.platform == "win32":
        roots = [os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        rel = {
            "chrome": r"Google\Chrome\Application\chrome.exe",
            "msedge": r"Microsoft\Edge\Application\msedge.exe",
            "brave": r"BraveSoftware\Brave-Browser\Application\brave.exe",
        }[name]
        return [str(Path(r) / rel) for r in roots if r]
    if sys.platform == "darwin":
        app = {"chrome": "Google Chrome", "msedge": "Microsoft Edge", "brave": "Brave Browser"}[name]
        return [f"/Applications/{app}.app/Contents/MacOS/{app}"]
    cmds = {
        "chrome": ["google-chrome", "google-chrome-stable"],
        "msedge": ["microsoft-edge", "microsoft-edge-stable"],
        "brave": ["brave-browser", "brave"],
    }[name]
    return [p for c in cmds if (p := shutil.which(c))]


def find_browser(choice: str = "auto", path: str | None = None) -> tuple[str, str] | None:
    """Return (name, executable) for an installed browser, or None to use bundled Chromium."""
    if path:
        if not Path(path).exists():
            raise SystemExit(f"--browser-path not found: {path}")
        return "custom", path
    if choice == "chromium":
        return None
    names = ("chrome", "msedge", "brave") if choice == "auto" else (choice,)
    for name in names:
        for exe in _candidates(name):
            if Path(exe).exists():
                return name, exe
    if choice != "auto":
        raise SystemExit(f"Could not find {choice} on this computer. Try --browser auto.")
    return None


def _base_args(profile_dir: Path) -> list[str]:
    # Must be absolute: on Windows, Chrome ignores a relative --user-data-dir and opens the
    # user's everyday profile instead, where remote debugging is blocked.
    return [f"--user-data-dir={profile_dir.resolve()}", "--no-first-run", "--no-default-browser-check"]


def open_plain(exe: str, profile_dir: Path, url: str) -> subprocess.Popen:
    """Open the browser with no automation attached (used for signing in)."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([exe, *_base_args(profile_dir), "--new-window", url])


@dataclass
class Session:
    context: BrowserContext
    # Set True to leave the browser running for the user when the tool exits.
    keep_open: bool = False


@contextmanager
def connected_browser(
    p: Playwright, exe: str | None, profile_dir: Path, headless: bool
) -> Iterator[Session]:
    """Yield a Session for ``profile_dir``, driving a real browser over CDP when possible."""
    profile_dir = profile_dir.resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    if exe is None:
        ctx = p.chromium.launch_persistent_context(str(profile_dir), headless=headless, no_viewport=True)
        session = Session(ctx)
        try:
            yield session
        finally:
            if session.keep_open and not headless:
                # Bundled Chromium dies with this process, so wait for the user instead.
                input("Finish in the browser window, then press Enter here to close it… ")
            ctx.close()
        return

    # Port 0 lets the browser pick a free port and write it to DevToolsActivePort,
    # so we never depend on HTTP (or the system proxy) to discover the endpoint.
    port_file = profile_dir / "DevToolsActivePort"
    port_file.unlink(missing_ok=True)
    cmd = [exe, *_base_args(profile_dir), "--remote-debugging-port=0", "about:blank"]
    if headless:
        cmd.insert(1, "--headless=new")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    browser = None
    try:
        deadline = time.time() + 20
        while True:
            lines = port_file.read_text().split() if port_file.exists() else []
            if len(lines) >= 2:
                endpoint = f"ws://127.0.0.1:{lines[0]}{lines[1]}"
                break
            if time.time() > deadline or (proc.poll() is not None and time.time() > deadline - 15):
                raise SystemExit(
                    "Could not connect to the browser. It is probably still running from an earlier step.\n"
                    "Close every window of that browser (also check the ^ tray icons near the clock), "
                    "or on Windows run:  taskkill /IM chrome.exe /F   (closes all Chrome windows)\n"
                    "then try again."
                )
            time.sleep(0.25)
        browser = p.chromium.connect_over_cdp(endpoint)
        session = Session(browser.contexts[0] if browser.contexts else browser.new_context())
        yield session
        if session.keep_open and not headless:
            # Disconnect and leave the browser running as a plain, un-automated window.
            browser.close()
            return
    finally:
        if browser is not None and proc.poll() is None and not _detached(browser):
            try:
                # Graceful shutdown so cookies (your Luma session) are flushed to disk.
                browser.new_browser_cdp_session().send("Browser.close")
            except Exception:  # noqa: BLE001 - already closed by the user
                pass
        if browser is None or not _detached(browser):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.terminate()


def _detached(browser) -> bool:
    return not browser.is_connected()
