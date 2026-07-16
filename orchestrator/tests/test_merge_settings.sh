#!/bin/bash
# test_merge_settings.sh — regression test for the settings-fragment merge
# (orchestrator/lib/merge-settings.jq). Covers #68: the merge must preserve
# operator-added ("foreign") hooks while keeping MS4CC-managed hooks idempotent.
#
# No framework needed — runs the jq filter directly against fixtures. Requires jq.
#   ./orchestrator/tests/test_merge_settings.sh

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
FILTER="$HERE/../lib/merge-settings.jq"
ORCH="/opt/orch"          # pretend orchestrator dir; managed commands live under it
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0
ok()  { echo "  ok   - $1"; pass=$((pass+1)); }
bad() { echo "  FAIL - $1"; fail=$((fail+1)); }
# assert <description> <actual> <expected>
assert() { if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (got '$2', want '$3')"; fi; }

if ! command -v jq >/dev/null 2>&1; then echo "jq not installed"; exit 2; fi

# The fragment, already $ORCHESTRATOR_DIR-substituted (as bootstrap hands it to jq).
cat > "$TMP/fragment.json" <<JSON
{
  "autoCompactEnabled": true,
  "env": { "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "92" },
  "hooks": {
    "SessionStart": [ { "matcher": "*", "hooks": [
      { "type": "command", "command": "$ORCH/.venv/bin/python $ORCH/hooks/session_start.py" } ] } ],
    "Stop": [ { "matcher": "*", "hooks": [
      { "type": "command", "command": "$ORCH/.venv/bin/python $ORCH/hooks/session_end.py" } ] } ]
  }
}
JSON

# Existing settings.json: an operator has added a foreign Stop hook and a foreign
# Notification event, has their own env key, AND carries a STALE managed Stop hook
# from a previous bootstrap run.
cat > "$TMP/settings.json" <<JSON
{
  "env": { "FOO": "bar" },
  "hooks": {
    "Stop": [
      { "matcher": "*", "hooks": [
        { "type": "command", "command": "$ORCH/.venv/bin/python $ORCH/hooks/session_end.py" } ] },
      { "matcher": "*", "hooks": [
        { "type": "command", "command": "/home/user/myhook.sh" } ] }
    ],
    "Notification": [
      { "matcher": "*", "hooks": [
        { "type": "command", "command": "/home/user/notify.sh" } ] }
    ]
  }
}
JSON

merge() {  # merge <in-settings> -> stdout merged json
  jq --arg orch "$ORCH" --slurpfile frag "$TMP/fragment.json" -f "$FILTER" "$1"
}
# count exact command matches across all events
count_cmd() { jq --arg c "$2" '[.hooks[]?[]?.hooks[]?.command] | map(select(. == $c)) | length' "$1"; }

echo "== first merge =="
if ! merge "$TMP/settings.json" > "$TMP/out1.json" 2> "$TMP/err1"; then
  bad "jq merge exited non-zero: $(cat "$TMP/err1")"
else
  ok "jq merge produced valid output"
  assert "foreign Stop hook (/home/user/myhook.sh) preserved" \
    "$(count_cmd "$TMP/out1.json" "/home/user/myhook.sh")" "1"
  assert "foreign Notification event preserved" \
    "$(count_cmd "$TMP/out1.json" "/home/user/notify.sh")" "1"
  assert "managed Stop hook present exactly once (stale stripped, fragment re-added)" \
    "$(count_cmd "$TMP/out1.json" "$ORCH/.venv/bin/python $ORCH/hooks/session_end.py")" "1"
  assert "managed SessionStart hook applied from fragment" \
    "$(count_cmd "$TMP/out1.json" "$ORCH/.venv/bin/python $ORCH/hooks/session_start.py")" "1"
  assert "operator env key preserved" \
    "$(jq -r '.env.FOO' "$TMP/out1.json")" "bar"
  assert "fragment env key merged in" \
    "$(jq -r '.env.CLAUDE_AUTOCOMPACT_PCT_OVERRIDE' "$TMP/out1.json")" "92"
  assert "autoCompactEnabled set from fragment" \
    "$(jq -r '.autoCompactEnabled' "$TMP/out1.json")" "true"
fi

echo "== second merge (idempotency: re-running bootstrap must not stack duplicates) =="
if ! merge "$TMP/out1.json" > "$TMP/out2.json" 2> "$TMP/err2"; then
  bad "second jq merge exited non-zero: $(cat "$TMP/err2")"
else
  ok "second jq merge produced valid output"
  assert "managed Stop hook STILL exactly once after re-run" \
    "$(count_cmd "$TMP/out2.json" "$ORCH/.venv/bin/python $ORCH/hooks/session_end.py")" "1"
  assert "managed SessionStart hook STILL exactly once after re-run" \
    "$(count_cmd "$TMP/out2.json" "$ORCH/.venv/bin/python $ORCH/hooks/session_start.py")" "1"
  assert "foreign Stop hook STILL preserved after re-run" \
    "$(count_cmd "$TMP/out2.json" "/home/user/myhook.sh")" "1"
fi

echo "== empty settings (fresh .hooks) =="
echo '{}' > "$TMP/empty.json"
if merge "$TMP/empty.json" > "$TMP/out3.json" 2> "$TMP/err3"; then
  assert "fragment hooks applied onto empty settings" \
    "$(count_cmd "$TMP/out3.json" "$ORCH/.venv/bin/python $ORCH/hooks/session_start.py")" "1"
else
  bad "merge onto '{}' exited non-zero: $(cat "$TMP/err3")"
fi

echo
echo "passed: $pass   failed: $fail"
[[ "$fail" -eq 0 ]]
