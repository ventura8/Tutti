#!/usr/bin/env bash
# slot.sh <command...>: at most SLOTS heavy runs at once on this machine, and only once MIN_FREE_GB of memory is available.
# A run that waits is normal, not a hang. Every session that shares the machine must use the same SLOT_LOCK_PREFIX.
# env: SLOTS (2), MIN_FREE_GB (6), SLOT_LOCK_PREFIX (/tmp/tutti-slot-), SLOT_POLL (15 s);
#      SLOT_LOCKS: explicit lock files, space-separated, instead of <prefix><n>.lock (to share a lock an older script uses);
#      HEAT_STOP_C, HEAT_CAP_C, HOT_SLOTS (1): machines.<m>.heat. A run that already waits here re-reads the temperature
#      right before it starts: at HEAT_STOP_C or more it does not start, at HEAT_CAP_C or more only while fewer than
#      HOT_SLOTS other runs hold a slot; it gives its slot back and waits until the machine cools down.
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
while :; do
	for lock in "${locks[@]}"; do
		exec 9>"$lock"
		if flock -n 9; then
			until [ "$(avail_gb)" -ge "${MIN_FREE_GB:-6}" ]; do sleep 20; done
			if why=$(too_hot "$lock"); then
				# said once per kind (too hot / hot), not at every degree
				[ "${why%%:*}" = "$said" ] || echo "slot.sh: waiting, $why; it starts when the machine cools down" >&2
				said="${why%%:*}"
				exec 9>&-
				break
			fi
			"$@"
			exit $?
		fi
	done
	sleep "${SLOT_POLL:-15}"
done
