from __future__ import annotations

import json
from ops import receipt, remote_python

SOURCE = '''
import json, subprocess
def run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('IMAGE_INSPECTION_FAILED')
    return result.stdout
container_ids = run(['docker', 'ps', '-aq']).splitlines()
used = set()
if container_ids:
    used = {c['Image'] for c in json.loads(run(['docker', 'inspect', *container_ids]))}
images = run(['docker', 'image', 'ls', '-aq', '--no-trunc']).splitlines()
ctf_candidates = []
unattributed = 0
for image in json.loads(run(['docker', 'image', 'inspect', *sorted(set(images))])) if images else []:
    if image['Id'] in used:
        continue
    tags = image.get('RepoTags') or []
    if any(tag.lower().startswith(('ctf-', 'ctf_', 'ctfweb', 'ctf-web')) for tag in tags):
        ctf_candidates.append({'id': image['Id'], 'tags': tags, 'size_bytes': image['Size'],
            'created': image['Created'], 'rollback_tag': any('rollback' in tag or 'backup' in tag for tag in tags)})
    else:
        unattributed += 1
print(json.dumps({'ctf_unreferenced': ctf_candidates, 'other_or_unattributed_unreferenced_count': unattributed,
 'note': 'image sizes include shared layers; no deletions performed'}))
'''

value = remote_python(SOURCE, timeout=90)
path = receipt("server-unused-images.json", value)
print(json.dumps({"receipt": str(path), **value}))
