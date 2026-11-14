from __future__ import annotations

import json
from ops import receipt, remote_python

SOURCE = '''
import json, pathlib, shutil, subprocess
def inspect(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    error = result.stderr.lower()
    return {'status': result.returncode, 'output': result.stdout[:5000],
      'password_required': 'password is required' in error or '비밀번호' in error,
      'permission_denied': 'permission denied' in error or 'not allowed' in error}
result = {'sudo_policy': inspect(['sudo', '-n', '-l'])}
for name in ['vgs', 'lvs']:
    binary = shutil.which(name)
    result[name] = inspect([binary, '--readonly', '--reportformat', 'json', '--units', 'b', '--nosuffix']) if binary else {'missing': True}
backup = pathlib.Path('/etc/lvm/backup/ubuntu-vg')
result['lvm_metadata_readable'] = False
try:
    metadata = backup.read_text()
    result['lvm_metadata_readable'] = True
    # Only allocation counters are returned, never arbitrary configuration text.
    result['allocation_counters'] = [line.strip() for line in metadata.splitlines() if line.strip().startswith(('extent_size =', 'pe_count =', 'extent_count =', 'start_extent ='))]
except (OSError, PermissionError):
    pass
print(json.dumps(result))
'''

value = remote_python(SOURCE, timeout=60)
path = receipt("server-lvm-access.json", value)
print(json.dumps({"receipt": str(path), **value}))
