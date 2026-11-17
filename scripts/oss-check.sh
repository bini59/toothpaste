#!/bin/sh
# Pre-publication check (TP-158): fails when tracked or new, non-ignored files contain personal or company traces or
# token-shaped strings. Run from anywhere: scripts/oss-check.sh
set -u
cd "$(dirname "$0")/.." || exit 2

# personal / company names (case-insensitive) and macOS home paths
NAMES='bini59|kevin|counterpoint|cp-research'
# company repository / project names (TP-184), as whole words: "14_Zeus" hits, "supernova" does not
WORDS='(^|[^a-z])(zeus|mimir|pulsar|aurora|nova|europe|orion|janus|lyra)([^a-z]|$)|global website'
HOMES='/Users/'
# token shapes: GitHub (ghp_/gho_/...), OpenAI/Anthropic-style sk-, Slack xoxb-/xoxp-, Asana PAT (1/<gid>:<32 hex>)
TOKENS='(^|[^A-Za-z0-9])(gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[bp]-[A-Za-z0-9-]{10,}|[0-9]/[0-9]{6,}(/[0-9]{6,})?:[0-9a-f]{32})'

# Allowed hits, one ERE per line, matched against "path:line:text"
ALLOW='^LICENSE:[0-9]+:Copyright \(c\) [0-9]+ kevin-lim59$
^ticket_pilot/mode\.py:[0-9]+: .*re\.search\(r"localhost\|127\\\.0\\\.0\\\.1\|/Users/\|/opt/homebrew\|/Applications/", ln\)
^ticket_pilot/migrate\.py:[0-9]+:LABELS = .*com\.bini59\.ticket-pilot\.
^launchd/install\.sh:[0-9]+:.*com\.bini59\.ticket-pilot\.
^tests/test_owner_name\.py:[0-9]+:.*com\.bini59\.ticket-pilot\.
^\.github/workflows/deploy\.yml:[0-9]+:    if: github\.repository == '"'"'kevin-lim59/32_Ticket_pilot'"'"'$'
# the public repository (TP-186) may be named anywhere; other names on the same line still count
PUBLIC='bini59/toothpaste'

scan() { git grep -n -I -E --untracked "$@" -- . ':!scripts/oss-check.sh'; }
names() { scan -i "$NAMES" | while IFS= read -r l; do
  printf '%s\n' "$l" | sed "s#$PUBLIC##g" | grep -q -i -E "$NAMES" && printf '%s\n' "$l"; done; }
hits=$( { names; scan -i "$WORDS"; scan "$HOMES"; scan "$TOKENS"; } | sort -u | grep -v -E "$ALLOW")
if [ -n "$hits" ]; then
  printf '%s\n' "$hits"
  echo "oss-check: $(printf '%s\n' "$hits" | wc -l | tr -d ' ') line(s) above; fix them or add an exception in scripts/oss-check.sh" >&2
  exit 1
fi
echo "oss-check: ok"
