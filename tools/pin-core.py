#!/usr/bin/env python3
"""Write bin/klatalk's SHA-256 into every plugin directory (core.sha256).

A plugin install pins the plugin directory (Hermes: a commit SHA; OpenClaw:
a checkout) — the CLI copy it runs is installed separately and can be
swapped. The digest file travels inside the pinned directory, and each
adapter verifies the CLI's bytes against it before executing a line of it.
Run after every change to bin/klatalk; tests/test_klatalk.py fails when
the two drift."""
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(ROOT, "bin", "klatalk")
PLUGINS = [os.path.join(ROOT, "plugins", "hermes", "klatalk"),
           os.path.join(ROOT, "plugins", "openclaw", "klatalk")]


def digest():
    with open(CORE, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main(check=False):
    d = digest()
    stale = []
    for p in PLUGINS:
        path = os.path.join(p, "core.sha256")
        cur = ""
        try:
            with open(path, encoding="utf-8") as f:
                cur = f.read().split()[0]
        except (OSError, IndexError):
            pass
        if cur != d:
            stale.append(path)
            if not check:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"{d}  bin/klatalk\n")
    if check and stale:
        print("core.sha256 is stale in: " + ", ".join(stale) + " — run tools/pin-core.py")
        return 1
    print(d + ("  (stale: %d)" % len(stale) if check else "  written to %d plugin dir(s)" % len(PLUGINS)))
    return 0


if __name__ == "__main__":
    sys.exit(main(check="--check" in sys.argv[1:]))
