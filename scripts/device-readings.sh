#!/usr/bin/env bash
# device-readings.sh: one look at an Android device over adb, on the machine its adb sees it on. Prints key=value lines
# (tutti/devices.py reads them): adb, state, locked, battery, charging, battery_c, screen, wifi. Read-only: it changes no
# setting; WAKE=1 only presses the wake key, so an e2e run does not start against a dark screen.
# env: DEVICE_SERIAL (required: the serial `adb devices` lists, or ip:port for wireless debugging),
#      DEVICE_LOCK (its lock file: reports whether a run holds it), ADB (adb), WAKE (0).
[[ -n "${DEVICE_SERIAL:-}" ]] || { echo "usage: DEVICE_SERIAL=<serial> device-readings.sh" >&2; exit 2; }
adb=${ADB:-adb}
if [[ -n "${DEVICE_LOCK:-}" ]]; then
	mkdir -p "$(dirname "$DEVICE_LOCK")"
	if flock -n "$DEVICE_LOCK" true 2> /dev/null; then echo "locked=0"; else echo "locked=1"; fi
fi
command -v "$adb" > /dev/null 2>&1 || { echo "adb=missing"; exit 0; }
echo "adb=ok"
state=$(timeout 20 "$adb" devices 2> /dev/null | tr -d '\r' | awk -v s="$DEVICE_SERIAL" '$1 == s {print $2}')
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
s=$(on cmd wifi status)
if printf '%s' "$s" | grep -qi 'connected to'; then echo "wifi=1"; elif [[ -n "$s" ]]; then echo "wifi=0"; fi
exit 0
