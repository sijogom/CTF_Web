from __future__ import annotations

import json
from ops import receipt, remote_python

SOURCE = '''
import json, os, pathlib, subprocess
def run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError('FILESYSTEM_INSPECTION_FAILED')
    return result.stdout
info = json.loads(run(['docker', 'info', '--format', '{{json .}}']))
paths = ['/home/deploy/CTF_Web', info['DockerRootDir'], '/tmp', '/var/tmp']
filesystems = []
for path in paths:
    stat = os.statvfs(path)
    filesystems.append({'path': path, 'available_bytes': stat.f_bavail * stat.f_frsize,
      'total_bytes': stat.f_blocks * stat.f_frsize})
print(json.dumps({'filesystems': filesystems, 'docker_storage_driver': info.get('Driver'),
 'df': run(['df', '-hT']), 'mount_summary': run(['findmnt', '-rn', '-o', 'TARGET,SOURCE,FSTYPE,SIZE,AVAIL'])}))
'''

value = remote_python(SOURCE, timeout=90)
path = receipt("server-filesystems.json", value)
print(json.dumps({"receipt": str(path), **value}))
