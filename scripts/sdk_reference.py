"""Generate portable SDK reference MDX, or check that it matches the source.

Install Python's docs dependency group and run npm ci first. Uses Sphinx's JSON
builder in a temporary directory and the pinned Mintlify converter. Node.js 22
or 24 LTS is recommended. No Sphinx caches or absolute build paths are committed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check committed MDX without writing")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="tazo-sdk-reference-") as directory:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "sphinx",
                "-W",
                "-E",
                "-a",
                "-b",
                "json",
                str(ROOT / "docs/source"),
                directory,
            ],
            check=True,
        )
        subprocess.run(
            [
                "node",
                str(ROOT / "scripts/sdk_reference.mjs"),
                directory,
                *(["--check"] if args.check else []),
            ],
            check=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
