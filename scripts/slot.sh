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
#      SLOT_STALE_H (3): a waiting run names each slot holder (pid, age, command; <lock>.who) every SLOT_REPORT_S (300) s and
#      flags a holder past this many hours, or one whose process is gone, so a stuck run is seen; it never kills it.
#      A waiting run also says its place in the queue (<prefix>wait.<pid> files, oldest first).
#      SLOT_CPU_KNOBS (set: the CPU budget is on), SLOT_CPU_SLOTS, SLOT_CPU_OVERRIDE, SLOT_CPU_CORES, SLOT_CPU_DOCKER,
#      SLOT_CPU_THROTTLE, SLOT_CPU_WINDOW/DAYS/PCT/ZONE: the inputs of the per-slot CPU budget (tutti/slotcpus.py). Once the
#      lock is held, right before the command, the budget is worked out from the heat and the hour as they are then, and
#      exported as TUTTI_SLOT_CPUS plus each knob (NAME=template, {n} is the budget) the caller has not set. SLOT_NOW: the
#      epoch second to use as the time (tests).
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
# the percent of the limited-hours window the machine is in at this moment (100 outside it); the hours are in SLOT_CPU_ZONE
window_pct() {
	[ -n "${SLOT_CPU_WINDOW:-}" ] || { echo 100; return; }
	local a b h d now day
	a=$((10#${SLOT_CPU_WINDOW%-*})); b=$((10#${SLOT_CPU_WINDOW#*-}))
	now="@${SLOT_NOW:-$(date +%s)}"
	read -r h d < <(TZ="${SLOT_CPU_ZONE:-UTC}" date -d "$now" '+%H %a')
	h=$((10#$h)); d=$(echo "$d" | tr '[:upper:]' '[:lower:]')
	day=$d
	if [ "$a" -lt "$b" ]; then
		if [ "$h" -lt "$a" ] || [ "$h" -ge "$b" ]; then echo 100; return; fi
	elif [ "$h" -ge "$a" ]; then
		:
	elif [ "$h" -lt "$b" ]; then
		day=$(TZ="${SLOT_CPU_ZONE:-UTC}" date -d "@$(( ${now#@} - 86400 ))" '+%a' | tr '[:upper:]' '[:lower:]')   # a window past midnight belongs to the day it started
	else
		echo 100; return
	fi
	case ",${SLOT_CPU_DAYS:-mon,tue,wed,thu,fri,sat,sun}," in
		*",$day,"*) echo "${SLOT_CPU_PCT:-100}" ;;
		*) echo 100 ;;
	esac
}
# the slot's CPU budget now: its CPUs (SLOT_CPU_CORES or nproc), at most the docker runner's, at most the throttle at the heat
# cap, at the window's share, divided among the slots (the same arithmetic as slotcpus.budget())
cpu_budget() {
	local n t low
	if [ -n "${SLOT_CPU_OVERRIDE:-}" ]; then echo "$SLOT_CPU_OVERRIDE"; return; fi
	n=${SLOT_CPU_CORES:-$(nproc 2>/dev/null || echo 1)}
	if [ -n "${SLOT_CPU_DOCKER:-}" ] && [ "$n" -gt "$SLOT_CPU_DOCKER" ]; then n=$SLOT_CPU_DOCKER; fi
	low=${SLOT_CPU_THROTTLE:-1}
	if [ -n "${HEAT_CAP_C:-}" ] && [ "$n" -gt "$low" ]; then
		t=$(temp_c)
		if [ "$t" -gt 0 ] && [ "$t" -ge "$HEAT_CAP_C" ]; then n=$low; fi
	fi
	n=$(( n * $(window_pct) / 100 / ${SLOT_CPU_SLOTS:-1} ))
	[ "$n" -ge 1 ] || n=1
	echo "$n"
}
# export TUTTI_SLOT_CPUS and the worker knobs a caller has not set; the command and a docker -e both read this shell's environment
export_cpu_budget() {
	[ -n "${SLOT_CPU_KNOBS+x}" ] || return 0
	local kv k v
	[ -n "${TUTTI_SLOT_CPUS:-}" ] || TUTTI_SLOT_CPUS=$(cpu_budget)
	export TUTTI_SLOT_CPUS
	for kv in $SLOT_CPU_KNOBS; do
		k=${kv%%=*}; v=${kv#*=}
		[ -n "${!k:-}" ] || export "$k=${v//\{n\}/$TUTTI_SLOT_CPUS}"
	done
}
# who holds each slot, with age; a holder past SLOT_STALE_H hours or with no live process is flagged (never killed)
holders() {
	local lock pid since cmd age now
	now=$(date +%s)
	for lock in "${locks[@]}"; do
		[ -r "$lock.who" ] || continue
		flock -n "$lock" true 2>/dev/null && continue
		read -r pid since cmd < "$lock.who" || continue
		age=$(( (now - since) / 60 ))
		if ! kill -0 "$pid" 2>/dev/null && [ ! -d "/proc/$pid" ]; then
			echo "slot.sh:   ${lock##*/}: pid $pid (${age} min, $cmd): its process is gone but the lock is held (a child keeps it?)" >&2
		elif [ "$age" -ge $(( ${SLOT_STALE_H:-3} * 60 )) ]; then
			echo "slot.sh:   ${lock##*/}: pid $pid for ${age} min ($cmd): past ${SLOT_STALE_H:-3} h, likely stuck: check it" >&2
		else
			echo "slot.sh:   ${lock##*/}: pid $pid for ${age} min ($cmd)" >&2
		fi
	done
}
# this run's place among the waiters (1 = next)
position() {
	local f n=1 mine
	mine=$(stat -c %Y "$waitf" 2>/dev/null || echo 0)
	for f in "${prefix}"wait.*; do
		if [ ! -e "$f" ] || [ "$f" = "$waitf" ]; then continue; fi
		kill -0 "${f##*.}" 2>/dev/null || { rm -f "$f" 2>/dev/null; continue; }
		[ "$(stat -c %Y "$f" 2>/dev/null || echo 0)" -le "$mine" ] && n=$((n + 1))
	done
	echo "$n"
}
said=""
queued=""
reported=0
waitf="${prefix}wait.$$"
trap 'rm -f "$waitf"' EXIT
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
			until [ "$(avail_gb)" -ge "${MIN_FREE_GB:-6}" ]; do sleep 20; done
			if why=$(too_hot "$lock"); then
				# said once per kind (too hot / hot), not at every degree
				[ "${why%%:*}" = "$said" ] || echo "slot.sh: waiting, $why; it starts when the machine cools down" >&2
				said="${why%%:*}"
				exec 9>&-
				break
			fi
			# the dashboard reads these two lines: queued (progress 0, "waiting for a slot"), then out of the queue
			[[ -z "$queued" ]] || echo "slot.sh: got a slot" >&2
			rm -f "$waitf"
			echo "$$ $(date +%s) ${*:1:3}" | cut -c1-120 > "$lock.who" 2>/dev/null || true
			export_cpu_budget   # after the lock, so the heat and the hour are the ones at the start, not at the queueing
			"$@"
			exit $?
		fi
	done
	[[ -e "$waitf" ]] || : > "$waitf" 2>/dev/null || true
	if [[ -z "$queued" && -z "$said" ]]; then
		echo "slot.sh: waiting for a free slot (all ${#locks[@]} busy), place $(position) in the queue" >&2
		queued=1
	fi
	if [[ -n "$queued" ]] && [ $(( $(date +%s) - reported )) -ge "${SLOT_REPORT_S:-300}" ]; then
		echo "slot.sh: still waiting, place $(position) in the queue; the slots are held by:" >&2
		holders
		reported=$(date +%s)
	fi
	sleep "${SLOT_POLL:-15}"
done
