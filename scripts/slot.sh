#!/usr/bin/env bash
# slot.sh <command...>: at most SLOTS heavy runs at once on this machine, and only once MIN_FREE_GB of memory is available.
# A run that waits is normal, not a hang. Every session that shares the machine must use the same SLOT_LOCK_PREFIX.
# env: SLOTS (2), MIN_FREE_GB (6), SLOT_LOCK_PREFIX (/tmp/tutti-slot-), SLOT_POLL (15 s);
#      SLOT_LOCKS: explicit lock files, space-separated, instead of <prefix><n>.lock (to share a lock an older script uses)
prefix="${SLOT_LOCK_PREFIX:-/tmp/tutti-slot-}"
locks=()
if [ -n "${SLOT_LOCKS:-}" ]; then
	read -r -a locks <<< "$SLOT_LOCKS"
else
	for i in $(seq 1 "${SLOTS:-2}"); do locks+=("$prefix$i.lock"); done
fi
[ $# -gt 0 ] || { echo "usage: slot.sh <command...>" >&2; exit 2; }
avail_gb() { awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo; }
while :; do
	for lock in "${locks[@]}"; do
		exec 9>"$lock"
		if flock -n 9; then
			until [ "$(avail_gb)" -ge "${MIN_FREE_GB:-6}" ]; do sleep 20; done
			"$@"
			exit $?
		fi
	done
	sleep "${SLOT_POLL:-15}"
done
