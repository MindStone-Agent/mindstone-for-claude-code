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
