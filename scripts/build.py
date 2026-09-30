#!/usr/bin/env python3
"""Build the per-OS plugin files that Orca Cloud expects into dist/.

The plugin is a single source file. Each build differs only in its filename suffix,
which Orca Cloud uses to tell platform builds apart, and in the BUILD_TARGET it
reports in the log.

    python scripts/build.py                        # every target
    python scripts/build.py --target linux_arm64   # one target
"""

import argparse
import shutil
import sys
from pathlib import Path

import check_version

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
MARKER = 'BUILD_TARGET = "any"\n'
TARGETS = (
    "win_x86_64",
    "win_arm64",
    "linux_x86_64",
    "linux_arm64",
    "macosx_arm64",
    "macosx_x86_64",
)


def build(targets):
    check_version.main(["build.py"])
    text = check_version.PLUGIN.read_text(encoding="utf-8")
    if text.count(MARKER) != 1:
        sys.exit(f"error: expected exactly one {MARKER.strip()!r} line in the plugin")
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir()
    for target in targets:
        out = DIST / f"spoolio_{target}.py"
        stamped = text.replace(MARKER, f'BUILD_TARGET = "{target}"\n')
        out.write_text(stamped, encoding="utf-8", newline="\n")
        print(f"built {out.relative_to(ROOT)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--target", action="append", choices=TARGETS, help="build only this target")
    args = parser.parse_args()
    build(tuple(args.target) if args.target else TARGETS)


if __name__ == "__main__":
    main()
