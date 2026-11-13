"""Builds a release: the terminal app as one stdlib-only zipapp and the device app as a
tarball, both written where the website serves them. From the repository root:

    python3 tools/build_release.py            # the version in flugato/__init__.py
    python3 tools/build_release.py --check    # build to a temp dir and run both, write nothing

Writes site/releases/<version>/flugato.pyz, flugato-device.tar.gz and SHA256SUMS, and
site/releases/latest (the version number). The zipapp carries flugato/__init__.py, checks.py,
dhcp_probe.py, cable.py, demo.py and flugato/term/*; nothing that needs Pillow or the Pi. The
tarball carries the whole flugato/ package, pi/flugato.service, pi/flugato.conf.example and
LICENSE: what the device's updater (flugato/update.py) installs into /opt/flugato. Both archives
are rebuilt with fixed timestamps and owners, so the same sources give the same bytes and the
same checksums."""

import gzip
import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from flugato import VERSION  # noqa: E402

SHARED = ("__init__.py", "checks.py", "dhcp_probe.py", "cable.py", "demo.py")
MAIN = "import sys\nfrom flugato.term.cli import main\nsys.exit(main())\n"
STAMP = (2026, 1, 1, 0, 0, 0)
STAMP_T = 1767225600                      # the same instant as STAMP, for tar
PYZ, TARBALL = "flugato.pyz", "flugato-device.tar.gz"
DEVICE_FILES = ("pi/flugato.service", "pi/flugato.conf.example", "LICENSE")


def stage(into):
    pkg = os.path.join(into, "flugato")
    os.makedirs(os.path.join(pkg, "term"))
    for name in SHARED:
        shutil.copy(os.path.join(ROOT, "flugato", name), pkg)
    term = os.path.join(ROOT, "flugato", "term")
    for name in sorted(os.listdir(term)):
        if name.endswith(".py"):
            shutil.copy(os.path.join(term, name), os.path.join(pkg, "term"))
    with open(os.path.join(into, "__main__.py"), "w") as f:
        f.write(MAIN)
    shutil.copy(os.path.join(ROOT, "LICENSE"), os.path.join(into, "LICENSE"))


def archive(staged, target):
    """A zipapp with every member stamped the same, so the bytes do not depend on the clock."""
    with open(target, "wb") as f:
        f.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
            for dirpath, dirnames, filenames in os.walk(staged):
                dirnames.sort()
                for name in sorted(filenames):
                    path = os.path.join(dirpath, name)
                    rel = os.path.relpath(path, staged)
                    info = zipfile.ZipInfo(rel, date_time=STAMP)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o644 << 16
                    with open(path, "rb") as src:
                        z.writestr(info, src.read())
    os.chmod(target, 0o755)


def device_members(root=ROOT):
    """(archive name, source path) of everything the device tarball carries, sorted."""
    out = []
    pkg = os.path.join(root, "flugato")
    for dirpath, dirnames, filenames in os.walk(pkg):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith((".pyc", ".pyo")):
                continue
            path = os.path.join(dirpath, name)
            out.append((os.path.relpath(path, root), path))
    for rel in DEVICE_FILES:
        out.append((rel, os.path.join(root, rel)))
    for rel, path in out:
        if not stat.S_ISREG(os.lstat(path).st_mode):   # no links, nothing special
            raise SystemExit(f"not a plain file, not shipped: {rel}")
    return sorted(out)


def device_archive(target, root=ROOT):
    """The device tarball: fixed mtime, owner root, mode 644 (755 for the executables), gzip
    without a timestamp, so the same sources give the same bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel, path in device_members(root):
            info = tar.gettarinfo(path, arcname=rel)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = STAMP_T
            info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
            info.pax_headers = {}
            with open(path, "rb") as src:
                tar.addfile(info, src)
    with open(target, "wb") as f:
        f.write(gzip.compress(buf.getvalue(), mtime=0))   # no file name, no clock in the header


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def build(out_dir):
    """-> {name: (path, digest)} for the zipapp and the tarball; writes SHA256SUMS too."""
    os.makedirs(out_dir, exist_ok=True)
    pyz = os.path.join(out_dir, PYZ)
    with tempfile.TemporaryDirectory() as tmp:
        staged = os.path.join(tmp, "app")
        os.makedirs(staged)
        stage(staged)
        archive(staged, pyz)
    tarball = os.path.join(out_dir, TARBALL)
    device_archive(tarball)
    built = {PYZ: (pyz, sha256(pyz)), TARBALL: (tarball, sha256(tarball))}
    with open(os.path.join(out_dir, "SHA256SUMS"), "w") as f:
        for name in (PYZ, TARBALL):
            f.write(f"{built[name][1]}  {name}\n")
    return built


def check(built):
    """The built files run on their own, away from the source tree: the zipapp answers
    --version and a demo run; the tarball's tree draws a preview screen (the device's own
    smoke test, the one its updater runs before it swaps the code in)."""
    env = dict(os.environ, PYTHONPATH="")
    pyz = built[PYZ][0]
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run([sys.executable, pyz, "--version"], capture_output=True, text=True, cwd=tmp, env=env)
        if out.returncode or VERSION not in out.stdout:
            raise SystemExit(f"the zipapp does not run: {out.stdout}{out.stderr}")
        out = subprocess.run([sys.executable, pyz, "--demo", "good", "--once"], capture_output=True, text=True,
                             cwd=tmp, env=dict(env, XDG_STATE_HOME=tmp))
        if out.returncode or "ALL GOOD" not in out.stdout:
            raise SystemExit(f"the zipapp's demo run failed: {out.stdout}{out.stderr}")
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(built[TARBALL][0]) as tar:
            tar.extractall(tmp, filter="data")
        png = os.path.join(tmp, "check.png")
        out = subprocess.run([sys.executable, "-m", "flugato", "--preview", png, "--scenario", "good"],
                             capture_output=True, text=True, cwd=tmp, env=env, timeout=120)
        if out.returncode or not os.path.exists(png):
            raise SystemExit(f"the device tree does not draw: {out.stdout}{out.stderr}")
    return True


def main(argv):
    if "--check" in argv:
        with tempfile.TemporaryDirectory() as tmp:
            built = build(tmp)
            check(built)
            print(f"ok: flugato {VERSION}, " + ", ".join(
                f"{name} {os.path.getsize(path)} bytes sha256 {digest[:12]}…" for name, (path, digest) in built.items()))
        return
    out_dir = os.path.join(ROOT, "site", "releases", VERSION)
    built = build(out_dir)
    check(built)
    with open(os.path.join(ROOT, "site", "releases", "latest"), "w") as f:
        f.write(VERSION + "\n")
    for name, (path, digest) in built.items():
        print(f"flugato {VERSION}: {os.path.relpath(path, ROOT)} ({os.path.getsize(path)} bytes), sha256 {digest}")


if __name__ == "__main__":
    main(sys.argv[1:])
