"""Trusted Linux-side supervisor for an isolated command qualification candidate.

Not an admitted live dispatcher backend. Only the controller supplies requests.
Run with Python -I; worker code runs only after bubblewrap establishes namespaces.
"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import selectors
import signal
import subprocess
import sys
import time

BWRAP_SHA256 = "8e19e40e7d5f7a7e8b488c7926feb040eab6ed10c58fa360e266d2f70670e92b"
PYTHON_SHA256 = "fa9796cd3a30878e11a2f40372f773d3fcd913fff35e5bee8dd9a036e22e93ab"
RUNTIME_PATH = Path(__file__).resolve().parents[2] / 'docs/registries/dispatch-wsl-runtime.json'


def runtime_profile():
    profile = json.loads(RUNTIME_PATH.read_text(encoding='utf-8'))
    if (profile.get('schema') != 1 or profile.get('distro') != 'Ubuntu'
            or profile.get('uid') != 1000 or not isinstance(profile.get('mounts'), list)):
        raise ValueError('Unknown runtime profile')
    entries = profile.get('inventory', {}).get('entries', {})
    targets = set()
    for mount in profile['mounts']:
        if not isinstance(mount, dict) or set(mount) != {'source', 'target'}:
            raise ValueError('Invalid runtime mount')
        source, target = mount['source'], mount['target']
        if (not isinstance(source, str) or not isinstance(target, str)
                or source not in entries or entries[source].get('type') != 'file'
                or not target.startswith(('/usr/bin/', '/usr/lib/', '/lib64/'))
                or '..' in target.split('/') or str(PurePosixPath(target)) != target
                or target in targets or any(c in target for c in ('\0', '\\', '\n', '\r'))):
            raise ValueError('Runtime mount exceeds fixed scope')
        targets.add(target)
    expected_links = [{'target':'usr/bin','link':'/bin'},
                      {'target':'python3.14','link':'/usr/bin/python3'},
                      {'target':'bash','link':'/usr/bin/sh'}]
    if profile.get('symlinks') != expected_links:
        raise ValueError('Unexpected runtime aliases')
    if set(entries) != {m['source'] for m in profile['mounts']}:
        raise ValueError('Runtime inventory and mounts differ')
    return profile


def validate_request(raw):
    if not isinstance(raw, dict) or set(raw) != {"root", "argv", "timeout_seconds", "output_limit"}:
        raise ValueError("Invalid contained-command request")
    root = raw["root"]
    if (not isinstance(root, str) or not root.startswith("/mnt/c/") or
            not root.endswith("/worker") or any(c in root for c in ("\0", "\\", "\n", "\r")) or
            ".." in root.split("/") or str(PurePosixPath(root)) != root):
        raise ValueError("Expected exact controller-prepared worker directory")
    args = raw["argv"]
    if (not isinstance(args, list) or not args or len(args) > 100 or
            any(not isinstance(a, str) or "\0" in a for a in args) or
            sum(len(a) for a in args) > 50000 or not args[0].startswith("/")):
        raise ValueError("Expected bounded argv with absolute executable")
    for key, maximum in (("timeout_seconds", 1800), ("output_limit", 4*1024*1024)):
        if type(raw[key]) is not int or not 1 <= raw[key] <= maximum:
            raise ValueError("Invalid command limit")
    return dict(raw)


def sandbox_argv(request):
    profile = runtime_profile()
    args = ["/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session",
            "--cap-drop", "ALL"]
    for mount in profile['mounts']:
        args += ['--ro-bind', mount['source'], mount['target']]
    for link in profile['symlinks']:
        args += ['--symlink', link['target'], link['link']]
    return args + ["--proc", "/proc", "--dev", "/dev",
            "--tmpfs", "/tmp", "--bind", request["root"], "/work",
            "--chdir", "/work", "--clearenv", "--setenv", "PATH", "/usr/bin:/bin",
            "--setenv", "HOME", "/tmp", "--remount-ro", "/", "--", *request["argv"]]


def verify_host(root):
    if sys.platform != "linux" or os.getuid() != 1000:
        raise ValueError("Requires non-root Linux supervisor")
    profile = runtime_profile()
    # Python -I excludes the script directory; load this trusted sibling explicitly.
    spec = importlib.util.spec_from_file_location('dispatch_runtime_inventory',
                                                 Path(__file__).with_name('runtime_inventory.py'))
    inventory = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inventory)
    inventory.verify_inventory(profile['inventory'])
    for filename, expected in (("/usr/bin/bwrap", BWRAP_SHA256), (sys.executable, PYTHON_SHA256)):
        with open(filename, "rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError("Runtime executable changed; requalification required")
    path = Path(root)
    if not path.is_dir() or str(path.resolve()) != root:
        raise ValueError("Worker directory must exist without path redirection")
    for ancestor in [path, *path.parents]:
        if ancestor.is_symlink():
            raise ValueError("Linked worker root")


def supervise(request, control_fd=0):
    verify_host(request["root"])
    started = time.monotonic()
    child = subprocess.Popen(sandbox_argv(request), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True, env={"PATH": "/usr/bin:/bin"})
    output = {"stdout": bytearray(), "stderr": bytearray()}
    reason = None
    sent_kill = False
    control = bytearray()
    try:
        with selectors.DefaultSelector() as events:
            for stream, name in ((child.stdout, "stdout"), (child.stderr, "stderr")):
                events.register(stream, selectors.EVENT_READ, name)
            events.register(control_fd, selectors.EVENT_READ, "control")
            while True:
                if time.monotonic() - started >= request["timeout_seconds"] and reason is None:
                    reason = "timeout"
                if reason and not sent_kill:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    sent_kill = True
                for key, _ in events.select(.05):
                    data = os.read(key.fd, 4096)
                    if key.data == "control":
                        if not data:
                            events.unregister(key.fileobj)
                            reason = reason or "controller_disconnected"
                        else:
                            control.extend(data)
                            if control == b"STOP\n":
                                reason = reason or "cancelled"
                            elif len(control) > 5 or not b"STOP\n".startswith(control):
                                reason = reason or "invalid_control"
                    elif not data:
                        events.unregister(key.fileobj)
                    else:
                        remaining = request["output_limit"] - sum(map(len, output.values()))
                        output[key.data].extend(data[:remaining])
                        if len(data) > remaining:
                            reason = reason or "output_limit"
                if child.poll() is not None and not any(
                        key.data != "control" for key in events.get_map().values()):
                    break
            child.wait()
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        child.stdout.close()
        child.stderr.close()
    return {"status": reason or ("completed" if child.returncode == 0 else "failed"),
            "exit_code": child.returncode, "elapsed_seconds": time.monotonic()-started,
            "stdout_b64": base64.b64encode(output["stdout"]).decode("ascii"),
            "stderr_b64": base64.b64encode(output["stderr"]).decode("ascii"),
            "termination_observed": True}


def main():
    # Unbuffered header reads prevent swallowing a subsequent STOP into a buffer.
    header = bytearray()
    while len(header) <= 65536:
        char = os.read(0, 1)
        if char == b"\n":
            break
        if not char:
            raise ValueError("Missing request")
        header.extend(char)
    else:
        raise ValueError("Request too large")
    result = supervise(validate_request(json.loads(header)))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
