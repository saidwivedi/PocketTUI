"""The one test that starts a real browser, when this machine has one.

It is the only proof that the pieces fit: the pipe really is on fds 3 and 4, the
flags really do produce a browser that does not announce itself as automated,
and `close()` really does leave nothing behind — including the systemd scope,
which would otherwise keep its name and collide with the next launch.

Skipped, not failed, where there is no Chromium: most machines running the
suite are not the machine running the browser.
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chromium as C  # noqa: E402

FOUND = C.find_chromium(force=True)
pytestmark = pytest.mark.skipif(FOUND is None, reason="no Chromium on this machine")

PAGE = "data:text/html,<title>ok</title><p>hello"


def unit_alive(unit: str) -> bool:
    out = subprocess.run(["systemctl", "--user", "is-active", unit],
                         capture_output=True, text=True)
    return out.stdout.strip() == "active"


def test_launch_navigate_evaluate_close(tmp_path, monkeypatch):
    # The profile and Chrome's own log land under HOME, so HOME is the temp dir
    # and this test never touches the profile the real browser pane uses.
    monkeypatch.setenv("HOME", str(tmp_path))

    async def main():
        browser = await C.launch(FOUND, C.load_browser_config())
        try:
            assert browser.pid > 0
            assert (tmp_path / ".pockettui" / "chromium-profile").is_dir()
            assert oct(os.stat(tmp_path / ".pockettui" / "chromium-profile").st_mode)[-3:] == "700"

            target = await browser.cdp.send("Target.createTarget",
                                            {"url": "about:blank"})
            sess = await browser.cdp.attach(target["targetId"])
            await sess.send("Page.enable")

            loaded = asyncio.get_running_loop().create_future()
            sess.on("Page.loadEventFired",
                    lambda params: loaded.done() or loaded.set_result(True))
            await sess.send("Page.navigate", {"url": PAGE})
            await asyncio.wait_for(loaded, 10)

            async def value(expr):
                res = await sess.send("Runtime.evaluate",
                                      {"expression": expr, "returnByValue": True})
                return res["result"]["value"]

            assert await value("document.title") == "ok"
            assert await value("navigator.webdriver") is False
            ua = await value("navigator.userAgent")
            assert "Headless" not in ua and "Chrome/" in ua
            assert "Headless" not in browser.user_agent
        finally:
            unit = browser.unit
            await browser.close()
            await browser.close()  # idempotent

        assert browser.closed.done()
        gone = time.monotonic() + 5
        while time.monotonic() < gone:
            try:
                os.kill(browser.pid, 0)
            except OSError:
                break
            await asyncio.sleep(0.1)
        with pytest.raises(OSError):
            os.kill(browser.pid, 0)
        if unit:
            assert not unit_alive(unit)

    asyncio.run(main())
