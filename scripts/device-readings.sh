#!/usr/bin/env bash
# device-readings.sh: one look at an Android device over adb, on the machine its adb sees it on. Prints key=value lines
# (tutti/devices.py reads them): adb, state, locked, battery, charging, battery_c, screen, call, wifi. Read-only: it changes no
# setting; WAKE=1 only presses the wake key, so an e2e run does not start against a dark screen.
# env: DEVICE_SERIAL (required: the serial `adb devices` lists, or ip:port for wireless debugging), EXT_LOCK and EXT_STALE
#      (another tool's lock file for it and its staleness in seconds), PREP_SAVED (values a prep saved, not yet restored),
#      DEVICE_LOCK (its lock file: reports whether a run holds it), ADB (adb), WAKE (0).
[[ -n "${DEVICE_SERIAL:-}" ]] || { echo "usage: DEVICE_SERIAL=<serial> device-readings.sh" >&2; exit 2; }
adb=${ADB:-adb}
if [[ -n "${DEVICE_LOCK:-}" ]]; then
	mkdir -p "$(dirname "$DEVICE_LOCK")"
	if flock -n "$DEVICE_LOCK" true 2> /dev/null; then echo "locked=0"; else echo "locked=1"; fi
fi
# another tool's lock file for this device (EXT_LOCK), fresher than EXT_STALE seconds: who holds it
if [[ -n "${EXT_LOCK:-}" && -e "$EXT_LOCK" ]] && (( $(date +%s) - $(stat -c %Y "$EXT_LOCK") < ${EXT_STALE:-1800} )); then
	echo "external=1"
	echo "external_by=$(head -n 1 "$EXT_LOCK" | tr -d '\r' | tr -c '[:alnum:] ._:()+-' ' ' | cut -c1-120)"
fi
# a prep whose saved values were never put back (the run's machine rebooted, the device was gone): the next round restores it
if [[ -n "${PREP_SAVED:-}" && -e "$PREP_SAVED" ]]; then echo "prep_left=1"; fi
command -v "$adb" > /dev/null 2>&1 || { echo "adb=missing"; exit 0; }
# a Windows adb.exe called from WSL without interop is found but cannot run ("Exec format error")
timeout 20 "$adb" version > /dev/null 2>&1 || { echo "adb=broken"; exit 0; }
echo "adb=ok"
state_of() { timeout 20 "$adb" devices 2> /dev/null | tr -d '\r' | awk -v s="$1" '$1 == s {print $2}'; }
state=$(state_of "$DEVICE_SERIAL")
# a wireless-debugging serial (<name>._adb-tls-connect._tcp) whose port changed: find today's ip:port with
# `adb mdns services` and connect to it, so adb lists the device again (its port is never written in the config)
if [[ -z "$state" && "$DEVICE_SERIAL" == *._adb-tls-connect._tcp ]]; then
	addr=$(timeout 20 "$adb" mdns services 2> /dev/null | tr -d '\r' | awk -v n="${DEVICE_SERIAL%%._adb-tls-connect._tcp}" '$1 == n {print $3; exit}')
	if [[ -n "$addr" ]]; then
		timeout 20 "$adb" connect "$addr" > /dev/null 2>&1
		echo "resolved=$addr"
		state=$(state_of "$DEVICE_SERIAL")
		# an adb that lists it by ip:port only: read it there for the rest of this look
		[[ "$state" = device ]] || { state=$(state_of "$addr"); [[ "$state" = device ]] && DEVICE_SERIAL=$addr; }
	fi
fi
echo "state=${state:-missing}"
[[ "$state" = device ]] || exit 0
on() { timeout 20 "$adb" -s "$DEVICE_SERIAL" shell "$@" 2> /dev/null | tr -d '\r'; }
[[ "${WAKE:-0}" = 1 ]] && on input keyevent KEYCODE_WAKEUP > /dev/null
on dumpsys battery | awk -F': *' '
	/^ *(AC|USB|Wireless|Dock) powered:/ { if ($2 == "true") p = 1 }
	/^ *level:/ { print "battery=" $2 }
	/^ *temperature:/ { print "battery_c=" $2 / 10 }
	END { print "charging=" (p ? 1 : 0) }'
w=$(on dumpsys power | grep -m1 -o 'mWakefulness=[A-Za-z]*')
w=${w#mWakefulness=}
if [[ "$w" = Awake ]]; then echo "screen=1"; elif [[ -n "$w" ]]; then echo "screen=0"; fi
# the call state of every SIM slot (0 idle, 1 ringing, 2 offhook): the highest one, so a call on any slot counts
c=$(on dumpsys telephony.registry | grep -o 'mCallState=[0-9]' | cut -d= -f2 | sort -n | tail -1)
[[ -n "$c" ]] && echo "call=$c"
s=$(on cmd wifi status)
if printf '%s' "$s" | grep -qi 'connected to'; then echo "wifi=1"; elif [[ -n "$s" ]]; then echo "wifi=0"; fi
exit 0
