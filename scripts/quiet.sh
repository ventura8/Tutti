#!/usr/bin/env bash
# quiet.sh <command...>: runs it, keeps the full log, prints only `rc=<code> log=<file>`. On failure, when QUIET_SUMMARY is
# set to a command (for example: tutti ask small "..."), the last 300 lines go to its stdin and its short answer is printed,
# so an orchestrator reads five lines instead of a thousand.
[ $# -gt 0 ] || { echo "usage: quiet.sh <command...>" >&2; exit 2; }
dir="${QUIET_DIR:-${TMPDIR:-/tmp}/tutti-quiet}"
mkdir -p "$dir"
log="$dir/$(date +%s)-$$.log"
"$@" > "$log" 2>&1
rc=$?
echo "rc=$rc log=$log"
if [ "$rc" -ne 0 ]; then
	if [ ! -s "$log" ]; then
		echo "(the command printed nothing)"
	elif [ -n "${QUIET_SUMMARY:-}" ]; then
		tail -n 300 "$log" | bash -c "$QUIET_SUMMARY" || tail -n 15 "$log"
	else
		tail -n 15 "$log"
	fi
fi
exit "$rc"
