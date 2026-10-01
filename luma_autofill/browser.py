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
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _base_args(profile_dir: Path) -> list[str]:
    return [f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check"]


def open_plain(exe: str, profile_dir: Path, url: str) -> subprocess.Popen:
    """Open the browser with no automation attached (used for signing in)."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([exe, *_base_args(profile_dir), "--new-window", url])


@contextmanager
def connected_browser(
    p: Playwright, exe: str | None, profile_dir: Path, headless: bool
) -> Iterator[BrowserContext]:
    """Yield a context for ``profile_dir``, driving a real browser over CDP when possible."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    if exe is None:
        ctx = p.chromium.launch_persistent_context(str(profile_dir), headless=headless, no_viewport=True)
        try:
            yield ctx
        finally:
            ctx.close()
        return

    port = _free_port()
    cmd = [exe, *_base_args(profile_dir), f"--remote-debugging-port={port}", "about:blank"]
    if headless:
        cmd.insert(1, "--headless=new")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    browser = None
    try:
        endpoint = f"http://127.0.0.1:{port}"
        deadline = time.time() + 20
        while True:
            try:
                urllib.request.urlopen(endpoint + "/json/version", timeout=1).read()
                break
            except OSError:
                if proc.poll() is not None:
                    raise SystemExit(
                        "The browser closed immediately. Close every window that uses this profile "
                        f"({profile_dir}) and try again."
                    )
                if time.time() > deadline:
                    raise SystemExit("Timed out waiting for the browser to start.")
                time.sleep(0.25)
        browser = p.chromium.connect_over_cdp(endpoint)
        yield browser.contexts[0] if browser.contexts else browser.new_context()
    finally:
        if browser is not None:
            try:
                # Graceful shutdown so cookies (your Luma session) are flushed to disk.
                browser.new_browser_cdp_session().send("Browser.close")
            except Exception:  # noqa: BLE001 - already closed by the user
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
