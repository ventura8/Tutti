#!/usr/bin/env bash
# Tutti installer for Linux, WSL and macOS: curl -fsSL https://raw.githubusercontent.com/ventura8/Tutti/main/install/install.sh | bash
# Installs (or updates) the latest Tutti from GitHub into $TUTTI_HOME (default ~/.local/share/tutti) with its own Python venv,
# and puts a `tutti` command in ~/.local/bin. Re-run it to update. No sudo, no secrets.
set -euo pipefail
repo="${TUTTI_REPO:-https://github.com/ventura8/Tutti.git}"
home="${TUTTI_HOME:-$HOME/.local/share/tutti}"
bin="${TUTTI_BIN:-$HOME/.local/bin}"
need() { command -v "$1" >/dev/null 2>&1 || { echo "tutti: $1 is missing; install it ($2) and run this again" >&2; exit 1; }; }
need git "e.g. sudo apt install git, or brew install git"
need python3 "e.g. sudo apt install python3 python3-venv, or brew install python"
if [ -d "$home/.git" ]; then
	git -C "$home" pull --ff-only --quiet
else
	mkdir -p "$(dirname "$home")"
	git clone --quiet --depth 1 "$repo" "$home"
fi
python3 -m venv "$home/.venv"
"$home/.venv/bin/pip" install --quiet --upgrade pip
"$home/.venv/bin/pip" install --quiet -r "$home/requirements.txt"
mkdir -p "$bin"
printf '#!/usr/bin/env bash\nexec "%s/.venv/bin/python" "%s/bin/tutti" "$@"\n' "$home" "$home" > "$bin/tutti"
chmod +x "$bin/tutti"
case ":$PATH:" in *":$bin:"*) ;; *) echo "tutti: add $bin to your PATH (e.g. in ~/.bashrc)";; esac
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
config_dir="$config_home/tutti"
config_path="$config_dir/config.yaml"
if [ ! -e "$config_path" ]; then
	mkdir -p "$config_dir"
	cp "$home/tutti.config.example.yaml" "$config_path"
	echo "tutti: wrote example config to $config_path"
fi
echo "tutti $(git -C "$home" rev-parse --short HEAD) installed. Next: edit $config_path, then run: tutti config; tutti workers"
