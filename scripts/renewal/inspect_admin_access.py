"""Inspect only the named administrator's account and SSH policy, read only."""
import argparse
import json
import re
from ops import receipt, remote_python

SOURCE = '''
import json, pathlib, re, subprocess
def run(args):
    result = subprocess.run(args,capture_output=True,text=True,timeout=20)
    return result.returncode,result.stdout
status,output=run(['getent','passwd',ADMIN_USER])
account=None
if status==0:
    parts=output.strip().split(':')
    account={'name':parts[0],'uid':int(parts[2]),'gid':int(parts[3]),'home':parts[5],'shell':parts[6]}
status,output=run(['id','-nG',ADMIN_USER])
groups={'available':status==0,'sudo_member':'sudo' in output.split(),'docker_member':'docker' in output.split()}
directives=[]
paths=[pathlib.Path('/etc/ssh/sshd_config'),*sorted(pathlib.Path('/etc/ssh/sshd_config.d').glob('*.conf'))]
for path in paths:
    try:
        for number,line in enumerate(path.read_text().splitlines(),1):
            active=line.strip()
            if active.startswith('#') or not active:continue
            key,*values=active.split()
            if key.lower() in {'passwordauthentication','pubkeyauthentication','kbdinteractiveauthentication','usepam','authenticationmethods'}:
                directives.append({'file':str(path),'line':number,'key':key,'value':' '.join(values)})
            elif key.lower() in {'allowusers','denyusers','allowgroups','denygroups','match'}:
                directives.append({'file':str(path),'line':number,'key':key,'mentions_admin':any(ADMIN_USER in s for s in values),'mentions_deploy':any('deploy' in s for s in values)})
    except PermissionError:
        directives.append({'file':str(path),'readable':False})
status,output=run(['lastlog','-u',ADMIN_USER])
rows=output.strip().splitlines()
login={'available':status==0,'has_record':len(rows)>1 and 'Never logged in' not in rows[-1]}
print(json.dumps({'account':account,'groups':groups,'ssh_policy':directives,'lastlog':login}))
'''

def main():
    parser = argparse.ArgumentParser(description="지정한 서버 계정의 SSH 접근 정책을 조회합니다.")
    parser.add_argument("--user", required=True, help="조회할 Linux 계정명")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}[$]?", args.user):
        parser.error("유효한 Linux 계정명을 지정하세요.")
    value = remote_python("ADMIN_USER=" + repr(args.user) + "\n" + SOURCE, timeout=90)
    path = receipt("server-admin-access-inspection.json", value)
    print(json.dumps({"receipt":str(path),**value}))


if __name__ == "__main__":
    main()
