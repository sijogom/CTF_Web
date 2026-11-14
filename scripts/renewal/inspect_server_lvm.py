from __future__ import annotations

import json
from ops import receipt, remote_python

SOURCE = '''
import json, subprocess
def inspect(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    return {'status': result.returncode, 'output': result.stdout[:6000]}
print(json.dumps({'block_devices': inspect(['lsblk', '--json', '--bytes', '-o', 'NAME,SIZE,TYPE,MOUNTPOINT']),
 'volume_groups': inspect(['sudo', '-n', 'vgs', '--reportformat', 'json', '--units', 'b', '--nosuffix', '-o', 'vg_name,vg_size,vg_free']),
 'logical_volumes': inspect(['sudo', '-n', 'lvs', '--reportformat', 'json', '--units', 'b', '--nosuffix', '-o', 'lv_path,vg_name,lv_size'])}))
'''

value = remote_python(SOURCE, timeout=75)
path = receipt("server-lvm.json", value)
print(json.dumps({"receipt": str(path), **value}))
