"""Read only the explicitly named game workload on the deployment host."""
import json
from ops import receipt, remote_python

SOURCE = '''
import json, pathlib, re, subprocess
def run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    return result.returncode, result.stdout
_, output = run(['docker','ps','-a','--format','{{json .}}'])
containers = []
for line in output.splitlines():
    item = json.loads(line)
    if re.search(r'palworld|pal-server|palserver',item['Names']+' '+item['Image'],re.I):
        containers.append({key:item.get(key) for key in ('ID','Names','Image','State','Status')})
processes = []
for directory in pathlib.Path('/proc').iterdir():
    if not directory.name.isdigit(): continue
    try:
        name = (directory/'comm').read_text().strip()
        executable = (directory/'exe').resolve().name
        if re.search(r'palworld|palserver',name+' '+executable,re.I):
            processes.append({'pid':int(directory.name),'name':name,'executable':executable,'uid':directory.stat().st_uid})
    except (PermissionError,FileNotFoundError,ProcessLookupError): pass
services = {}
for scope,args in [('system',['systemctl']),('deploy_user',['systemctl','--user'])]:
    status, output = run(args+['list-units','--all','--type=service','--no-legend','--plain'])
    services[scope] = {'status':status,'matched_units':[line.split()[:4] for line in output.splitlines() if re.search(r'palworld|palserver',line,re.I)]}
print(json.dumps({'containers':containers,'processes':processes,'services':services}))
'''

value = remote_python(SOURCE, timeout=90)
path = receipt("server-palworld.json", value)
print(json.dumps({"receipt":str(path), **value}))
