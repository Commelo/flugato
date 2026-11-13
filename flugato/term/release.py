"""`flugato update` and `flugato uninstall`. A release is one file, flugato.pyz, under
<site>/releases/<version>/ next to its SHA256SUMS; <site>/releases/latest holds the newest
version number. The site is flugato.com unless FLUGATO_URL says otherwise (tests, mirrors)."""

import hashlib
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

from . import SITE, VERSION


def base_url():
    return os.environ.get("FLUGATO_URL", SITE).rstrip("/")


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": f"flugato/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def installed_file():
    """The zipapp this process runs from, or None when running from the source tree."""
    main = sys.argv[0]
    if main and os.path.isfile(main) and zipfile.is_zipfile(main):
        return os.path.realpath(main)
    return None


def latest(base=None):
    return fetch((base or base_url()) + "/releases/latest").decode().strip()


def sum_for(sums_text, name):
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    return None


def download(version, base=None):
    """-> bytes of the verified flugato.pyz for `version`; raises on a bad checksum."""
    base = base or base_url()
    sums = fetch(f"{base}/releases/{version}/SHA256SUMS").decode()
    want = sum_for(sums, "flugato.pyz")
    if not want:
        raise ValueError("SHA256SUMS has no line for flugato.pyz")
    data = fetch(f"{base}/releases/{version}/flugato.pyz")
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise ValueError(f"sha256 mismatch: got {got[:12]}…, SHA256SUMS says {want[:12]}…")
    return data


def update(out=print, target=None, base=None):
    """Replaces the running zipapp with the newest release. Returns an exit code."""
    target = target or installed_file()
    if not target:
        out("flugato update: not running from an installed flugato.pyz (a source checkout?)")
        return 4
    try:
        new = latest(base)
    except (urllib.error.URLError, OSError, ValueError) as e:
        out(f"flugato update: cannot reach {base or base_url()}: {e}")
        return 4
    if new == VERSION:
        out(f"flugato {VERSION} is the latest")
        return 0
    out(f"flugato {new} is out (this is {VERSION}): downloading")
    try:
        data = download(new, base)
    except (urllib.error.URLError, OSError, ValueError) as e:
        out(f"flugato update: {e}")
        return 4
    tmp = tempfile.NamedTemporaryFile(dir=os.path.dirname(target), prefix=".flugato-", delete=False)
    try:
        tmp.write(data)
        tmp.close()
        os.chmod(tmp.name, 0o755)
        os.replace(tmp.name, target)
    except OSError as e:
        os.unlink(tmp.name) if os.path.exists(tmp.name) else None
        out(f"flugato update: cannot write {target}: {e}")
        return 4
    out(f"installed flugato {new} to {target}")
    return 0


def state_dirs():
    home = os.path.expanduser("~")
    return [os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state"), "flugato"),
            os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "flugato")]


def uninstall(out=print, ask=input, yes=False, target=None):
    target = target or installed_file()
    if not target:
        out("flugato uninstall: not running from an installed flugato.pyz")
        return 4
    try:
        os.unlink(target)
        out(f"removed {target}")
    except OSError as e:
        out(f"flugato uninstall: cannot remove {target}: {e}")
        return 4
    dirs = [d for d in state_dirs() if os.path.isdir(d)]
    if dirs:
        if not yes:
            try:
                answer = ask(f"also remove the history and settings ({', '.join(dirs)})? [y/N] ")
            except EOFError:
                answer = ""
            yes = answer.strip().lower().startswith("y")
        if yes:
            for d in dirs:
                shutil.rmtree(d, ignore_errors=True)
            out("removed " + ", ".join(dirs))
        else:
            out("kept " + ", ".join(dirs))
    return 0
