"""The device updates itself from the site: Check for updates and Install on the web UI's
Settings page, when the Pi is on a Wi-Fi network with internet. A release is
<site>/releases/<version>/flugato-device.tar.gz next to its SHA256SUMS (tools/build_release.py
makes both); <site>/releases/latest holds the newest version number, the same for the device
and the terminal app.

Install: download (capped), check the sum, unpack beside the installed tree (members checked
first: plain files under flugato/ or pi/, bounded count and size), make sure the tree says the
version asked for, draw a preview screen with it (the smoke test: every module imports, Pillow
works), then exchange it with the installed tree in one rename syscall, keep the old tree as
flugato.prev (Roll back on the same page), and restart through systemd. Anything that fails
leaves the running tree alone. After a restart that follows an update, three starts that die
young roll back by themselves (boot_guard), so a bad release cannot loop forever.

Refused while the root is the read-only overlay (/opt lives in RAM then: the update would be
gone at the next reboot) and off a Wi-Fi network (the hotspot has no route out). The service
file is never touched by an update (pi/setup.sh installs it). The sum protects against a
broken download, not against a site that was taken over: that is what TLS is for, and a
signed release would be the next step. The service runs as root, as the file moves need."""

import ctypes
import ctypes.util
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request

from . import VERSION, checks, hotspot, inline
from .term.release import base_url, sum_for

ROOT = "/opt/flugato"
TARBALL = "flugato-device.tar.gz"
MAX_SIZE = 20 << 20           # the download; a release is ~150 kB
MAX_UNPACKED = 64 << 20       # the members' sizes together
MAX_MEMBERS = 2000
PREVIEW_TIMEOUT = 60
RUNNING = ("checking", "downloading", "verifying", "trying", "installing", "restarting")
MARKER = ".just-updated"       # in root: written before the restart, counts the starts since
BAD_STARTS = 3                 # that many short-lived starts after an update roll back
SETTLED_AFTER = 90             # seconds alive after which a start counts as good
RENAME_EXCHANGE = 2
AT_FDCWD = -100


def newer(a, b):
    """True when dotted version a is newer than b; anything not digits and dots is not."""
    if not (valid(a) and valid(b)):
        return False
    return tuple(int(x) for x in a.split(".")) > tuple(int(x) for x in b.split("."))


def valid(version):
    return isinstance(version, str) and re.fullmatch(r"\d{1,4}(\.\d{1,4}){0,3}", version) is not None


def tree_version(path):
    """VERSION from a tree's __init__.py without importing it; None when it has none (the
    trees from before 0.5.0)."""
    try:
        with open(os.path.join(path, "__init__.py")) as f:
            m = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(1 << 16), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def read_mounts():
    try:
        with open("/proc/mounts") as f:
            return f.read()
    except OSError:
        return ""


def fetch(url, limit=MAX_SIZE, timeout=20, progress=None):
    """GET url, at most `limit` bytes; progress(done_bytes, total_or_None) per chunk."""
    req = urllib.request.Request(url, headers={"User-Agent": f"flugato-device/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        total = r.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None
        if total is not None and total > limit:
            raise ValueError(f"the file is too large ({total} bytes)")
        chunks, done = [], 0
        while True:
            chunk = r.read(min(65536, limit + 1 - done))
            if not chunk:
                break
            done += len(chunk)
            if done > limit:
                raise ValueError(f"the file is too large (over {limit} bytes)")
            chunks.append(chunk)
            if progress:
                progress(done, total)
    return b"".join(chunks)


def safe_members(tar):
    """The tarball's members, or raises: plain files and directories only, under flugato/ or
    pi/, or LICENSE; nothing absolute, nothing with .., no links; a bounded count and size."""
    members = tar.getmembers()
    if len(members) > MAX_MEMBERS:
        raise ValueError(f"too many members in the release ({len(members)})")
    total = 0
    for m in members:
        name = m.name
        parts = name.split("/")
        if (name.startswith("/") or ".." in parts or not parts[0]
                or not (m.isfile() or m.isdir())):
            raise ValueError(f"unexpected member in the release: {name}")
        if not (parts[0] in ("flugato", "pi") or name == "LICENSE"):
            raise ValueError(f"unexpected member in the release: {name}")
        total += max(m.size, 0)
        if total > MAX_UNPACKED:
            raise ValueError("the release unpacks to too much")
    if not any(m.name == "flugato/__main__.py" for m in members):
        raise ValueError("the release has no flugato/__main__.py")
    return members, total


def exchange(a, b):
    """Swaps two paths in one syscall (renameat2 with RENAME_EXCHANGE: both exist before and
    after, never a moment with one missing). Falls back to three renames where the libc or
    the kernel has no exchange."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        fn = libc.renameat2
    except (OSError, AttributeError):
        fn = None
    if fn:
        fn.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        if fn(AT_FDCWD, os.fsencode(a), AT_FDCWD, os.fsencode(b), RENAME_EXCHANGE) == 0:
            return
        err = ctypes.get_errno()
        if err not in (38, 22, 95):           # ENOSYS, EINVAL, EOPNOTSUPP: fall back
            raise OSError(err, os.strerror(err), a)
    swap = a + ".swap"
    shutil.rmtree(swap, ignore_errors=True)
    os.rename(a, swap)
    try:
        os.rename(b, a)
    except OSError:
        os.rename(swap, a)
        raise
    os.rename(swap, b)


def fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fmt_size(n):
    return f"{n / 1024:.0f} kB" if n < 1 << 20 else f"{n / (1 << 20):.1f} MB"


class Updater:
    """One per app; both web UIs show it. root: where the tree is installed (/opt/flugato,
    the package at root/flugato). base: the site (FLUGATO_URL or flugato.com). client():
    hotspot.client, the Wi-Fi network the Pi is on or None. mounts(): /proc/mounts' text.
    python: the interpreter for the smoke test. on_restart(): the app's restart (SIGTERM;
    systemd brings the new tree up)."""

    def __init__(self, root=ROOT, base=None, client=hotspot.client, mounts=read_mounts,
                 python=sys.executable, on_restart=None, clock=time.time, run=checks.run):
        self.root, self.base_given, self.client, self.mounts = root, base, client, mounts
        self.python, self.on_restart, self.clock, self.run = python, on_restart, clock, run
        self.version = VERSION
        self._lock = threading.Lock()      # the state dict
        self._op = threading.Lock()        # one check, install or roll back at a time
        self._state = {"phase": "idle", "text": "", "available": None, "checked_at": None,
                       "error": None}
        self.thread = None

    @property
    def base(self):
        return (self.base_given or base_url()).rstrip("/")

    # --- state ---------------------------------------------------------------------------
    def state(self):
        with self._lock:
            return dict(self._state)

    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)

    @property
    def busy(self):
        return self.state()["phase"] in RUNNING

    @property
    def tree(self):
        return os.path.join(self.root, "flugato")

    @property
    def prev(self):
        return os.path.join(self.root, "flugato.prev")

    @property
    def marker(self):
        return os.path.join(self.root, MARKER)

    def previous(self):
        """The version kept as flugato.prev: a string, "an earlier version" when that tree
        has no number, or None when there is none."""
        if not os.path.isdir(self.prev):
            return None
        return tree_version(self.prev) or "an earlier version"

    def blocked(self):
        """Why an update cannot run now, or None."""
        if not os.path.isdir(self.tree):
            return f"fluGATO is not installed in {self.root} (running from a checkout?)."
        if inline.root_in_ram(self.mounts()):
            return ("The root is read-only (pi/readonly.sh): an update would be gone at the next "
                    "reboot. Turn it off, reboot, then update.")
        if not self.client():
            return "Join a Wi-Fi network with internet to check for updates: the hotspot has no way out."
        return None

    # --- check -----------------------------------------------------------------------------
    def check(self):
        """Asks the site for the newest version; returns the state. Synchronous (one tiny
        file, 10 s at most). Nothing happens while an install or a roll back runs."""
        if not self._op.acquire(blocking=False):
            return self.state()
        try:
            self._set(phase="checking", text="asking the site…", error=None)
            try:
                new = fetch(self.base + "/releases/latest", limit=64, timeout=10).decode().strip()
                if not valid(new):
                    raise ValueError("the site's latest version is not a version number")
            except (urllib.error.URLError, OSError, ValueError, UnicodeDecodeError) as e:
                self._set(phase="idle", text="", error=f"cannot reach {self.base}: {reason(e)}",
                          checked_at=self.clock())
                return self.state()
            if newer(new, self.version):
                self._set(phase="idle", text=f"{new} is out (this is {self.version})", available=new,
                          checked_at=self.clock())
            else:
                self._set(phase="idle", text=f"{self.version} is the latest", available=None,
                          checked_at=self.clock())
            return self.state()
        finally:
            self._op.release()

    # --- install -----------------------------------------------------------------------------
    def install(self, version=None):
        """Starts the install in a thread; False when nothing starts (busy, blocked, no
        version known)."""
        version = version or self.state()["available"]
        if not valid(version) or self.blocked():
            return False
        if not self._op.acquire(blocking=False):
            return False
        self._set(phase="downloading", text=f"downloading {version}…", error=None)
        self.thread = threading.Thread(target=self._install, args=(version,), name="update", daemon=True)
        self.thread.start()
        return True

    def _install(self, version):
        """In its thread, with the operation lock held by install()."""
        stage = os.path.join(self.root, ".update")
        try:
            try:
                data = self._download(version)
                self._set(phase="verifying", text="unpacking and checking…")
                shutil.rmtree(stage, ignore_errors=True)
                os.makedirs(stage)
                path = os.path.join(stage, TARBALL)
                with open(path, "wb") as f:
                    f.write(data)
                with tarfile.open(path) as tar:
                    _, total = safe_members(tar)
                    free = shutil.disk_usage(self.root).free
                    if free < 3 * total + (8 << 20):
                        raise ValueError(f"not enough space on the card ({fmt_size(free)} free)")
                    tar.extractall(stage, filter="data")
                os.unlink(path)
                got = tree_version(os.path.join(stage, "flugato"))
                if got != version:
                    raise ValueError(f"the release says it is {got or 'no version'}, not {version}")
                self._set(phase="trying", text=f"trying {version}: drawing a screen with it…")
                self._smoke_test(stage)
                self._set(phase="installing", text=f"installing {version}…")
                self._swap_in(stage)
                shutil.rmtree(stage, ignore_errors=True)
                self._mark_updated()
                os.sync()
            except (urllib.error.URLError, OSError, ValueError, tarfile.TarError,
                    subprocess.SubprocessError, UnicodeDecodeError) as e:
                shutil.rmtree(stage, ignore_errors=True)
                self._set(phase="error", text="", error=f"update to {version} failed: {reason(e)}")
                return
            self._set(phase="restarting", text=f"{version} installed: restarting…", available=None)
        finally:
            self._op.release()
        if self.on_restart:
            self.on_restart()

    def _download(self, version):
        sums = fetch(f"{self.base}/releases/{version}/SHA256SUMS", limit=4096).decode()
        want = sum_for(sums, TARBALL)
        if not want:
            raise ValueError(f"SHA256SUMS has no line for {TARBALL}")

        def progress(done, total):
            self._set(text=f"downloading {version}: {fmt_size(done)}" + (f" of {fmt_size(total)}" if total else ""))

        data = fetch(f"{self.base}/releases/{version}/{TARBALL}", progress=progress)
        got = hashlib.sha256(data).hexdigest()
        if got != want:
            raise ValueError(f"sha256 mismatch: got {got[:12]}…, SHA256SUMS says {want[:12]}…")
        return data

    def _smoke_test(self, stage):
        """The new tree draws a preview screen on its own: every module imports, Pillow and
        the fonts work. Runs with the stage as the working directory, so `-m flugato` is the
        new tree and not the installed one."""
        png = os.path.join(stage, "check.png")
        env = dict(os.environ, PYTHONPATH="")
        out = subprocess.run([self.python, "-m", "flugato", "--preview", png, "--scenario", "good"],
                             capture_output=True, text=True, cwd=stage, env=env, timeout=PREVIEW_TIMEOUT)
        if out.returncode or not os.path.exists(png):
            tail = (out.stderr or out.stdout).strip().splitlines()[-1:] or ["no output"]
            raise ValueError(f"the new version does not run: {tail[0]}")
        os.unlink(png)

    def _swap_in(self, stage):
        """The staged tree becomes the installed one in one exchange; the old one, now in the
        stage, becomes flugato.prev."""
        new = os.path.join(stage, "flugato")
        exchange(new, self.tree)                 # root/flugato is the new tree from here on
        shutil.rmtree(self.prev, ignore_errors=True)
        os.rename(new, self.prev)
        fsync_dir(self.root)

    def _mark_updated(self):
        with open(self.marker, "w") as f:
            f.write("0\n")

    # --- roll back -----------------------------------------------------------------------
    def rollback(self, restart=True):
        """The kept tree back in place, the current one kept instead (one exchange); then a
        restart. False when there is nothing to go back to or an install is running."""
        if not os.path.isdir(self.prev):
            return False
        if not self._op.acquire(blocking=False):
            return False
        try:
            was = self.previous()
            try:
                exchange(self.tree, self.prev)
                fsync_dir(self.root)
                os.sync()
            except OSError as e:
                self._set(phase="error", text="", error=f"roll back failed: {reason(e)}")
                return False
            self._set(phase="restarting", text=f"back to {was}: restarting…", available=None, error=None)
        finally:
            self._op.release()
        if restart and self.on_restart:
            self.on_restart()
        return True

    # --- the first starts after an update ----------------------------------------------------
    def boot_guard(self, log=print):
        """Called at start-up. After an update the marker counts the starts; the start that
        lives SETTLED_AFTER seconds removes it (settle, on a timer). BAD_STARTS short-lived
        starts in a row mean the new version dies young: back to the previous one, and this
        process exits so systemd starts it. Returns True when it rolled back."""
        try:
            with open(self.marker) as f:
                count = int(f.read().strip() or 0)
        except (OSError, ValueError):
            return False
        count += 1
        if count >= BAD_STARTS and os.path.isdir(self.prev):
            try:
                os.unlink(self.marker)
            except OSError:
                pass
            log(f"update: {count - 1} starts died young since the update: rolling back to "
                f"{self.previous()}", flush=True)
            if self.rollback(restart=False):
                return True
        try:
            with open(self.marker, "w") as f:
                f.write(f"{count}\n")
        except OSError:
            pass
        timer = threading.Timer(SETTLED_AFTER, self.settle)
        timer.daemon = True
        timer.start()
        return False

    def settle(self):
        """This start lived long enough: the update is good."""
        try:
            os.unlink(self.marker)
        except OSError:
            pass


def reason(e):
    """An exception as one short line for the page."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return str(e.reason)
    if isinstance(e, subprocess.TimeoutExpired):
        return "the smoke test timed out"
    return str(e) or e.__class__.__name__
