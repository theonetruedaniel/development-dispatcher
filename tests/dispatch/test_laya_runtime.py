import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.dispatch.laya_runtime import validate_profile, load_profile, sandbox_argv


def fixture():
    mounts = [dict(source='/usr/bin/python3.14', target='/usr/bin/python3.14'),
        dict(source='/usr/bin/python3.14', target='/runtime/bin/python'),
        dict(source='/candidate/packages', target='/runtime/lib/python3.14/site-packages'),
        dict(source='/candidate/pyvenv.cfg', target='/runtime/pyvenv.cfg'),
        dict(source='/candidate/checkpoint', target='/checkpoint'),
        dict(source='/candidate/worker.py', target='/worker.py')]
    roots = sorted({m['source'] for m in mounts})
    entries = {root: dict(type='directory' if root.endswith(('packages','checkpoint')) else 'file')
               for root in roots}
    return dict(schema=1, checkpoint_revision='a'*40, mounts=mounts,
                inventory=dict(schema=1, roots=roots, entries=entries))


class LayaRuntime(unittest.TestCase):
    def test_fixed_offline_launch_and_no_writable_host_mount(self):
        profile = fixture()
        argv = sandbox_argv(profile)
        self.assertIn('--unshare-all', argv)
        self.assertIn('--die-with-parent', argv)
        self.assertNotIn('--bind', argv)
        self.assertNotIn('--share-net', argv)
        self.assertEqual(argv[-7:], ['/runtime/bin/python','-I','/worker.py',
            '--checkpoint','/checkpoint','--revision','a'*40])
        for setting in ('HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE'):
            pos = argv.index(setting)
            self.assertEqual(argv[pos-1:pos+2], ['--setenv',setting,'1'])
        self.assertEqual(profile, fixture())

    def test_broad_redirected_duplicate_or_uninventoried_mounts_rejected(self):
        for target in ('/usr','/usr/lib','/work','/home','/tmp','/proc',
                       '/usr/lib/../private','/usr/lib//x','/usr/lib/x\n'):
            changed = fixture()
            changed['mounts'][0]['target'] = target
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_profile(changed)
        for change in ('unknown_source','duplicate_target','directory_system','extra_inventory','missing_root'):
            changed=fixture()
            if change=='unknown_source':changed['mounts'][0]['source']='/private/secret'
            elif change=='duplicate_target':changed['mounts'].append(copy.deepcopy(changed['mounts'][0]))
            elif change=='directory_system':changed['inventory']['entries']['/usr/bin/python3.14']['type']='directory'
            elif change=='extra_inventory':changed['inventory']['entries']['/private/secret']={'type':'file'}
            else:changed['inventory']['roots'].pop()
            with self.subTest(change=change), self.assertRaises(ValueError):validate_profile(changed)

    def test_mount_source_cannot_be_parent_of_another_source(self):
        profile=fixture()
        profile['mounts'][4]['source']='/candidate/packages/checkpoint'
        profile['inventory']['roots']=sorted({m['source'] for m in profile['mounts']})
        profile['inventory']['entries']['/candidate/packages/checkpoint']=profile['inventory']['entries'].pop('/candidate/checkpoint')
        with self.assertRaises(ValueError):validate_profile(profile)

    def test_missing_runtime_components_and_authority_fields_rejected(self):
        for index in range(1,6):
            profile=fixture();profile['mounts'].pop(index)
            with self.subTest(index=index), self.assertRaises(ValueError):validate_profile(profile)
        profile=fixture();profile['qualified']=True
        with self.assertRaises(ValueError):validate_profile(profile)
        profile=fixture();profile['checkpoint_revision']='main'
        with self.assertRaises(ValueError):validate_profile(profile)

    def test_hash_and_inventory_verified_before_return(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'profile.json'
            raw=json.dumps(fixture()).encode();path.write_bytes(raw)
            digest=hashlib.sha256(raw).hexdigest()
            with patch('scripts.dispatch.laya_runtime.verify_inventory') as verify:
                self.assertEqual(load_profile(path,digest),fixture())
                verify.assert_called_once_with(fixture()['inventory'])
                verify.reset_mock()
                with self.assertRaises(ValueError):load_profile(path,'0'*64)
                verify.assert_not_called()
            with patch('scripts.dispatch.laya_runtime.verify_inventory',side_effect=ValueError('drift')):
                with self.assertRaises(ValueError):load_profile(path,digest)

    def test_duplicate_json_keys_and_profile_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'profile.json'
            raw=b'{"schema":1,"schema":1}';path.write_bytes(raw)
            with self.assertRaises(ValueError):load_profile(path,hashlib.sha256(raw).hexdigest())
            with patch.object(Path,'is_symlink',return_value=True), self.assertRaises(ValueError):
                load_profile(path,hashlib.sha256(raw).hexdigest())
