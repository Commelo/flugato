import hashlib
import http.server
import importlib.util
import os
import threading


class Site(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(directory):
    handler = lambda *a, **k: Site(*a, directory=directory, **k)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def make_release(root, version, body=b"PK\x05\x06" + bytes(18)):
    d = os.path.join(root, "releases", version)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "flugato.pyz"), "wb") as f:
        f.write(body)
    with open(os.path.join(d, "SHA256SUMS"), "w") as f:
        f.write(f"{hashlib.sha256(body).hexdigest()}  flugato.pyz\n")
    with open(os.path.join(root, "releases", "latest"), "w") as f:
        f.write(version + "\n")


def _build_release_module():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools", "build_release.py")
    spec = importlib.util.spec_from_file_location("flugato_test_build_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_device_release(root, version, tree_dir):
    package = os.path.join(tree_dir, "flugato")
    pi = os.path.join(tree_dir, "pi")
    os.makedirs(package, exist_ok=True)
    os.makedirs(pi, exist_ok=True)
    with open(os.path.join(package, "__init__.py"), "w") as f:
        f.write(f'VERSION = "{version}"\n')
    with open(os.path.join(package, "__main__.py"), "w") as f:
        f.write("""import sys

if len(sys.argv) == 5 and sys.argv[1] == "--preview" and sys.argv[3:] == ["--scenario", "good"]:
    with open(sys.argv[2], "wb") as preview:
        preview.write(b"preview")
else:
    raise SystemExit(2)
""")
    for name in ("flugato.service", "flugato.conf.example"):
        with open(os.path.join(pi, name), "w") as f:
            f.write("test fixture\n")
    with open(os.path.join(tree_dir, "LICENSE"), "w") as f:
        f.write("test fixture\n")

    release = os.path.join(root, "releases", version)
    os.makedirs(release, exist_ok=True)
    target = os.path.join(release, "flugato-device.tar.gz")
    build_release = _build_release_module()
    build_release.device_archive(target, tree_dir)
    with open(target, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    with open(os.path.join(release, "SHA256SUMS"), "w") as f:
        f.write(f"{digest}  flugato-device.tar.gz\n")
    with open(os.path.join(root, "releases", "latest"), "w") as f:
        f.write(version + "\n")
