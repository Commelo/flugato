"""The Update card and pages on the web UI, with a stand-in for update.Updater."""

import http.client
import os
import shutil
import tempfile
import threading
import unittest
import urllib.parse

from flugato import VERSION, web


class FakeUpdater:
    """What the web UI reads and calls; the real one is tested in test_update."""

    def __init__(self, blocked=None, previous=None, phase="idle", available=None, error=None,
                 text="", checked_at=None):
        self.base = "https://site.example"
        self._blocked, self._previous = blocked, previous
        self._state = {"phase": phase, "text": text, "available": available, "error": error,
                       "checked_at": checked_at}
        self.calls = []

    def state(self):
        return dict(self._state)

    def blocked(self):
        return self._blocked

    def previous(self):
        return self._previous

    def check(self):
        self.calls.append("check")
        self._state.update(phase="idle", text=f"{VERSION} is the latest", checked_at=1.0)
        return self.state()

    def install(self, version=None):
        self.calls.append(f"install {version or self._state['available']}")
        self._state.update(phase="downloading", text="downloading 9.9.9: 10 kB")
        return True

    def rollback(self, restart=True):
        self.calls.append("rollback")
        self._state.update(phase="restarting", text=f"back to {self._previous}: restarting…")
        return True


class Web(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conf = os.path.join(self.tmp, "flugato.conf")
        with open(self.conf, "w") as f:
            f.write("CHECK_HOST=check.example\n")
        self.ui = None

    def tearDown(self):
        if self.ui:
            self.ui.stop()
        shutil.rmtree(self.tmp)

    def start(self, updater, pin=None):
        self.ui = web.WebUI(self.tmp, self.conf, updater=updater, pin=pin,
                            on_restart=threading.Event().set)
        self.ui.start("127.0.0.1", 0)
        self.host = f"127.0.0.1:{self.ui.port}"
        return self.ui

    def request(self, method, path, form=None, cookie=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.ui.port, timeout=5)
        body = urllib.parse.urlencode(form).encode() if form is not None else None
        headers = {"Host": self.host}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = origin or f"http://{self.host}"
        if cookie:
            headers["Cookie"] = cookie
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = res.read().decode(errors="replace")
        conn.close()
        return res, data

    # --- the card ---------------------------------------------------------------------------
    def test_no_updater_no_card_and_no_routes(self):
        self.start(None)
        _, page = self.request("GET", "/settings")
        self.assertNotIn("Update</b>", page)
        self.assertIn(f"fluGATO {VERSION}", page)       # the version shows anyway
        _, page = self.request("GET", "/update")
        self.assertIn("nothing at this address", page)
        _, page = self.request("POST", "/update/check", {})
        self.assertIn("nothing at this address", page)

    def test_blocked_shows_the_reason_and_no_buttons(self):
        self.start(FakeUpdater(blocked="Join a Wi-Fi network with internet to check for updates."))
        _, page = self.request("GET", "/settings")
        self.assertIn("Join a Wi-Fi network with internet", page)
        self.assertNotIn('action="/update/check"', page)
        self.assertNotIn('action="/update/install"', page)

    def test_idle_card_offers_a_check_and_the_status_page_shows_the_version(self):
        up = FakeUpdater()
        self.start(up)
        _, page = self.request("GET", "/settings")
        self.assertIn('action="/update/check"', page)
        self.assertNotIn('action="/update/install"', page)
        self.assertNotIn("Roll back", page)
        _, status = self.request("GET", "/")
        self.assertIn("Version", status)
        self.assertIn(VERSION, status)
        _, page = self.request("POST", "/update/check", {})
        self.assertEqual(up.calls, ["check"])
        self.assertIn(f"{VERSION} is the latest", page)   # back on Settings with the answer

    def test_available_offers_install_and_the_status_page_says_so(self):
        up = FakeUpdater(available="9.9.9", text=f"9.9.9 is out (this is {VERSION})", checked_at=1.0)
        self.start(up)
        _, page = self.request("GET", "/settings")
        self.assertIn("Install 9.9.9", page)
        self.assertIn('action="/update/install"', page)
        _, status = self.request("GET", "/")
        self.assertIn("9.9.9 available", status)
        _, page = self.request("POST", "/update/install", {})
        self.assertEqual(up.calls, ["install 9.9.9"])
        self.assertIn("Updating", page)
        self.assertIn("downloading 9.9.9", page)
        self.assertIn('content="2;url=/update"', page)     # follows itself

    def test_error_shows_on_the_card_and_on_the_progress_page(self):
        self.start(FakeUpdater(phase="error", error="update to 9.9.9 failed: sha256 mismatch"))
        _, page = self.request("GET", "/settings")
        self.assertIn("sha256 mismatch", page)
        self.assertIn('action="/update/check"', page)      # one can try again
        _, page = self.request("GET", "/update")
        self.assertIn("Update failed", page)
        self.assertIn("nothing was changed", page)

    def test_restarting_is_the_restart_page(self):
        self.start(FakeUpdater(phase="restarting", text="9.9.9 installed: restarting…"))
        _, page = self.request("GET", "/update")
        self.assertIn("Restarting", page)
        self.assertIn(f'content="{web.RESTART_RELOAD_LAN};url=/settings"', page)
        _, page = self.request("GET", "/settings")
        self.assertIn('href="/update"', page)                # the card points at the progress

    def test_rollback_asks_then_rolls_back(self):
        up = FakeUpdater(previous="0.4.9")
        self.start(up)
        _, page = self.request("GET", "/settings")
        self.assertIn("Roll back to 0.4.9", page)
        _, page = self.request("POST", "/update/rollback", {})
        self.assertIn("Back to 0.4.9?", page)
        self.assertIn('action="/update/rollback/now"', page)
        self.assertEqual(up.calls, [])                       # the question alone does nothing
        _, page = self.request("POST", "/update/rollback/now", {})
        self.assertEqual(up.calls, ["rollback"])
        self.assertIn("Restarting", page)

    def test_without_a_previous_tree_there_is_no_rollback(self):
        self.start(FakeUpdater())
        _, page = self.request("POST", "/update/rollback", {})
        self.assertIn("nothing at this address", page)
        _, page = self.request("POST", "/update/rollback/now", {})
        self.assertIn("nothing at this address", page)

    # --- the guards -----------------------------------------------------------------------
    def test_the_pin_and_the_origin_still_guard_everything(self):
        up = FakeUpdater(available="9.9.9")
        self.start(up, pin=lambda: "4321")
        res, _ = self.request("POST", "/update/install", {})
        self.assertEqual(res.status, 303)                    # to the PIN page
        res, _ = self.request("GET", "/update")
        self.assertEqual(res.status, 303)
        res, _ = self.request("POST", "/login", {"pin": "4321"})
        cookie = res.getheader("Set-Cookie").split(";")[0]
        res, _ = self.request("POST", "/update/install", {}, cookie, origin="http://evil.example")
        self.assertEqual(res.status, 403)
        self.assertEqual(up.calls, [])
        res, _ = self.request("POST", "/update/install", {}, cookie)
        self.assertEqual(res.status, 200)
        self.assertEqual(up.calls, ["install 9.9.9"])


if __name__ == "__main__":
    unittest.main()
