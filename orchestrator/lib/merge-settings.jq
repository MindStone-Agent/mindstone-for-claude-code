# merge-settings.jq — merge the MS4CC settings fragment into an existing
# ~/.claude/settings.json WITHOUT clobbering hooks the operator (or another
# tool) added on top of the framework. Fixes #68.
#
# Consumed by bootstrap.sh AND orchestrator/tests/test_merge_settings.sh, so the
# merge logic lives in exactly one place.
#
# Inputs:
#   .        the existing settings.json object (stdin)
#   $frag    slurpfile of the fragment, already $ORCHESTRATOR_DIR-substituted;
#            the fragment object is $frag[0]
#   $orch    the absolute orchestrator dir. A hook entry is "MS4CC-managed" iff
#            its command string contains "$orch/" (every fragment command is
#            "$ORCHESTRATOR_DIR/.venv/bin/python $ORCHESTRATOR_DIR/hooks/…", so
#            the trailing slash also avoids a "/opt/orch" vs "/opt/orchestra"
#            prefix collision).
#
# .hooks is keyed by event (SessionStart, Stop, …); each event holds an array of
# matcher-groups, each group an array of {type, command} entries. Per event we:
#   * DROP every managed entry from the existing settings, then re-add the
#     fragment's entries fresh — so re-running bootstrap stays idempotent and
#     never stacks duplicate MS4CC hooks (the original reason the old code
#     overwrote .hooks wholesale); and
#   * PRESERVE every foreign entry untouched — which is the bug fix: the old
#     `.hooks = $frag[0].hooks` deleted any hook outside the fragment.

# Strip managed command-entries from an event's matcher-group array, then drop
# any group left empty.
def strip_managed($orch):
  map(.hooks = ((.hooks // []) | map(select((.command // "") | contains($orch + "/") | not))))
  | map(select((.hooks | length) > 0));

# The status line we install: one absolute dir used twice, then optionally the wrapped command.
def is_ours_statusline:
  type == "object" and ((.command // "") | type) == "string"
  and ((.command // "") | gsub("^\\s+|\\s+$"; "")
       | test("^(/\\S+)/\\.venv/bin/python \\1/fleet/statusline\\.py( --wrap-b64 [A-Za-z0-9+/=]+)?$"));

def wrapped_b64: ((first(.command | scan("--wrap-b64 ([A-Za-z0-9+/=]+)")) // [null])[0]);

($frag[0].hooks // {}) as $new
| (
    (.hooks // {})
    | to_entries
    | map({key: .key, value: (.value | strip_managed($orch))})
    | from_entries
  ) as $kept
| .hooks = (
    reduce ($new | keys_unsorted[]) as $ev ($kept;
      .[$ev] = ((.[$ev] // []) + $new[$ev]))
    # Drop events left with no entries (e.g. a stale managed hook for an event
    # the fragment no longer registers).
    | with_entries(select((.value | length) > 0))
  )
| (if $frag[0].autoCompactEnabled != null
     then .autoCompactEnabled = $frag[0].autoCompactEnabled
     else . end)
| .env = ((.env // {}) + ($frag[0].env // {}))
# statusLine (Fleet, synapse#31 section 5). Installed only while Fleet is on (bootstrap
# passes --argjson fleet true when synapse.toml has [fleet] enabled = true), and removed again
# when it is off:
# - Fleet on: install the fragment's status line, but WRAP an existing one, never replace it.
#   The user's command rides along base64-encoded after `--wrap-b64` (shell-safe, no quoting),
#   and fleet/statusline.py runs it with the same stdin and shows its output unchanged. Every
#   other field of the user's statusLine (padding, refreshInterval, ...) is kept. A re-run sees
#   the command is already ours and only refreshes our prefix, keeping the wrapped command, so
#   bootstrap stays idempotent and never wraps us inside ourselves.
# - Fleet off, or a fragment with no statusLine: if the status line is ours, put the wrapped
#   command back (other fields kept), or remove the key if we installed the only one there was.
# "Ours" is the exact shape we install, one absolute dir used twice
# (<dir>/.venv/bin/python <dir>/fleet/statusline.py [--wrap-b64 <b64>]), so a moved
# orchestrator dir is still recognised, while a user's own command (a relative or ~ path, two
# different dirs, a compound `cd x && ...`) is wrapped, never replaced or removed. A statusLine
# that isn't an object, or has no usable command, is left untouched.
| ($frag[0].statusLine // null) as $mine
| (($ARGS.named.fleet // false) == true) as $fleet_on
| (.statusLine // null) as $old
| if ($old | is_ours_statusline) then
    ($old | wrapped_b64) as $w
    | if $mine != null and $fleet_on then
        .statusLine = ($old + {type: "command",
                               command: ($mine.command + (if $w then " --wrap-b64 " + $w else "" end))})
      elif $w == null then del(.statusLine)
      else (try ($w | @base64d) catch null) as $cmd
        # Our wrap is @base64 of a jq string, so it always decodes to valid UTF-8; anything that
        # doesn't (U+FFFD after decoding) was edited by hand: leave it, statusline.py shows its
        # own bar for it.
        | if ($cmd | type) == "string" and ($cmd | length) > 0 and ($cmd | contains("\ufffd") | not)
          then .statusLine = ($old + {command: $cmd})
          else . end
      end
  elif $mine == null or ($fleet_on | not) then .
  elif $old == null then .statusLine = $mine
  elif ($old | type) != "object" then .
  elif (($old.type // "command") == "command") and (($old.command // "") | type) == "string"
       and (($old.command // "") | length) > 0 then
    .statusLine = ($old + {type: "command", command: ($mine.command + " --wrap-b64 " + ($old.command | @base64))})
  else .
  end
