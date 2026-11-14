from __future__ import annotations

import json
from ops import receipt, remote_python

SOURCE = '''
import json, pathlib, subprocess
def inspect(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=45)
    return {'status': result.returncode, 'output': result.stdout[:18000]}
result = {'docker_summary': inspect(['docker', 'system', 'df'])}
for name, path in [('live', '/home/deploy/CTF_Web'), ('release_history', '/home/deploy/.ctf-web-targeted-deploy')]:
    if pathlib.Path(path).is_dir():
        result[name] = inspect(['du', '-x', '--max-depth=1', '--block-size=1', path])
print(json.dumps(result))
'''

value = remote_python(SOURCE, timeout=120)
path = receipt("server-space.json", value)
print(json.dumps({"receipt": str(path), **value}))
