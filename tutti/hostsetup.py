import subprocess
import os
import shutil

def setup_host(address):
    """Automatic host setup: SSH key installation and tutti installation."""
    
    # If address contains 'adb:' prefix or looks like an ADB serial, treat as ADB
    if address.startswith('adb:'):
        return _setup_adb_device(address[4:])
    
    # 1. Install SSH key
    if subprocess.run(['ssh-copy-id', address]).returncode != 0:
        return {'agent': address, 'status': 'error', 'changed': [], 'note': 'ssh-copy-id failed'}

    # 2. Copy and run install script
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    install_script = os.path.join(repo_root, 'install', 'install.sh')
    
    try:
        with open(install_script, 'r') as f:
            script_content = f.read()
        subprocess.run(['ssh', address, 'bash -s'], input=script_content, text=True, check=True)
    except subprocess.CalledProcessError as e:
        return {'agent': address, 'status': 'error', 'changed': [], 'note': f'installation on host failed: {e}'}

    return {'agent': address, 'status': 'connected', 'changed': [], 'note': 'host connected and tutti installed successfully'}

def _setup_adb_device(serial):
    # This would require adbhost.Host functionality.
    # For now, just return not implemented.
    return {'agent': f'adb:{serial}', 'status': 'error', 'changed': [], 'note': 'ADB setup not yet implemented'}
