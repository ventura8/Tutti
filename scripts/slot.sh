#!/usr/bin/env bash
# slot.sh <command...>: at most SLOTS heavy runs at once on this machine, and only once MIN_FREE_GB of memory is available.
# A run that waits is normal, not a hang. Every session that shares the machine must use the same SLOT_LOCK_PREFIX.
# env: SLOTS (2), MIN_FREE_GB (6), SLOT_LOCK_PREFIX (/tmp/tutti-slot-), SLOT_POLL (15 s);
#      SLOT_LOCKS: explicit lock files, space-separated, instead of <prefix><n>.lock (to share a lock an older script uses);
#      HEAT_STOP_C, HEAT_CAP_C, HOT_SLOTS (1): machines.<m>.heat. A run that already waits here re-reads the temperature
#      right before it starts: at HEAT_STOP_C or more it does not start, at HEAT_CAP_C or more only while fewer than
#      HOT_SLOTS other runs hold a slot; it gives its slot back and waits until the machine cools down.
#      SLOT_SHARED: a household's shared machine: its lock folder is mode 1777, so a lock file another login made is opened
#      read-only (flock needs no write access) and a missing one is made readable by every login (umask 0 for that file
#      only: the command itself runs with the caller's umask). A lock that is a symlink is refused, a new one is made with
#      noclobber (O_EXCL: a planted link is never created through), and every lock is opened read-only, so even a link
#      swapped in between the check and the open is never written or truncated.
#      THERMAL_GLOB (/sys/class/thermal/thermal_zone*/temp): where the millidegrees are read (tests point it elsewhere).
prefix="${SLOT_LOCK_PREFIX:-/tmp/tutti-slot-}"
locks=()
if [ -n "${SLOT_LOCKS:-}" ]; then
	read -r -a locks <<< "$SLOT_LOCKS"
else
	for i in $(seq 1 "${SLOTS:-2}"); do locks+=("$prefix$i.lock"); done
fi
[ $# -gt 0 ] || { echo "usage: slot.sh <command...>" >&2; exit 2; }
avail_gb() { awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo; }
# the hottest of the CPU's thermal zones and the GPUs (nvidia-smi, in WSL from /usr/lib/wsl/lib), in whole degrees C;
# 0 when none is readable. NVIDIA_SMI names the program (tests point it elsewhere; `none` skips it).
temp_c() {
	local t g smi="${NVIDIA_SMI:-}"
	t=$(compgen -G "${THERMAL_GLOB:-/sys/class/thermal/thermal_zone*/temp}" | while read -r f; do cat "$f" 2>/dev/null; done | sort -n | tail -n 1)
	t=$(( ${t:-0} / 1000 ))
	if [ -z "$smi" ]; then smi=$(command -v nvidia-smi || echo /usr/lib/wsl/lib/nvidia-smi); fi
	if [ "$smi" != none ] && [ -x "$smi" ]; then
		g=$("$smi" --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null | cut -d. -f1 | sort -n | tail -n 1)
		if [ -n "$g" ] && [ "$g" -gt "$t" ] 2>/dev/null; then t=$g; fi
	fi
	echo "$t"
}
# how many of the other slots are held right now
busy_others() {
	local n=0 lock
	for lock in "${locks[@]}"; do
		[ "$lock" = "$1" ] && continue
		if ! flock -n "$lock" true 2>/dev/null; then n=$((n + 1)); fi
	done
	echo "$n"
}
# why the machine is too hot to start now, or nothing
too_hot() {
	local t
	t=$(temp_c)
	[ "$t" -gt 0 ] || return 1
	if [ -n "${HEAT_STOP_C:-}" ] && [ "$t" -ge "$HEAT_STOP_C" ]; then
		echo "too hot: $t C (nothing new at $HEAT_STOP_C C or more)"
		return 0
	fi
	if [ -n "${HEAT_CAP_C:-}" ] && [ "$t" -ge "$HEAT_CAP_C" ] && [ "$(busy_others "$1")" -ge "${HOT_SLOTS:-1}" ]; then
		echo "hot: $t C, at most ${HOT_SLOTS:-1} run(s) at $HEAT_CAP_C C or more"
		return 0
	fi
	return 1
}
said=""
queued=""
while :; do
	for lock in "${locks[@]}"; do
		if [[ ! -e "$lock" && ! -L "$lock" ]]; then
			if [[ -n "${SLOT_SHARED:-}" ]]; then
				(umask 0000; set -o noclobber; : > "$lock") 2> /dev/null || true
			else
				(set -o noclobber; : > "$lock") 2> /dev/null || true
			fi
		fi
		if [[ -L "$lock" ]]; then
			echo "slot.sh: $lock is a symbolic link (someone else's file?): refused; remove it, then run again" >&2
			exit 2
		fi
		exec 9< "$lock"   # flock needs no write access: the file is never truncated or written
		if flock -n 9; then
			if [ "$(avail_gb)" -lt "${MIN_FREE_GB:-6}" ]; then
				exec 9>&-
				break
			fi
			if why=$(too_hot "$lock"); then
				# said once per kind (too hot / hot), not at every degree
				[ "${why%%:*}" = "$said" ] || echo "slot.sh: waiting, $why; it starts when the machine cools down" >&2
				said="${why%%:*}"
				exec 9>&-
				break
			fi
			# the dashboard reads these two lines: queued (progress 0, "waiting for a slot"), then out of the queue
			[[ -z "$queued" ]] || echo "slot.sh: got a slot" >&2
			audit_script="$(dirname "$0")/audit.sh"
			audit_pid=""
			cleanup_audit() {
				if [ -n "$audit_pid" ]; then
					kill "$audit_pid" 2>/dev/null || true
					wait "$audit_pid" 2>/dev/null || true
					audit_pid=""
				fi
			}
			if [ -x "$audit_script" ]; then
				"$audit_script" $$ 9<&- >&2 &
				audit_pid=$!
				trap cleanup_audit EXIT TERM INT
			fi
			"$@"
			rc=$?
			cleanup_audit
			exit $rc
		fi
	done
	if [[ -z "$queued" && -z "$said" ]]; then
		echo "slot.sh: waiting for a free slot (all ${#locks[@]} busy)" >&2
		queued=1
	fi
	sleep "${SLOT_POLL:-15}"
done
