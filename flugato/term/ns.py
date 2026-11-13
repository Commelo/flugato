"""The test ports in their own network namespace, so a test can never touch the rest of the
machine: with root, the supervisor (this process) makes the namespace, moves the wired ports
that do not carry the default route into it, runs the app again inside it (`ip netns exec`)
and, when that run ends, moves the ports back and deletes the namespace. The inner run finds
FLUGATO_NS in its environment. Without root nothing here is used."""

import json
import os
import signal
import subprocess
import sys

from ..checks import run

NAME = "flugato"
ENV_INSIDE, ENV_PORTS, ENV_HOST = "FLUGATO_NS", "FLUGATO_NS_PORTS", "FLUGATO_HOST_PORTS"


def is_root():
    return os.geteuid() == 0


def inside():
    return os.environ.get(ENV_INSIDE) == "1"


def ns_ports():
    """The ports the supervisor moved in (inside the namespace)."""
    try:
        return json.loads(os.environ.get(ENV_PORTS) or "[]")
    except ValueError:
        return []


def host_ports():
    """What the host had: [{name, kind, note}] from the supervisor (inside the namespace)."""
    try:
        return json.loads(os.environ.get(ENV_HOST) or "[]")
    except ValueError:
        return []


def wired_ports():
    base = "/sys/class/net"
    out = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        path = os.path.join(base, name)
        if name == "lo" or os.path.isdir(os.path.join(path, "wireless")):
            continue
        if os.path.exists(os.path.join(path, "device")):
            out.append(name)
    return out


def wireless_ports():
    base = "/sys/class/net"
    try:
        return sorted(n for n in os.listdir(base) if os.path.isdir(os.path.join(base, n, "wireless")))
    except OSError:
        return []


def default_route_devs():
    """The interfaces carrying a default route: this computer's own connection(s)."""
    try:
        routes = json.loads(run(["ip", "-j", "route", "show", "default"]) or "[]")
    except ValueError:
        routes = []
    return sorted({r.get("dev") for r in routes if r.get("dev")})


def choose_ports(wanted=None, force=False):
    """(ports to move, refused) : the wired ports that are not the computer's own connection,
    or just `wanted`; a port with the default route is refused unless `force`."""
    own = set(default_route_devs())
    wired = wired_ports()
    if wanted:
        if wanted not in wired:
            return [], f"{wanted} is not a wired port here"
        if wanted in own and not force:
            return [], f"{wanted} carries this computer's default route (your own connection); " \
                       "use --force to test it anyway, or pick another port with --port"
        return [wanted], None
    ports = [p for p in wired if p not in own]
    if not ports:
        if wired:
            return [], (f"the only wired port, {wired[0]}, carries your default route; "
                        "flugato --port {wired[0]} --force moves it into the test namespace anyway")
        return [], "no wired port on this computer"
    return ports, None


def leftovers():
    """A namespace left by a crashed run keeps its ports: give them back first."""
    if not os.path.exists(f"/run/netns/{NAME}"):
        return
    links = run(["ip", "-n", NAME, "-j", "link", "show"]) or "[]"
    try:
        names = [l.get("ifname") for l in json.loads(links)]
    except ValueError:
        names = []
    for name in names:
        if name and name != "lo":
            run(["ip", "-n", NAME, "link", "set", name, "netns", "1"])
    run(["ip", "netns", "delete", NAME])


def self_command():
    """How to run this program again: the zipapp file, or the package from the source tree."""
    main = sys.argv[0]
    if main and os.path.isfile(main) and not main.endswith("__main__.py"):
        return [sys.executable, os.path.abspath(main)]
    return [sys.executable, "-m", "flugato.term"]


def self_hint():
    """The command to show a person: `~/.local/bin/flugato` for the installed file."""
    cmd = self_command()
    if len(cmd) == 2 and cmd[1].endswith(".pyz") or (len(cmd) == 2 and os.path.isfile(cmd[1]) and "-m" not in cmd):
        home = os.path.expanduser("~")
        path = cmd[1]
        return "~" + path[len(home):] if path.startswith(home + "/") else path
    return "python3 -m flugato.term"


def supervise(ports, argv, host_ports_info):
    """Moves `ports` into the namespace, runs this program inside it with `argv` and cleans up.
    Returns the inner run's exit code."""
    if run(["ip", "netns", "list"]) is None:
        sys.stderr.write("flugato: `ip netns` is not available (iproute2 needed)\n")
        return 4
    leftovers()
    if run(["ip", "netns", "add", NAME]) is None:
        sys.stderr.write("flugato: could not make the network namespace\n")
        return 4
    moved = []
    try:
        run(["ip", "-n", NAME, "link", "set", "lo", "up"])
        for p in ports:
            if run(["ip", "link", "set", p, "netns", NAME]) is not None:
                moved.append(p)
                run(["ip", "-n", NAME, "link", "set", p, "up"])
        if not moved:
            sys.stderr.write("flugato: could not move any port into the namespace\n")
            return 4
        env = dict(os.environ, **{ENV_INSIDE: "1", ENV_PORTS: json.dumps(moved),
                                  ENV_HOST: json.dumps(host_ports_info)})
        cmd = ["ip", "netns", "exec", NAME] + self_command() + argv
        old = signal.signal(signal.SIGINT, signal.SIG_IGN)     # the child gets Ctrl-C, we wait
        try:
            child = subprocess.Popen(cmd, env=env)
            try:
                return child.wait()
            except KeyboardInterrupt:
                child.terminate()
                return child.wait()
        finally:
            signal.signal(signal.SIGINT, old)
    finally:
        for p in moved:
            run(["ip", "-n", NAME, "link", "set", p, "netns", "1"])
        run(["ip", "netns", "delete", NAME])
        for p in moved:
            run(["ip", "link", "set", p, "up"])
