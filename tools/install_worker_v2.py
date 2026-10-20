#!/usr/bin/env python3
"""Install a recorded worker release into a separate directory; never update on restart."""

import argparse
from contextlib import contextmanager
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler,Request,build_opener


WORKER_REPOSITORY='https://github.com/Daniel-T-S-Adams/community-pool-worker.git'
POOL_REPOSITORY='https://github.com/Daniel-T-S-Adams/community-pool.git'
MARKER='installation-v2.json'
VERSION='2.0'


class InstallError(ValueError):pass


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def origin(value):
    parsed=urlsplit(value)
    if parsed.scheme!='https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
        raise InstallError('an HTTPS pool origin is required')
    return value.rstrip('/')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise InstallError('release metadata must not redirect to another origin')


def fetch(pool,path):
    request=Request(origin(pool)+path,headers={'Accept':'application/json','Cache-Control':'no-cache'})
    with build_opener(NoRedirect()).open(request,timeout=20) as response:raw=response.read(1024*1024+1)
    if len(raw)>1024*1024:raise InstallError('release metadata is too large')
    return json.loads(raw)


def validate_manifest(value):
    if not isinstance(value,dict) or type(value.get('manifest_version')) is not int or value.get('manifest_version')!=1 or value.get('api_version')!=VERSION:
        raise InstallError('unsupported paired release manifest')
    for key,expected in (('pool',POOL_REPOSITORY),('worker',WORKER_REPOSITORY)):
        release=value[key]
        if (release['repository']!=expected or not re.fullmatch(r'[0-9a-f]{40}',release['commit'])
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}',release['tag'])):
            raise InstallError('release repository, full commit or tag is invalid')
    if type(value['worker'].get('state_version')) is not int or value['worker'].get('state_version')!=1:raise InstallError('incompatible worker evidence format')
    if not re.fullmatch(r'[0-9a-f]{64}',value['worker']['installer_sha256']):raise InstallError('recorded installer checksum required')
    # The pool pins each challenge's official runtime image and sends it inside every
    # assignment; the served release lists the current pins for information only.
    if 'runtime_version' in value and (not isinstance(value['runtime_version'],str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',value['runtime_version'])):
        raise InstallError('the TIG runtime version must be a dotted release number')
    if 'runtime_images' in value and not isinstance(value['runtime_images'],dict):raise InstallError('pinned runtime images must be a map')
    return value


def release_from_pool(pool):
    capabilities=fetch(pool,'/api/v2/capabilities')
    if capabilities.get('api_version')!=VERSION or capabilities.get('assignment_unit')!='whole-benchmark':
        raise InstallError('pool does not support this whole-benchmark worker')
    value=validate_manifest(fetch(pool,'/api/v2/release'))
    digest=hashlib.sha256(canonical(value).encode()).hexdigest()
    if capabilities.get('release_digest')!=digest or capabilities.get('pool_commit')!=value['pool']['commit']:
        raise InstallError('pool and release metadata disagree; retry after the deployment is stable')
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=value['worker']['installer_sha256']:
        raise InstallError('download the current installer from the pool Join page before installing or updating')
    return value


def atomic(path,text,mode=0o600):
    path=Path(path)
    descriptor,temporary=tempfile.mkstemp(prefix='.writing-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'w') as output:
            os.fchmod(output.fileno(),mode);output.write(text);output.flush();os.fsync(output.fileno())
        os.replace(temporary,path)
        sync(path.parent)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def sync(path):
    descriptor=os.open(path,os.O_DIRECTORY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def command(args,*,cwd=None):
    try:result=subprocess.run(args,cwd=cwd,check=False,capture_output=True,text=True,timeout=600)
    except subprocess.TimeoutExpired as error:raise InstallError('installation command timed out: '+Path(args[0]).name) from error
    if result.returncode:
        # Credentials, environment values and remote output are not error messages.
        raise InstallError('installation command failed: '+Path(args[0]).name)
    return result.stdout.strip()


def verify_checkout(directory,release):
    if directory.is_symlink() or not directory.is_dir():raise InstallError('release checkout is missing or redirected')
    if command(['git','remote','get-url','origin'],cwd=directory)!=release['repository']:
        raise InstallError('checkout belongs to a different repository')
    if command(['git','rev-parse','HEAD'],cwd=directory)!=release['commit']:
        raise InstallError('checkout is not at the recorded commit')
    if command(['git','status','--porcelain','--untracked-files=normal'],cwd=directory):
        raise InstallError('checkout contains changes; preserve and review them before updating')
    source=directory/'tools/install_worker_v2.py'
    if not source.is_file() or source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest()!=release['installer_sha256']:
        raise InstallError('checkout installer does not match the recorded release checksum')


def read_installation(root):
    if root.is_symlink() or not root.is_dir() or not (root/MARKER).is_file() or (root/MARKER).is_symlink():
        raise InstallError('directory is not an owned v2 worker installation')
    marker=json.loads((root/MARKER).read_text())
    if marker.get('kind')!='innopool-v2-worker' or marker.get('directory')!=str(root) or marker.get('version')!=1:
        raise InstallError('installation ownership marker does not match this directory')
    if any((root/name).is_symlink() for name in ('data','releases','worker.json','execution-token','installation.lock')):
        raise InstallError('owned installation paths cannot redirect to another directory')
    if marker.get('worker_commit'):
        manifest=validate_manifest(marker['manifest'])
        if marker['worker_commit']!=manifest['worker']['commit'] or marker['release_digest']!=hashlib.sha256(canonical(manifest).encode()).hexdigest():
            raise InstallError('recorded release identity changed')
    return marker


@contextmanager
def installation_lock(root):
    with (root/'installation.lock').open('a') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as error:raise InstallError('worker or installation is running; drain the worker before updating') from error
        yield


@contextmanager
def stopped_and_drained(root):
    with (root/'data'/'worker.lock').open('a') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as error:raise InstallError('worker is still running; request a drain and wait for it to exit') from error
        path=root/'data'/'member-worker.sqlite3'
        if path.exists():
            connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
            try:
                if connection.execute('PRAGMA user_version').fetchone()[0]!=1:
                    raise InstallError('worker evidence requires a compatible release')
                if connection.execute('SELECT 1 FROM requests WHERE finished=0 LIMIT 1').fetchone():
                    raise InstallError('saved work is unfinished; drain it using the installed release before updating')
            finally:connection.close()
        yield


def build_release(root,manifest):
    release=manifest['worker'];target=root/'releases'/release['commit']
    if target.is_symlink():raise InstallError('release directory cannot redirect to another installation')
    if target.exists():
        verify_checkout(target/'code',release)
        if not (target/'venv/bin/python').is_file():raise InstallError('existing release environment is incomplete')
        return target
    stage=Path(tempfile.mkdtemp(prefix='.install-',dir=root/'releases'))
    try:
        code=stage/'code';code.mkdir()
        command(['git','init','--quiet'],cwd=code)
        command(['git','remote','add','origin',release['repository']],cwd=code)
        command(['git','fetch','--quiet','--depth=1','origin','refs/tags/'+release['tag']],cwd=code)
        actual=command(['git','rev-parse','FETCH_HEAD^{commit}'],cwd=code)
        if actual!=release['commit']:raise InstallError('release tag does not resolve to its recorded commit')
        command(['git','-c','advice.detachedHead=false','checkout','--quiet','--detach',actual],cwd=code)
        verify_checkout(code,release)
        command([sys.executable,'-m','venv',str(stage/'venv')])
        command([str(stage/'venv/bin/python'),'-m','pip','install','--disable-pip-version-check','-r',str(code/'requirements-v2.txt')])
        command([str(stage/'venv/bin/python'),'-c','import worker_v2'],cwd=code)
        atomic(stage/'release.json',canonical(manifest)+'\n')
        # venv entry-point shebangs contain their creation path. Keep the build at
        # its permanent location through a rename-safe Python invocation; no pip
        # or activation-script entry points are used when running the worker.
        os.replace(stage,target);sync(target.parent)
        return target
    finally:
        if stage.exists():shutil.rmtree(stage)  # Only this operation's fresh temporary build.


LAUNCHER='''#!/usr/bin/env python3
import fcntl,hashlib,json,os,re,stat,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parent
handle=(root/'installation.lock').open('a')
try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError:raise SystemExit('worker or installation is already running')
# Keep this lock for the worker lifetime, including the exec boundary. Docker
# subprocesses close unrelated descriptors, so only the worker retains it.
os.set_inheritable(handle.fileno(),True)
record=json.loads((root/'installation-v2.json').read_text())
if record.get('kind')!='innopool-v2-worker' or record.get('directory')!=str(root):raise SystemExit('invalid installation identity')
if not re.fullmatch(r'[0-9a-f]{40}',record['worker_commit']):raise SystemExit('invalid installed revision')
release=root/'releases'/record['worker_commit']
manifest=record['manifest']
if hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()!=record['release_digest']:raise SystemExit('release metadata changed')
if record['worker_commit']!=manifest['worker']['commit']:raise SystemExit('worker revision differs from the paired release')
code=release/'code'
for arguments,expected in ((['remote','get-url','origin'],manifest['worker']['repository']),(['rev-parse','HEAD'],record['worker_commit']),(['status','--porcelain','--untracked-files=normal'],'')):
    result=subprocess.run(['git',*arguments],cwd=code,capture_output=True,text=True,check=True)
    if result.stdout.strip()!=expected:raise SystemExit('installed checkout changed; review before starting')
token=root/'execution-token'
if token.is_symlink() or stat.S_IMODE(token.stat().st_mode)&0o077:raise SystemExit('execution token file must be private')
config=json.loads((root/'worker.json').read_text())
if config['pool_origin']!=record['pool_origin'] or Path(config['data_directory']).resolve()!=root/'data' or config.get('worker_commit')!=record['worker_commit']:raise SystemExit('worker configuration differs from its owned installation or recorded release')
os.environ['POOL_V2_EXECUTION_TOKEN']=token.read_text().strip()
os.chdir(code)
python=str(release/'venv/bin/python')
os.execv(python,[python,'-m','worker_v2','--config',str(root/'worker.json'),*sys.argv[1:]])
'''


def install(root,pool,manifest,*,resource=None,compute_type=None,workers=1,execution_token=None,update=False):
    if root.is_symlink():raise InstallError('installation destination cannot be a symlink')
    root=root.resolve();pool=origin(pool);manifest=validate_manifest(manifest)
    marker=None
    if root.exists():
        if not update:raise InstallError('destination already exists; use an explicit update for an owned installation')
        marker=read_installation(root)
        if marker['pool_origin']!=pool:raise InstallError('an update cannot switch pools or member evidence directories')
    elif update:raise InstallError('there is no installation to update')
    else:
        if not execution_token or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}',execution_token):
            raise InstallError('a member execution token is required')
        cpu={'aws_t3','aws_t3a','aws_t4g','aws_c7i','aws_c7a','aws_c7g','aws_m7i','aws_m7a','aws_m7g'}
        if resource not in ('CPU','GPU') or (compute_type not in cpu if resource=='CPU' else compute_type!='aws_g4dn'):
            raise InstallError('offer CPU with a CPU verification type, or GPU with aws_g4dn')
        machine=platform.machine().lower()
        if machine not in ('x86_64','amd64','aarch64','arm64') or ((compute_type in {'aws_t4g','aws_c7g','aws_m7g'})!=(machine in ('aarch64','arm64'))):
            raise InstallError('verification compute type must match this machine architecture')
        if type(workers) is not int or not 1<=workers<=4096:raise InstallError('worker capacity must be between 1 and 4096')
        root.mkdir(mode=0o700,parents=True)
        (root/'data').mkdir(mode=0o700);(root/'releases').mkdir(mode=0o700)
        marker={'version':1,'kind':'innopool-v2-worker','directory':str(root),'pool_origin':pool}
        # Ownership is established before building. A failed install can be
        # retried explicitly without mistaking another application's files for ours.
        atomic(root/MARKER,canonical(marker)+'\n')
        config={'pool_origin':pool,'data_directory':str(root/'data'),'resource':resource,
            'compute_type':compute_type,'workers':workers,'worker_commit':manifest['worker']['commit']}
        atomic(root/'worker.json',canonical(config)+'\n');atomic(root/'execution-token',execution_token+'\n')
    with installation_lock(root),stopped_and_drained(root):
        if marker.get('worker_commit'):
            previous=marker['manifest']
            verify_checkout(root/'releases'/marker['worker_commit']/'code',previous['worker'])
        config=json.loads((root/'worker.json').read_text())
        if config['pool_origin']!=pool or Path(config['data_directory']).resolve()!=root/'data':
            raise InstallError('existing configuration points outside its owned installation')
        target=build_release(root,manifest)
        # Images arrive inside each assignment now; an older installation's copy is retired on update.
        config.pop('runtime_images',None)
        config['worker_commit']=manifest['worker']['commit']
        marker.update(worker_commit=manifest['worker']['commit'],pool_commit=manifest['pool']['commit'],
            release_digest=hashlib.sha256(canonical(manifest).encode()).hexdigest(),manifest=manifest)
        atomic(root/'worker.json',canonical(config)+'\n')
        atomic(root/'run',LAUNCHER,0o700)
        atomic(root/MARKER,canonical(marker)+'\n')
        (root/'data'/'drain.request').unlink(missing_ok=True);sync(root/'data')
    return root/'run'


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('install','update','drain'))
    parser.add_argument('--directory',required=True,type=Path)
    parser.add_argument('--pool')
    parser.add_argument('--resource',choices=('CPU','GPU'))
    parser.add_argument('--compute-type')
    parser.add_argument('--workers',type=int,default=1)
    args=parser.parse_args(argv)
    try:
        root=args.directory.expanduser().absolute()
        if root.is_symlink():raise InstallError('installation directory cannot be a symlink')
        root=root.resolve()
        if args.action=='drain':
            read_installation(root)
            atomic(root/'data'/'drain.request','Drain requested by member\n')
            print('Drain requested. The installed worker will finish saved work and exit.');return 0
        pool=args.pool or (read_installation(root)['pool_origin'] if args.action=='update' else None)
        if not pool:raise InstallError('--pool is required for a new installation')
        manifest=release_from_pool(pool)
        token=None
        if args.action=='install':token=os.environ.get('POOL_V2_EXECUTION_TOKEN') or getpass.getpass('Member execution token: ')
        launcher=install(root,pool,manifest,resource=args.resource,compute_type=args.compute_type,workers=args.workers,
            execution_token=token,update=args.action=='update')
        print('Installed the recorded worker release. Start it with: '+str(launcher))
        return 0
    except (InstallError,KeyError,TypeError,OSError,sqlite3.Error,json.JSONDecodeError) as error:
        print('Installation stopped: '+(str(error) if isinstance(error,InstallError) else type(error).__name__),file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
