from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from worker_v2.state import Store


spec=importlib.util.spec_from_file_location('worker_installer',Path(__file__).resolve().parents[2]/'tools/install_worker_v2.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='innopool-v2-installer-test-')
        self.addCleanup(self.temporary.cleanup)
        self.base=Path(self.temporary.name);self.root=self.base/'member-installation'
        self.repo=self.base/'fixture-repository';self.repo.mkdir()
        self.git('init','--quiet');self.git('config','user.name','Fixture');self.git('config','user.email','fixture@example.invalid')
        (self.repo/'worker_v2').mkdir()
        (self.repo/'tools').mkdir();(self.repo/'tools/install_worker_v2.py').write_bytes(Path(installer.__file__).read_bytes())
        (self.repo/'worker_v2/__init__.py').write_text('')
        (self.repo/'requirements-v2.txt').write_text('# Empty dependency fixture: the installer still builds a real isolated venv.\n')
        (self.repo/'.gitignore').write_text('__pycache__/\n*.pyc\n')
        self.first=self.revision('one')
        self.second=self.revision('two')
        repository=patch.object(installer,'WORKER_REPOSITORY',str(self.repo));repository.start();self.addCleanup(repository.stop)
        self.token='fixture_execution_token_'+'x'*32
        self.compute='aws_c7g' if platform.machine().lower() in ('aarch64','arm64') else 'aws_c7a'

    def git(self,*arguments):
        return subprocess.run(['git',*arguments],cwd=self.repo,check=True,capture_output=True,text=True).stdout.strip()

    def revision(self,name):
        (self.repo/'worker_v2/__main__.py').write_text('''import json,os,sys,time
from pathlib import Path
config=json.loads(Path(sys.argv[sys.argv.index('--config')+1]).read_text())
if not os.environ.get('POOL_V2_EXECUTION_TOKEN'):raise SystemExit('missing execution token')
directory=Path(config['data_directory'])
if '--hold' in sys.argv:
    (directory/'fixture-running').write_text('ready')
    end=time.monotonic()+15
    while not (directory/'drain.request').exists() and time.monotonic()<end:time.sleep(.02)
print('fixture-revision='''+name+'''')
''')
        self.git('add','.');self.git('commit','--quiet','-m','Fixture '+name)
        commit=self.git('rev-parse','HEAD');self.git('tag','v2-fixture-'+name)
        return {'repository':str(self.repo),'commit':commit,'tag':'v2-fixture-'+name,'state_version':1,
            'installer_sha256':hashlib.sha256(Path(installer.__file__).read_bytes()).hexdigest()}

    def manifest(self,worker=None):
        return {'manifest_version':1,'api_version':'2.0','pool':{'repository':installer.POOL_REPOSITORY,
            'commit':'a'*40,'tag':'v2-pool-fixture'},'worker':worker or self.first,'runtime_version':'0.0.8',
            'runtime_images':{'c001':'ghcr.io/tig-foundation/tig-monorepo/satisfiability/runtime@sha256:'+'b'*64}}

    def install(self,manifest=None):
        return installer.install(self.root,'https://pool.example',manifest or self.manifest(),
            resource='CPU',compute_type=self.compute,workers=2,execution_token=self.token)

    def run_worker(self,*args):
        return subprocess.run([str(self.root/'run'),*args],capture_output=True,text=True,timeout=20,
            env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})

    def test_install_uses_recorded_tag_and_restart_keeps_its_commit_and_private_token(self):
        self.install()
        self.assertEqual(installer.read_installation(self.root)['worker_commit'],self.first['commit'])
        self.assertEqual(stat.S_IMODE((self.root/'execution-token').stat().st_mode),0o600)
        for _ in range(2):
            result=self.run_worker()
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),'fixture-revision=one')
            self.assertNotIn(self.token,result.stdout+result.stderr)
        self.assertEqual(self.git('rev-parse','HEAD'),self.second['commit'])  # Upstream advanced before install.
        config=json.loads((self.root/'worker.json').read_text())
        self.assertEqual((config['resource'],config['workers'],config['data_directory'],config['worker_commit']),
            ('CPU',2,str(self.root/'data'),self.first['commit']))
        self.assertNotIn('runtime_images',config)  # the pool names the image inside each assignment
        self.assertFalse((self.root/'releases'/self.first['commit']/'code'/'execution-token').exists())
        wrapper=Path(__file__).resolve().parents[2]/'scripts/start-fresh.sh'
        started=subprocess.run(['bash',str(wrapper),str(self.root)],capture_output=True,text=True,timeout=20,
            env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        self.assertEqual((started.returncode,started.stdout.strip()),(0,'fixture-revision=one'),started.stderr)

    def test_existing_unknown_directory_and_moved_release_tag_are_not_adopted(self):
        self.root.mkdir();original=self.root/'original-config';original.write_text('retain this')
        with self.assertRaises(installer.InstallError):self.install()
        self.assertEqual(original.read_text(),'retain this')
        with self.assertRaises(installer.InstallError):installer.install(self.root,'https://pool.example',self.manifest(),update=True)
        other=self.base/'wrong-tag'
        bad=self.manifest();bad['worker']={**self.first,'tag':self.second['tag']}
        with self.assertRaisesRegex(installer.InstallError,'tag'):
            installer.install(other,'https://pool.example',bad,resource='CPU',compute_type=self.compute,execution_token=self.token)
        self.assertFalse((other/'releases'/self.first['commit']).exists())
        # An interrupted fresh build can be retried from its explicit ownership marker.
        installer.install(other,'https://pool.example',self.manifest(),update=True)
        self.assertEqual(installer.read_installation(other)['worker_commit'],self.first['commit'])
        mismatch={**self.first,'installer_sha256':'0'*64}
        with self.assertRaisesRegex(installer.InstallError,'checksum'):
            installer.verify_checkout(other/'releases'/self.first['commit']/'code',mismatch)

    def test_update_requires_drained_work_and_preserves_configuration_token_and_evidence(self):
        self.install()
        config=json.loads((self.root/'worker.json').read_text());config['workers']=3
        config['runtime_limits']={'cpu_millis':1000,'memory_mib':1280,'pids_limit':128,'cgroup_parent':'innopoolv2pilot.slice'}
        config['runtime_images']={'c001':'an older installation copied the release images into its configuration'}
        (self.root/'worker.json').write_text(json.dumps(config))
        store=Store(self.root/'data');request=store.request({'resource':'CPU'});store.close()
        evidence=self.root/'data'/'retained-arbitration-evidence';evidence.write_text('retain through settlement')
        with self.assertRaisesRegex(installer.InstallError,'unfinished'):
            installer.install(self.root,'https://pool.example',self.manifest(self.second),update=True)
        store=Store(self.root/'data');store.finish(request['request_key'],'expired');store.close()
        installer.install(self.root,'https://pool.example',self.manifest(self.second),update=True)
        self.assertEqual(json.loads((self.root/'worker.json').read_text())['workers'],3)
        self.assertEqual(json.loads((self.root/'worker.json').read_text())['runtime_limits'],config['runtime_limits'])
        self.assertEqual(json.loads((self.root/'worker.json').read_text())['worker_commit'],self.second['commit'])
        self.assertNotIn('runtime_images',json.loads((self.root/'worker.json').read_text()))
        self.assertEqual((self.root/'execution-token').read_text().strip(),self.token)
        self.assertEqual(evidence.read_text(),'retain through settlement')
        self.assertTrue((self.root/'releases'/self.first['commit']).is_dir())
        self.assertEqual(self.run_worker().stdout.strip(),'fixture-revision=two')

    def test_running_launcher_blocks_upgrade_until_a_drain_finishes(self):
        self.install()
        process=subprocess.Popen([str(self.root/'run'),'--hold'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
            env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        def stop():
            if process.poll() is None:process.terminate()
            process.communicate(timeout=10)
        self.addCleanup(stop)
        deadline=time.monotonic()+5
        while not (self.root/'data'/'fixture-running').exists() and time.monotonic()<deadline:time.sleep(.02)
        self.assertIsNone(process.poll())
        self.assertTrue((self.root/'data'/'fixture-running').exists())
        with self.assertRaisesRegex(installer.InstallError,'running'):
            installer.install(self.root,'https://pool.example',self.manifest(self.second),update=True)
        self.assertEqual(installer.main(['drain','--directory',str(self.root)]),0)
        stdout,stderr=process.communicate(timeout=10)
        self.assertEqual(process.returncode,0,stderr)
        installer.install(self.root,'https://pool.example',self.manifest(self.second),update=True)
        self.assertFalse((self.root/'data'/'drain.request').exists())

    def test_interrupted_activation_fails_closed_and_an_explicit_retry_recovers(self):
        self.install()
        next_manifest=self.manifest(self.second)
        next_manifest['runtime_version']='0.0.9'
        write=installer.atomic
        def fail_marker(path,*args,**kwargs):
            if path==self.root/installer.MARKER:raise OSError('fixture lost power before activation')
            return write(path,*args,**kwargs)
        with patch.object(installer,'atomic',side_effect=fail_marker),self.assertRaises(OSError):
            installer.install(self.root,'https://pool.example',next_manifest,update=True)
        self.assertNotEqual(self.run_worker().returncode,0)
        installer.install(self.root,'https://pool.example',next_manifest,update=True)
        self.assertEqual(self.run_worker().stdout.strip(),'fixture-revision=two')

    def test_changed_checkout_or_redirected_data_cannot_be_overwritten_by_update(self):
        self.install()
        code=self.root/'releases'/self.first['commit']/'code'
        changed=code/'requirements-v2.txt';changed.write_text('member edit retained\n')
        with self.assertRaisesRegex(installer.InstallError,'changes'):
            installer.install(self.root,'https://pool.example',self.manifest(self.second),update=True)
        self.assertEqual(changed.read_text(),'member edit retained\n')
        self.assertNotEqual(self.run_worker().returncode,0)
        redirected=self.base/'unrelated-data';redirected.mkdir()
        (self.root/'data').rename(self.root/'saved-data')
        (self.root/'data').symlink_to(redirected,target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError,'redirect'):
            installer.install(self.root,'https://pool.example',self.manifest(self.second),update=True)
        self.assertEqual(list(redirected.iterdir()),[])

    def test_pool_and_manifest_must_identify_the_same_compatible_release(self):
        manifest=self.manifest();digest=hashlib.sha256(installer.canonical(manifest).encode()).hexdigest()
        caps={'api_version':'2.0','assignment_unit':'whole-benchmark','pool_commit':'a'*40,'release_digest':digest}
        with patch.object(installer,'fetch',side_effect=[caps,manifest]):
            self.assertEqual(installer.release_from_pool('https://pool.example'),manifest)
        for invalid in ({**caps,'api_version':'1.0'},{**caps,'release_digest':'0'*64}):
            with patch.object(installer,'fetch',side_effect=[invalid,manifest]),self.assertRaises(installer.InstallError):
                installer.release_from_pool('https://pool.example')
        changed=deepcopy(manifest);changed['worker']['installer_sha256']='0'*64
        changed_caps={**caps,'release_digest':hashlib.sha256(installer.canonical(changed).encode()).hexdigest()}
        with patch.object(installer,'fetch',side_effect=[changed_caps,changed]),self.assertRaisesRegex(installer.InstallError,'current installer'):
            installer.release_from_pool('https://pool.example')
        wrong=deepcopy(manifest);wrong['worker']['repository']='https://github.com/example/other-worker.git'
        with self.assertRaises(installer.InstallError):installer.validate_manifest(wrong)
        wrong=deepcopy(manifest);wrong['runtime_version']='latest'
        with self.assertRaises(installer.InstallError):installer.validate_manifest(wrong)
        # The served pins are informational to the installer: a release without them, or with more, installs.
        installer.validate_manifest({key:value for key,value in manifest.items() if key!='runtime_images'})
        installer.validate_manifest({**manifest,'runtime_images':{**manifest['runtime_images'],'c005':'ghcr.io/tig-foundation/tig-monorepo/hypergraph/runtime@sha256:'+'c'*64}})

    def test_wrong_machine_type_is_rejected_before_installing_or_offering_work(self):
        from worker_v2.runner import Runner
        from worker_v2.state import StateError
        with patch.object(installer.platform,'machine',return_value='aarch64'),self.assertRaises(installer.InstallError):
            installer.install(self.root,'https://pool.example',self.manifest(),resource='CPU',compute_type='aws_t3',execution_token=self.token)
        self.assertFalse(self.root.exists())
        class ArmRuntime:arch='arm64'
        with self.assertRaises(StateError):Runner(None,None,ArmRuntime(),resource='CPU',compute_type='aws_t3')
        with self.assertRaises(StateError):Runner(None,None,ArmRuntime(),resource='GPU',compute_type='aws_c7g')

    def test_worker_entrypoint_honors_a_saved_drain_request(self):
        from worker_v2 import __main__ as entry
        directory=self.base/'draining-data';directory.mkdir();(directory/'drain.request').write_text('requested')
        config={'pool_origin':'https://pool.example','data_directory':str(directory),'resource':'CPU',
            'compute_type':self.compute,'workers':1,'worker_commit':self.first['commit']}
        path=self.base/'draining.json';path.write_text(json.dumps(config))
        with patch.dict(os.environ,{'POOL_V2_EXECUTION_TOKEN':self.token}),patch.object(entry,'Runner') as runner,\
                patch.object(entry.signal,'signal'),patch.object(entry.logging,'basicConfig'):
            runner.return_value.step.return_value='idle'
            self.assertEqual(entry.main(['--config',str(path)]),0)
            runner.return_value.step.assert_called_once_with(drain=True)


if __name__=='__main__':unittest.main()
