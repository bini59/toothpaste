#!/bin/sh
# TP-186: export HEAD's tree for the public repository into an empty folder (git archive: .gitattributes export-ignore
# leaves out deploy.yml), then run scripts/oss-check.sh there. Fails (exit 1) when the check finds anything.
# Usage: scripts/export-public.sh <empty folder>
set -eu
dst=${1:?usage: scripts/export-public.sh <empty folder>}
src=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$dst"
[ -z "$(ls -A "$dst")" ] || { echo "export-public: $dst is not empty" >&2; exit 2; }
git -C "$src" archive --format=tar HEAD | tar -x -C "$dst"
# oss-check scans with git grep: a throwaway repository with no commits, removed afterwards
git -C "$dst" init -q
ok=0
(cd "$dst" && scripts/oss-check.sh) || ok=$?
rm -rf "$dst/.git"
[ "$ok" -eq 0 ] || { echo "export-public: oss-check failed in $dst" >&2; exit 1; }
echo "export-public: $(git -C "$src" rev-parse --short HEAD) -> $dst"
