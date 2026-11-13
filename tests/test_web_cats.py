"""The cat on the web pages: image routes, cache, the page that each situation shows,
and the safety headers around them."""

import http.client
import io
import os
import re
import shutil
import tempfile
import unittest

from PIL import Image

from flugato import web


class FakeHotspot:
    def status(self):
        return {"on": True, "ssid": "flugato-test", "clients": 1, "address": "192.0.2.1"}


class WebCats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.caps = os.path.join(self.tmp, "caps")
        os.makedirs(self.caps)
        self.conf = os.path.join(self.tmp, "flugato.conf")
        with open(self.conf, "w") as f:
            f.write("CHECK_HOST=old.example\n")
        self.ui = web.WebUI(self.caps, self.conf, FakeHotspot(), proc_root=os.path.join(self.tmp, "proc"))
        self.ui.start("127.0.0.1", 0)
        self.host = f"127.0.0.1:{self.ui.port}"

    def tearDown(self):
        self.ui.stop()
        shutil.rmtree(self.tmp)

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.ui.port, timeout=5)
        conn.request("GET", path, headers={"Host": self.host, **(headers or {})})
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res, data

    def test_png_and_gif_images(self):
        res, data = self.get("/cat/sit.png")
        self.assertEqual(res.status, 200)
        self.assertEqual(res.getheader("Content-Type"), "image/png")
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertEqual(Image.open(io.BytesIO(data)).size, (28 * web.IMG_SCALE, 18 * web.IMG_SCALE))
        res, data = self.get("/cat/good-lick.gif")
        self.assertEqual(res.status, 200)
        self.assertEqual(res.getheader("Content-Type"), "image/gif")
        self.assertTrue(data.startswith(b"GIF89a"))
        gif = Image.open(io.BytesIO(data))
        self.assertGreater(gif.n_frames, 1)
        self.assertIn("transparency", gif.info)
        self.assertLess(len(data), 40_000)                   # light enough for the Pi 1
        self.assertIn("max-age", res.getheader("Cache-Control"))
        self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")

    def test_every_loop_renders(self):
        for name in web.LOOPS:
            made = web.render_image(name + ".gif")
            self.assertIsNotNone(made, name)
            self.assertTrue(made[0].startswith(b"GIF89a"), name)

    def test_images_are_cached(self):
        _, first = self.get("/cat/web-fiddle.gif")
        self.assertIn("web-fiddle.gif", self.ui._images)
        _, again = self.get("/cat/web-fiddle.gif")
        self.assertEqual(first, again)
        self.assertEqual(len(self.ui._images), 1)

    def test_unknown_images_are_404(self):
        for bad in ("nope.png", "nope.gif", "sit.jpg", "..%2Fsit.png", "SIT.PNG", "sit.png.gif", ""):
            res, _ = self.get(f"/cat/{bad}")
            self.assertEqual(res.status, 404, bad)
        self.assertIsNone(web.render_image("../sit.png"))
        self.assertIsNone(web.render_image("a" * 70 + ".png"))

    def test_host_check_covers_images(self):
        res, _ = self.get("/cat/sit.png", {"Host": "evil.example"})
        self.assertEqual(res.status, 400)

    def test_pages_only_load_our_own_images(self):
        for path in ("/", "/settings", "/captures", "/nothing-here"):
            res, data = self.get(path)
            page = data.decode()
            self.assertNotIn("<script", page)
            for src in re.findall(r'src="([^"]*)"', page):
                self.assertTrue(src.startswith("/cat/"), src)
            for href in re.findall(r'href="([^"]*)"', page):
                self.assertTrue(href.startswith("/"), href)
            csp = res.getheader("Content-Security-Policy")
            self.assertIn("default-src 'none'", csp)
            self.assertIn("img-src 'self'", csp)
            self.assertNotIn("script-src", csp)
            self.assertIn("/cat/", page)                     # every page has its cat

    def test_the_status_cat_follows_the_tester(self):
        _, data = self.get("/")
        self.assertIn('src="/cat/sleep-', data.decode())    # nothing published yet: asleep
        for mode, rows, title, expect in (
                ("nolink", [], "NO LINK", "router-"),
                ("inline", [], "INLINE", "inline-"),
                ("network", [("LINK", "1G Full", "ok"), ("IP", "none", "fail")], "PROBLEM FOUND", "fail-"),
                ("network", [("LINK", "100M", "warn")], "CHECK WARNINGS", "warn-"),
                ("network", [("LINK", "1G Full", "ok"), ("DNS", "…", "pending")], "TESTING…", "testing-"),
                ("cable", [("PAIRS", "all good", "ok")], "ALL GOOD", "good-")):
            self.ui.publish(mode, rows, title)
            _, data = self.get("/")
            self.assertIn(f'src="/cat/{expect}', data.decode(), mode)
            self.assertIn(b"the fluGATO cat", data)

    def test_captures_and_errors_have_their_cats(self):
        _, data = self.get("/captures")
        self.assertIn(b"web-box.gif", data)                 # empty: the box
        self.assertIn(b"confused.png", data)
        with open(os.path.join(self.caps, "inline-1.pcap"), "wb") as f:
            f.write(b"\xd4\xc3\xb2\xa1")
        _, data = self.get("/captures")
        self.assertIn(b"web-fish.gif", data)
        res, data = self.get("/captures/missing.pcap")
        self.assertEqual(res.status, 404)
        self.assertIn(b"web-confused.gif", data)
        res, data = self.get("/settings")
        self.assertIn(b"web-fiddle.gif", data)


if __name__ == "__main__":
    unittest.main()
