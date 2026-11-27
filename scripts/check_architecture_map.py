"""Require a code map update when a pull request changes code."""

import argparse
import os
from pathlib import PurePosixPath
import subprocess
import sys


ARCHITECTURE_MAP = "docs/architecture.md"

# Keep this scope in sync with docs/architecture.md. Paths are relative to the Git root.
SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts"}
CODE_SUFFIXES = {
    "ticket_pilot": {".py", ".js", ".mjs", ".cjs"},
    "web": {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".css", ".html", ".svg"},
    "web/themes": {".json"},
    "tests": {".py", ".js", ".mjs", ".cjs", ".ts", ".sh"},
    "bin": SCRIPT_SUFFIXES,
    "scripts": SCRIPT_SUFFIXES,
    "docker": {".py", ".sh", ".yml", ".yaml"},
    "launchd": {".sh", ".plist"},
    "systemd": {".sh", ".service", ".timer"},
    ".github/workflows": {".yml", ".yaml"},
}
CODE_FILES = {
    "bin/tp",
    ".dockerignore",
    "docker/Dockerfile",
    "web/package.json",
    "web/package-lock.json",
    "web/tsconfig.json",
}


def is_code(path: str, old_mode: bytes, new_mode: bytes) -> bool:
    file = PurePosixPath(path)
    suffix, parts = file.suffix, file.parts
    if len(parts) == 1 and suffix in SCRIPT_SUFFIXES:
        return True
    if not suffix and b"100755" in (old_mode, new_mode) and (len(parts) == 1 or parts[0] in {"bin", "scripts"}):
        return True
    return path in CODE_FILES or any(
        path.startswith(directory + "/") and suffix in suffixes
        for directory, suffixes in CODE_SUFFIXES.items()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", help="PR target branch or commit")
    parser.add_argument("head", nargs="?", default="HEAD", help="PR head (default: HEAD)")
    args = parser.parse_args()

    # Three-dot diff shows the PR's changes. Disabling rename detection exposes both paths of each move.
    diff = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--no-relative", "--no-renames", "--raw", "--no-abbrev", "-z",
         "--end-of-options", f"{args.base}...{args.head}", "--"],
        stdout=subprocess.PIPE,
    )
    if diff.returncode:
        print("Could not compare the PR commits. Check that both refs and their common history are available.",
              file=sys.stderr)
        return 2

    fields = diff.stdout.split(b"\0")
    code_paths = []
    map_updated = False
    for change, raw_path in zip(fields[::2], fields[1::2]):
        old_mode, new_mode, old_blob, new_blob, _status = change.removeprefix(b":").split()
        path = os.fsdecode(raw_path)
        if path == ARCHITECTURE_MAP:
            # A mode-only change also has status M. Require new Markdown content in a regular file.
            map_updated = new_mode in {b"100644", b"100755"} and old_blob != new_blob
        elif is_code(path, old_mode, new_mode):
            code_paths.append(path)
    if not code_paths or map_updated:
        return 0

    print(f"Update {ARCHITECTURE_MAP} in this PR. Changed code paths:", file=sys.stderr)
    for path in sorted(code_paths):
        print(f"  {path!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
