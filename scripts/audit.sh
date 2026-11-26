#!/usr/bin/env bash
# audit.sh <pid> [interval_s]: measure and report host CPU, GPU/NPU and device (camera/sensor) utilization for a running task.
set -u
pid="${1:-}"
[ -n "$pid" ] || { echo "usage: audit.sh <pid> [interval_s]" >&2; exit 2; }
interval="${2:-${AUDIT_INTERVAL:-30}}"

host_cpu() {
	awk '{print $1}' /proc/loadavg 2>/dev/null || echo 0
}

proc_cpu() {
	local v
	v=$(ps -p "$pid" -o %cpu= 2>/dev/null | tr -d ' ')
	echo "${v:-0}"
}

gpu_npu() {
	local smi="${NVIDIA_SMI:-}" v="" f
	if [ -z "$smi" ]; then smi=$(command -v nvidia-smi 2>/dev/null || echo /usr/lib/wsl/lib/nvidia-smi); fi
	if [ "$smi" != none ] && [ -x "$smi" ]; then
		v=$("$smi" --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -n 1 | tr -d ' ')
	fi
	if [ -z "$v" ]; then
		for f in /sys/class/drm/card*/device/gpu_busy_percent /sys/class/accel/*/device/npu_busy_time_us; do
			if [ -r "$f" ]; then
				v=$(tr -d ' \n' < "$f" 2>/dev/null)
				[ -n "$v" ] && break
			fi
		done
	fi
	echo "${v:-0}"
}

device_util() {
	local count=0 d
	if [ -n "${AUDIT_DEVICE_GLOB:-}" ]; then
		while IFS= read -r d; do
			[ -e "$d" ] && count=$((count + 1))
		done < <(compgen -G "$AUDIT_DEVICE_GLOB" || true)
	else
		for d in /dev/video* /sys/bus/iio/devices/iio:device*; do
			[ -e "$d" ] && count=$((count + 1))
		done
	fi
	echo "$count"
}

sleep_pid=""
cleanup() {
	[ -n "$sleep_pid" ] && kill "$sleep_pid" 2>/dev/null || true
	exit 0
}
trap cleanup TERM INT EXIT

while kill -0 "$pid" 2>/dev/null; do
	echo "AUDIT: pid=$pid host_cpu=$(host_cpu) proc_cpu=$(proc_cpu)% gpu_npu=$(gpu_npu)% devices=$(device_util)"
	sleep "$interval" &
	sleep_pid=$!
	wait "$sleep_pid" 2>/dev/null || true
	sleep_pid=""
done
