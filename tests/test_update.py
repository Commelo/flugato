"""Device self-update tests against a local release site and temporary installed trees."""

import gzip
import hashlib
import io
import os
import sys
import tarfile
import tempfile
import threading
import unittest

from flugato import update
from .site_fake import make_device_release, serve


OLD_VERSION = "1.2.3"
NEW_VERSION = "9.9.9"


def write_tree(path, version, main=None):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "__init__.py"), "w") as f:
        f.write(f'VERSION = "{version}"\n')
    with open(os.path.join(path, "__main__.py"), "w") as f:
        f.write(main or "raise SystemExit(0)\n")


class Versions(unittest.TestCase):
    def test_newer_orders_numeric_parts_and_rejects_non_numeric_values(self):
        self.assertTrue(update.newer("2.0", "1.99"))
        self.assertTrue(update.newer("1.2.1", "1.2"))
        self.assertFalse(update.newer("1.2", "1.2.0"))
        self.assertFalse(update.newer("1.x", "1.0"))
        self.assertFalse(update.newer(None, "1.0"))

    def test_valid_accepts_bounded_numeric_versions(self):
        self.assertTrue(update.valid("1"))
        self.assertTrue(update.valid("1.20.300.4000"))
        for value in ("", "1.x", "1.2.3.4.5", "12345", "1." + "2" * 65, None):
            self.assertFalse(update.valid(value), value)

    def test_tree_version_with_and_without_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_tree(tmp, "4.5.6")
            self.assertEqual(update.tree_version(tmp), "4.5.6")
            with open(os.path.join(tmp, "__init__.py"), "w") as f:
                f.write("NAME = 'flugato'\n")
            self.assertIsNone(update.tree_version(tmp))

    def test_exchange_swaps_existing_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = os.path.join(tmp, "a"), os.path.join(tmp, "b")
            os.makedirs(a)
            os.makedirs(b)
            with open(os.path.join(a, "value"), "w") as f:
                f.write("a")
            with open(os.path.join(b, "value"), "w") as f:
                f.write("b")
            update.exchange(a, b)
            self.assertTrue(os.path.isdir(a))
            self.assertTrue(os.path.isdir(b))
            with open(os.path.join(a, "value")) as f:
                self.assertEqual(f.read(), "b")
            with open(os.path.join(b, "value")) as f:
                self.assertEqual(f.read(), "a")


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "installed")
        self.site = os.path.join(self.tmp.name, "site")
        os.makedirs(self.root)
        os.makedirs(self.site)
        write_tree(os.path.join(self.root, "flugato"), OLD_VERSION)
        self.source = os.path.join(self.tmp.name, "source")
        make_device_release(self.site, NEW_VERSION, self.source)
        self.server, self.base = serve(self.site)
        self.restarted = threading.Event()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def updater(self, **kw):
        options = {
            "root": self.root,
            "base": self.base,
            "client": lambda: {"ssid": "x", "address": "192.0.2.10"},
            "mounts": lambda: "ext4 / ext4 rw 0 0",
            "python": sys.executable,
            "on_restart": self.restarted.set,
            "clock": lambda: 123.0,
        }
        options.update(kw)
        return update.Updater(**options)

    def set_latest(self, text):
        with open(os.path.join(self.site, "releases", "latest"), "w") as f:
            f.write(text)

    def join(self, updater):
        updater.thread.join(5)
        self.assertFalse(updater.thread.is_alive())

    def installed_version(self):
        return update.tree_version(os.path.join(self.root, "flugato"))

    def rewrite_device_release(self, data, version=NEW_VERSION):
        release = os.path.join(self.site, "releases", version)
        path = os.path.join(release, update.TARBALL)
        with open(path, "wb") as f:
            f.write(data)
        with open(os.path.join(release, "SHA256SUMS"), "w") as f:
            f.write(f"{hashlib.sha256(data).hexdigest()}  {update.TARBALL}\n")
        self.set_latest(version + "\n")

    def test_check_reports_current_and_available_versions(self):
        u = self.updater()
        u.version = NEW_VERSION
        current = u.check()
        self.assertIn("is the latest", current["text"])
        self.assertIsNone(current["available"])

        self.set_latest("99.0.0\n")
        available = u.check()
        self.assertEqual(available["available"], "99.0.0")
        self.assertEqual(available["checked_at"], 123.0)

    def test_check_records_site_errors(self):
        cases = (
            ("http://127.0.0.1:1", None),
            (self.base, "not-a-version\n"),
            (self.base, "1" * 65),
        )
        for base, latest in cases:
            with self.subTest(latest=latest):
                if latest is not None:
                    self.set_latest(latest)
                state = self.updater(base=base).check()
                self.assertEqual(state["phase"], "idle")
                self.assertIn("cannot reach", state["error"])

    def test_blocked_reasons(self):
        readonly = self.updater(mounts=lambda: "overlay / overlay rw 0 0")
        self.assertIn("read-only", readonly.blocked())
        offline = self.updater(client=lambda: None)
        self.assertIn("Wi-Fi", offline.blocked())
        missing = self.updater(root=os.path.join(self.tmp.name, "missing"))
        self.assertIn("not installed", missing.blocked())

    def test_install_does_not_start_when_blocked_or_without_a_version(self):
        self.assertFalse(self.updater(client=lambda: None).install(NEW_VERSION))
        self.assertFalse(self.updater().install())

    def test_good_install_swaps_trees_and_restarts(self):
        u = self.updater()
        self.assertTrue(u.install(NEW_VERSION))
        self.join(u)
        self.assertEqual(self.installed_version(), NEW_VERSION)
        self.assertEqual(update.tree_version(os.path.join(self.root, "flugato.prev")), OLD_VERSION)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".update")))
        with open(os.path.join(self.root, update.MARKER)) as f:
            self.assertEqual(f.read().strip(), "0")
        self.assertTrue(self.restarted.is_set())
        self.assertEqual(u.state()["phase"], "restarting")

    def test_bad_checksum_leaves_installed_tree_untouched(self):
        sums = os.path.join(self.site, "releases", NEW_VERSION, "SHA256SUMS")
        with open(sums, "w") as f:
            f.write("0" * 64 + f"  {update.TARBALL}\n")
        u = self.updater()
        self.assertTrue(u.install(NEW_VERSION))
        self.join(u)
        self.assertEqual(self.installed_version(), OLD_VERSION)
        self.assertEqual(u.state()["phase"], "error")
        self.assertIn("sha256", u.state()["error"])

    def malicious_tar(self, kind):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            for name, body in (("flugato/__init__.py", b'VERSION = "9.9.9"\n'),
                               ("flugato/__main__.py", b"raise SystemExit(0)\n")):
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
            if kind == "symlink":
                info = tarfile.TarInfo("flugato/link")
                info.type = tarfile.SYMTYPE
                info.linkname = "flugato/__init__.py"
                tar.addfile(info)
            else:
                name = "etc/passwd" if kind == "outside" else "/etc/passwd"
                info = tarfile.TarInfo(name)
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))
        return out.getvalue()

    def test_unsafe_tar_members_are_refused(self):
        for kind in ("outside", "absolute", "symlink"):
            with self.subTest(kind=kind):
                self.rewrite_device_release(self.malicious_tar(kind))
                u = self.updater()
                self.assertTrue(u.install(NEW_VERSION))
                self.join(u)
                self.assertEqual(u.state()["phase"], "error")
                self.assertEqual(self.installed_version(), OLD_VERSION)

    def test_tar_member_header_over_unpacked_limit_is_refused(self):
        chunks = []
        for name, body in (("flugato/__init__.py", b'VERSION = "9.9.9"\n'),
                           ("flugato/__main__.py", b"raise SystemExit(0)\n")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            chunks.extend((info.tobuf(), body, bytes((-len(body)) % 512)))
        huge = tarfile.TarInfo("flugato/huge")
        huge.size = update.MAX_UNPACKED + 1
        chunks.extend((huge.tobuf(), bytes(1024)))
        self.rewrite_device_release(gzip.compress(b"".join(chunks), mtime=0))
        u = self.updater()
        self.assertTrue(u.install(NEW_VERSION))
        self.join(u)
        self.assertEqual(u.state()["phase"], "error")
        self.assertEqual(self.installed_version(), OLD_VERSION)

    def test_release_tree_version_must_match_latest(self):
        other_source = os.path.join(self.tmp.name, "other-source")
        make_device_release(self.site, "9.9.8", other_source)
        source_release = os.path.join(self.site, "releases", "9.9.8", update.TARBALL)
        with open(source_release, "rb") as f:
            data = f.read()
        self.rewrite_device_release(data, "9.9.9")
        u = self.updater()
        self.assertTrue(u.install("9.9.9"))
        self.join(u)
        self.assertIn("says it is", u.state()["error"])
        self.assertEqual(self.installed_version(), OLD_VERSION)

    def test_release_tree_must_pass_smoke_test(self):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            for name, body in (("flugato/__init__.py", b'VERSION = "9.9.9"\n'),
                               ("flugato/__main__.py", b"raise SystemExit(7)\n")):
                info = tarfile.TarInfo(name)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        self.rewrite_device_release(out.getvalue())
        u = self.updater()
        self.assertTrue(u.install(NEW_VERSION))
        self.join(u)
        self.assertIn("does not run", u.state()["error"])
        self.assertEqual(self.installed_version(), OLD_VERSION)

    def test_previous_reports_none_and_version(self):
        u = self.updater()
        self.assertIsNone(u.previous())
        write_tree(os.path.join(self.root, "flugato.prev"), "1.0.0")
        self.assertEqual(u.previous(), "1.0.0")

    def test_rollback_exchanges_trees_and_restarts(self):
        write_tree(os.path.join(self.root, "flugato.prev"), "1.0.0")
        u = self.updater()
        self.assertTrue(u.rollback())
        self.assertEqual(self.installed_version(), "1.0.0")
        self.assertEqual(update.tree_version(os.path.join(self.root, "flugato.prev")), OLD_VERSION)
        self.assertTrue(self.restarted.is_set())

    def test_rollback_without_previous_tree_returns_false(self):
        self.assertFalse(self.updater().rollback())
        self.assertFalse(self.restarted.is_set())

    def test_boot_guard_without_marker_does_nothing(self):
        self.assertFalse(self.updater().boot_guard(log=lambda *a, **k: None))

    def test_boot_guard_increments_marker(self):
        with open(os.path.join(self.root, update.MARKER), "w") as f:
            f.write("0\n")
        u = self.updater()
        self.assertFalse(u.boot_guard(log=lambda *a, **k: None))
        with open(u.marker) as f:
            self.assertEqual(f.read().strip(), "1")

    def test_boot_guard_rolls_back_after_repeated_failed_starts(self):
        write_tree(os.path.join(self.root, "flugato.prev"), "1.0.0")
        u = self.updater()
        with open(u.marker, "w") as f:
            f.write("2\n")
        self.assertTrue(u.boot_guard(log=lambda *a, **k: None))
        self.assertEqual(self.installed_version(), "1.0.0")
        self.assertFalse(os.path.exists(u.marker))
        self.assertFalse(self.restarted.is_set())

    def test_settle_removes_marker(self):
        u = self.updater()
        with open(u.marker, "w") as f:
            f.write("1\n")
        u.settle()
        self.assertFalse(os.path.exists(u.marker))


if __name__ == "__main__":
    unittest.main()
