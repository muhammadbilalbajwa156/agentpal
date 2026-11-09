"""Run unchanged full-suite command offline with isolated inherited TCP ports.

Other acceptance worktrees share the host. Legacy T2/T4 fixtures hard-code
8102/8104. Remap just those loopback socket destinations and binds to private
ephemeral ports in this Python process and its Python children. No route,
authentication, policy, provider result or assertion is modified. F1 stays 8205.
"""
from pathlib import Path
import os
import socket
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[3]
    directory = Path("/tmp/m8-f1/suite-network")
    directory.mkdir(parents=True, exist_ok=True)
    held = []
    mapping = {}
    for old in (8102, 8104):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        held.append(sock)
        mapping[old] = sock.getsockname()[1]
    code = '''import socket
PORTS = %r
def address(value):
    if isinstance(value, tuple) and len(value) == 2 and value[0] in ("127.0.0.1", "localhost"):
        return (value[0], PORTS.get(value[1], value[1]))
    return value
bind = socket.socket.bind
connect = socket.socket.connect
connect_ex = socket.socket.connect_ex
def isolated_bind(self, target):
    return bind(self, address(target))
def isolated_connect(self, target):
    return connect(self, address(target))
def isolated_connect_ex(self, target):
    return connect_ex(self, address(target))
socket.socket.bind = isolated_bind
socket.socket.connect = isolated_connect
socket.socket.connect_ex = isolated_connect_ex
''' % mapping
    (directory / "sitecustomize.py").write_text(code)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PAYPAL_", "CHANNEL3_", "KERNEL_", "F1_"))}
    env["PYTHONPATH"] = str(directory) + os.pathsep + env.get("PYTHONPATH", "")
    for sock in held:
        sock.close()
    # Additional pytest reporting options are allowed; the required gate is unchanged.
    return subprocess.call([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                            *sys.argv[1:]], cwd=root, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
