#!/usr/bin/env bash
# memguard.sh: keeps the desktop alive beside test containers. Under LOW_GB of available memory it PAUSES (docker pause,
# nothing is killed) every running container of GUARD_IMAGE; back over HIGH_GB it unpauses them.
# env: GUARD_IMAGE (required), LOW_GB (3), HIGH_GB (6). Start it once: nohup memguard.sh > memguard.log 2>&1 &
LOW_GB="${LOW_GB:-3}"
HIGH_GB="${HIGH_GB:-6}"
[ -n "${GUARD_IMAGE:-}" ] || { echo "memguard: set GUARD_IMAGE to the image whose containers may be paused" >&2; exit 2; }
paused=()
while :; do
	avail=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)
	if [ "$avail" -lt "$LOW_GB" ] && [ ${#paused[@]} -eq 0 ]; then
		mapfile -t paused < <(docker ps --filter "ancestor=$GUARD_IMAGE" --filter status=running --format '{{.ID}}')
		[ ${#paused[@]} -gt 0 ] && docker pause "${paused[@]}" > /dev/null && echo "$(date +%T) paused ${paused[*]} (avail ${avail} GB)"
	elif [ "$avail" -ge "$HIGH_GB" ] && [ ${#paused[@]} -gt 0 ]; then
		docker unpause "${paused[@]}" > /dev/null 2>&1
		echo "$(date +%T) unpaused ${paused[*]} (avail ${avail} GB)"
		paused=()
	fi
	sleep "${GUARD_POLL:-5}"
done
