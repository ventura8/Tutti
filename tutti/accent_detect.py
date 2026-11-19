import os
import subprocess


def get_linux_accent_color():
    # Try GNOME
    try:
        color = subprocess.check_output(
            ['gsettings', 'get', 'org.gnome.desktop.interface', 'accent-color'], text=True
        ).strip().strip("'")
        if color != 'nothing':
            return color
    except subprocess.CalledProcessError:
        pass

    # Try KDE
    try:
        # Check kdeglobals
        config_path = os.path.expanduser('~/.config/kdeglobals')
        if os.path.exists(config_path):
            with open(config_path) as f:
                for line in f:
                    if 'AccentColor=' in line:
                        return line.split('=')[1].strip()
    except OSError:
        pass

    return None


if __name__ == "__main__":
    print(get_linux_accent_color())
