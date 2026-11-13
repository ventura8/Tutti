#!/usr/bin/env bash
# tunnel.sh start|stop|status <name> <local host:port> <ssh host> <remote host:port>
# A self-healing ssh tunnel to a model server on another machine: a restart loop in the background (ServerAlive 15 s x 3, so
# a dead link is noticed in about 45 s; 5 s back-off). Pid and log files: $TUTTI_STATE (~/.local/state/tutti)/tunnel-<name>.*
[ $# -eq 5 ] || { echo "usage: tunnel.sh start|stop|status <name> <local host:port> <ssh host> <remote host:port>" >&2; exit 2; }
action=$1; name=$2; local_addr=$3; ssh_host=$4; remote_addr=$5
state="${TUTTI_STATE:-$HOME/.local/state/tutti}"
mkdir -p "$state"
pidf="$state/tunnel-$name.pid"
running() { [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2> /dev/null; }
loop() {
	while :; do
		echo "$(date '+%F %T') ssh up"
		ssh -N -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
			-L "$1:$3" "$2"
		echo "$(date '+%F %T') ssh exited ($?): restart in 5 s"
		sleep 5
	done
}
case $action in
	start)
		if running; then echo "already running: $(cat "$pidf")"; exit 0; fi
		export -f loop
		setsid nohup bash -c 'loop "$@"' loop "$local_addr" "$ssh_host" "$remote_addr" >> "$state/tunnel-$name.log" 2>&1 < /dev/null &
		echo $! > "$pidf"
		echo "started: $(cat "$pidf")" ;;
	stop)
		# by process group of the recorded pid: never `pkill -f <pattern>`, which also matches the shell that typed the pattern
		if running; then pkill -g "$(cat "$pidf")"; rm -f "$pidf"; echo stopped; else echo "not running"; fi ;;
	status)
		if running; then echo "loop $(cat "$pidf") running"; else echo "loop not running"; fi
		curl -s -m 5 "http://$local_addr/api/version" || echo "the model server is unreachable on $local_addr" ;;
	*)
		echo "unknown action $action: use start, stop or status" >&2; exit 2 ;;
esac
