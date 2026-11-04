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
ORCH="/opt/proj/orchestrator"  # pretend orchestrator dir; managed commands live under it
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

echo "== statusLine: install, wrap an existing one, stay idempotent (synapse#31) =="
SL="$ORCH/.venv/bin/python $ORCH/fleet/statusline.py"
jq --arg sl "$SL" '. + {statusLine: {type: "command", command: $sl, padding: 0}}' "$TMP/fragment.json" > "$TMP/frag_sl.json"
merge_sl() { jq --arg orch "$ORCH" --argjson fleet true --slurpfile frag "$TMP/frag_sl.json" -f "$FILTER" "$1"; }
merge_sl_off() { jq --arg orch "$ORCH" --argjson fleet false --slurpfile frag "$TMP/frag_sl.json" -f "$FILTER" "$1"; }
b64d() { printf '%s' "$1" | base64 -d 2>/dev/null || printf '%s' "$1" | base64 -D; }
USER_SL='bash -c "echo \"$(whoami) | x\"" ~/.claude/my statusline.sh'
jq -n --arg c "$USER_SL" '{statusLine: {type: "command", command: $c, padding: 2, refreshInterval: 5}}' > "$TMP/sl_user.json"
if merge_sl "$TMP/empty.json" > "$TMP/sl1.json" 2> "$TMP/esl1"; then
  assert "no existing statusLine: ours installed as-is" "$(jq -r '.statusLine.command' "$TMP/sl1.json")" "$SL"
else bad "statusLine merge onto {} failed: $(cat "$TMP/esl1")"; fi
if merge_sl "$TMP/sl_user.json" > "$TMP/sl2.json" 2> "$TMP/esl2"; then
  B64=$(jq -r '.statusLine.command' "$TMP/sl2.json" | sed -n 's/.*--wrap-b64 \([A-Za-z0-9+/=]*\).*/\1/p')
  assert "existing statusLine wrapped, not replaced (decodes back exactly)" \
    "$(printf '%s' "$B64" | base64 -d 2>/dev/null || printf '%s' "$B64" | base64 -D)" "$USER_SL"
  assert "wrapped command starts with ours" "$(jq -r '.statusLine.command' "$TMP/sl2.json" | cut -c1-${#SL})" "$SL"
  assert "user's padding kept" "$(jq -r '.statusLine.padding' "$TMP/sl2.json")" "2"
  assert "user's refreshInterval kept" "$(jq -r '.statusLine.refreshInterval' "$TMP/sl2.json")" "5"
else bad "statusLine wrap merge failed: $(cat "$TMP/esl2")"; fi
if merge_sl "$TMP/sl2.json" > "$TMP/sl3.json" 2> "$TMP/esl3"; then
  assert "re-run keeps the same wrapped command (idempotent, no double wrap)" \
    "$(jq -r '.statusLine.command' "$TMP/sl3.json")" "$(jq -r '.statusLine.command' "$TMP/sl2.json")"
  assert "re-run: exactly one --wrap-b64" "$(jq -r '.statusLine.command' "$TMP/sl3.json" | grep -o -- '--wrap-b64' | wc -l | tr -d ' ')" "1"
else bad "statusLine re-run merge failed: $(cat "$TMP/esl3")"; fi
if merge_sl "$TMP/sl1.json" > "$TMP/sl4.json" 2> "$TMP/esl4"; then
  assert "re-run over our own bare statusLine: unchanged, no wrap of ourselves" \
    "$(jq -r '.statusLine.command' "$TMP/sl4.json")" "$SL"
else bad "statusLine re-run over ours failed: $(cat "$TMP/esl4")"; fi
if merge "$TMP/sl_user.json" > "$TMP/sl5.json" 2> "$TMP/esl5"; then
  assert "a fragment without statusLine leaves the user's untouched" \
    "$(jq -r '.statusLine.command' "$TMP/sl5.json")" "$USER_SL"
else bad "merge without fragment statusLine failed: $(cat "$TMP/esl5")"; fi

echo "== statusLine: a user script that merely has fleet/statusline.py in its path is wrapped =="
jq -n '{statusLine: {type: "command", command: "python ~/tools/fleet/statusline.py --theme dark"}}' > "$TMP/sl_lookalike.json"
if merge_sl "$TMP/sl_lookalike.json" > "$TMP/sl6.json" 2> "$TMP/esl6"; then
  B64=$(jq -r '.statusLine.command' "$TMP/sl6.json" | sed -n 's/.*--wrap-b64 \([A-Za-z0-9+/=]*\).*/\1/p')
  assert "look-alike user command kept (wrapped)" "$(printf '%s' "$B64" | base64 -d 2>/dev/null || printf '%s' "$B64" | base64 -D)" "python ~/tools/fleet/statusline.py --theme dark"
else bad "look-alike merge failed: $(cat "$TMP/esl6")"; fi
echo '{"statusLine": "echo hi"}' > "$TMP/sl_str.json"
if merge_sl "$TMP/sl_str.json" > "$TMP/sl7.json" 2> "$TMP/esl7"; then
  assert "a non-object statusLine is left untouched (no jq error)" "$(jq -c '.statusLine' "$TMP/sl7.json")" '"echo hi"'
else bad "non-object statusLine made jq fail: $(cat "$TMP/esl7")"; fi
OTHER="/old/proj/orchestrator/.venv/bin/python /old/proj/orchestrator/fleet/statusline.py --wrap-b64 ZWNobyBoaQ=="
jq -n --arg c "$OTHER" '{statusLine: {type: "command", command: $c}}' > "$TMP/sl_moved.json"
if merge_sl "$TMP/sl_moved.json" > "$TMP/sl8.json" 2> "$TMP/esl8"; then
  assert "ours from a moved orchestrator dir is re-prefixed, wrapped command kept" \
    "$(jq -r '.statusLine.command' "$TMP/sl8.json")" "$SL --wrap-b64 ZWNobyBoaQ=="
else bad "moved-dir merge failed: $(cat "$TMP/esl8")"; fi

jq -n --arg c "$SL --wrap-b64 ZWNobyBoaQ==  " '{statusLine: {type: "command", command: $c}}' > "$TMP/sl_trail.json"
if merge_sl "$TMP/sl_trail.json" > "$TMP/sl9.json" 2> "$TMP/esl9"; then
  assert "ours with trailing whitespace is still ours (not wrapped inside ourselves)" \
    "$(jq -r '.statusLine.command' "$TMP/sl9.json")" "$SL --wrap-b64 ZWNobyBoaQ=="
else bad "trailing-space merge failed: $(cat "$TMP/esl9")"; fi

echo "== statusLine: Fleet off installs nothing and takes ours back out (#144 review S2-1) =="
if merge_sl_off "$TMP/empty.json" > "$TMP/o1.json" 2> "$TMP/eo1"; then
  assert "Fleet off, no statusLine: none installed" "$(jq -c '.statusLine' "$TMP/o1.json")" "null"
else bad "Fleet-off merge onto {} failed: $(cat "$TMP/eo1")"; fi
if merge_sl_off "$TMP/sl_user.json" > "$TMP/o2.json" 2> "$TMP/eo2"; then
  assert "Fleet off: the user's statusLine is left exactly as it was" \
    "$(jq -cS '.statusLine' "$TMP/o2.json")" "$(jq -cS '.statusLine' "$TMP/sl_user.json")"
else bad "Fleet-off merge over the user's failed: $(cat "$TMP/eo2")"; fi
if merge_sl_off "$TMP/sl2.json" > "$TMP/o3.json" 2> "$TMP/eo3"; then
  assert "Fleet off over a wrap: the user's statusLine comes back exactly (command, padding, refreshInterval)" \
    "$(jq -cS '.statusLine' "$TMP/o3.json")" "$(jq -cS '.statusLine' "$TMP/sl_user.json")"
else bad "Fleet-off unwrap failed: $(cat "$TMP/eo3")"; fi
if merge_sl_off "$TMP/sl1.json" > "$TMP/o4.json" 2> "$TMP/eo4"; then
  assert "Fleet off over our own bar (nothing wrapped): statusLine removed" "$(jq 'has("statusLine")' "$TMP/o4.json")" "false"
else bad "Fleet-off removal failed: $(cat "$TMP/eo4")"; fi
if merge "$TMP/sl2.json" > "$TMP/o5.json" 2> "$TMP/eo5"; then
  assert "a fragment with no statusLine (Fleet on) unwraps too" "$(jq -r '.statusLine.command' "$TMP/o5.json")" "$USER_SL"
else bad "no-statusLine fragment unwrap failed: $(cat "$TMP/eo5")"; fi
if merge_sl_off "$TMP/sl8.json" > "$TMP/o6.json" 2> "$TMP/eo6" && merge_sl_off "$TMP/sl_moved.json" > "$TMP/o7.json" 2>> "$TMP/eo6"; then
  assert "Fleet off: a wrapped 'echo hi' comes back" "$(jq -r '.statusLine.command' "$TMP/o6.json")" "echo hi"
  assert "Fleet off: ours from a moved dir is unwrapped too" "$(jq -r '.statusLine.command' "$TMP/o7.json")" "echo hi"
else bad "Fleet-off unwrap of moved/echo failed: $(cat "$TMP/eo6")"; fi
for w in //// eA; do  # invalid UTF-8; unpadded: neither round-trips, as statusline.py's validate=True refuses them
  jq -n --arg c "$SL --wrap-b64 $w" '{statusLine: {type: "command", command: $c}}' > "$TMP/sl_bad.json"
  if merge_sl_off "$TMP/sl_bad.json" > "$TMP/o8.json" 2> "$TMP/eo8"; then
    assert "Fleet off, a wrap that doesn't round-trip ($w): left as it is" "$(jq -r '.statusLine.command' "$TMP/o8.json")" "$SL --wrap-b64 $w"
  else bad "Fleet-off undecodable failed: $(cat "$TMP/eo8")"; fi
done
jq -n --arg c "echo $(printf '\xef\xbf\xbd') ok" '{statusLine: {type: "command", command: $c}}' > "$TMP/sl_fffd.json"
if merge_sl "$TMP/sl_fffd.json" > "$TMP/o10.json" 2> "$TMP/eo10" && merge_sl_off "$TMP/o10.json" > "$TMP/o11.json" 2>> "$TMP/eo10"; then
  assert "a user line containing U+FFFD is wrapped, then restored (#144 delta S3-A)" \
    "$(jq -cS '.statusLine' "$TMP/o11.json")" "$(jq -cS '.statusLine' "$TMP/sl_fffd.json")"
else bad "U+FFFD round trip failed: $(cat "$TMP/eo10")"; fi
if merge_sl "$TMP/o3.json" > "$TMP/o9.json" 2> "$TMP/eo9"; then
  assert "on, off, on again: the same wrap as the first time" "$(jq -cS '.statusLine' "$TMP/o9.json")" "$(jq -cS '.statusLine' "$TMP/sl2.json")"
else bad "on-off-on failed: $(cat "$TMP/eo9")"; fi

echo "== statusLine: only our exact shape counts as ours (#144 review S2-2) =="
for c in '~/tools/.venv/bin/python ~/tools/fleet/statusline.py' \
         'cd ~/x && ~/x/.venv/bin/python ~/x/fleet/statusline.py' \
         '/a/.venv/bin/python /b/fleet/statusline.py' \
         'cd /x && /x/.venv/bin/python /x/fleet/statusline.py' \
         '/home/u/proj/.venv/bin/python /home/u/proj/fleet/statusline.py'; do
  jq -n --arg c "$c" '{statusLine: {type: "command", command: $c}}' > "$TMP/look.json"
  if merge_sl "$TMP/look.json" > "$TMP/look_on.json" 2> "$TMP/elook" && merge_sl_off "$TMP/look.json" > "$TMP/look_off.json" 2>> "$TMP/elook"; then
    B64=$(jq -r '.statusLine.command' "$TMP/look_on.json" | sed -n 's/.*--wrap-b64 \([A-Za-z0-9+/=]*\).*/\1/p')
    assert "Fleet on wraps, never replaces: $c" "$(b64d "$B64")" "$c"
    assert "Fleet off leaves it alone: $c" "$(jq -r '.statusLine.command' "$TMP/look_off.json")" "$c"
  else bad "look-alike merge failed for $c: $(cat "$TMP/elook")"; fi
done

echo "== the REAL settings.fragment.json, substituted as bootstrap does =="
REAL="$HERE/../settings.fragment.json"
sed "s|\$ORCHESTRATOR_DIR|$ORCH|g" "$REAL" > "$TMP/real_frag.json"
merge_real() { jq --arg orch "$ORCH" --argjson fleet true --slurpfile frag "$TMP/real_frag.json" -f "$FILTER" "$1"; }
merge_real_off() { jq --arg orch "$ORCH" --argjson fleet false --slurpfile frag "$TMP/real_frag.json" -f "$FILTER" "$1"; }
if merge_real "$TMP/sl_user.json" > "$TMP/r1.json" 2> "$TMP/er1" && merge_real "$TMP/r1.json" > "$TMP/r2.json" 2> "$TMP/er2"; then
  assert "real fragment: re-run is byte-identical (idempotent)" "$(cmp -s "$TMP/r1.json" "$TMP/r2.json" && echo same)" "same"
  assert "real fragment: user statusLine wrapped by ours" \
    "$(jq -r '.statusLine.command' "$TMP/r1.json" | grep -c -- "$ORCH/fleet/statusline.py --wrap-b64 ")" "1"
  assert "real fragment: every fleet hook is async" \
    "$(jq '[.hooks[][].hooks[] | select(.command|contains("/fleet/report.py")) | .async] | all' "$TMP/r1.json")" "true"
  assert "real fragment: fleet reporter on 15 events" \
    "$(jq '[.hooks | to_entries[] | select(any(.value[].hooks[]; .command|contains("/fleet/report.py")))] | length' "$TMP/r1.json")" "15"
  assert "real fragment: the live-output watch hook is synchronous (its watchPaths are read)" \
    "$(jq '[.hooks.SessionStart[].hooks[] | select(.command|contains("/fleet/live.py --watch")) | (.async // false)] == [false]' "$TMP/r1.json")" "true"
  assert "real fragment: FileChanged has no matcher (for FileChanged a matcher is paths to watch)" \
    "$(jq '[.hooks.FileChanged[] | has("matcher")] | any' "$TMP/r1.json")" "false"
else bad "real fragment merge failed: $(cat "$TMP/er1" "$TMP/er2")"; fi
if merge_real_off "$TMP/r1.json" > "$TMP/r3.json" 2> "$TMP/er3" && merge_real_off "$TMP/empty.json" > "$TMP/r4.json" 2>> "$TMP/er3"; then
  assert "real fragment, Fleet turned off: the user's statusLine comes back exactly" \
    "$(jq -cS '.statusLine' "$TMP/r3.json")" "$(jq -cS '.statusLine' "$TMP/sl_user.json")"
  assert "real fragment, Fleet off from the start: no statusLine" "$(jq 'has("statusLine")' "$TMP/r4.json")" "false"
  assert "real fragment, Fleet off: hooks still merged" \
    "$(count_cmd "$TMP/r4.json" "$ORCH/.venv/bin/python $ORCH/hooks/session_start.py")" "1"
else bad "real fragment Fleet-off merge failed: $(cat "$TMP/er3")"; fi
assert "no fleet argument at all means off (an older bootstrap never installs it)" \
  "$(jq --arg orch "$ORCH" --slurpfile frag "$TMP/real_frag.json" -f "$FILTER" "$TMP/empty.json" | jq 'has("statusLine")')" "false"

echo
echo "passed: $pass   failed: $fail"
[[ "$fail" -eq 0 ]]
