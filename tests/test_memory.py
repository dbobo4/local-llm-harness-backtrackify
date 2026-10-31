"""Shared instructions at the existing setup/run boundary; core code is unchanged."""
from contextlib import nullcontext
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import harness
import install
from harness import atomic, Refused, Task
from tests.test_install import PathStore


class MemoryTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='Backtrackify memory spaces ');self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.assets=self.root/'external assets';self.model=self.assets/'models/model.gguf'

    def installed_files(self):
        atomic(self.root/'local.ps1','Existing settings')
        for path in install.required(self.assets,self.model,self.root):atomic(path,'verified fixture')

    def command(self,action):
        with patch('install.ROOT',self.root),patch.dict('os.environ',LOCALAPPDATA=str(self.root/'home')),patch('sys.argv',['install.py',action,str(self.assets),str(self.model)]):
            install.main()

    def test_setup_readiness_creates_default_instructions_and_keeps_custom_text(self):
        self.installed_files();self.command('ready')
        self.assertEqual((self.root/'memory.txt').read_text(),'Keep implementation simple.\nPreserve the original user goal.\n')
        atomic(self.root/'memory.txt','Use my explicit rules.\nUnicode \u2603\n')
        self.command('ready');self.command('check')
        self.assertEqual((self.root/'memory.txt').read_text(encoding='utf-8'),'Use my explicit rules.\nUnicode \u2603\n')
        self.assertFalse((self.assets/'memory.txt').exists())

    def test_existing_user_text_and_non_file_paths_are_preserved(self):
        atomic(self.root/'memory.txt','Existing user text')
        install.shared_instructions(self.root);install.shared_instructions(self.root)
        self.assertEqual((self.root/'memory.txt').read_text(),'Existing user text')
        (self.root/'memory.txt').unlink();(self.root/'memory.txt').mkdir()
        with self.assertRaisesRegex(Refused,'not a file'):install.shared_instructions(self.root)

    def test_failed_instruction_write_cannot_mark_setup_ready(self):
        self.installed_files()
        with patch('install.shared_instructions',side_effect=OSError('read only')),self.assertRaises(OSError):self.command('ready')
        self.assertFalse((self.root/'runtime/installed.log').exists())

    def test_default_uninstall_preserves_shared_instructions_and_external_assets(self):
        self.installed_files();self.command('ready');atomic(self.root/'memory.txt','User instructions')
        store=PathStore('unrelated');home=self.root/'home/Backtrackify'
        install.registration(self.root,home,store,search='')
        with patch('install.UserPath',return_value=store):self.command('uninstall')
        self.assertEqual(store.value,'unrelated');self.assertEqual((self.root/'memory.txt').read_text(),'User instructions')
        self.assertTrue(self.model.exists());self.assertFalse((self.root/'runtime/installed.log').exists())
        with patch('install.UserPath',return_value=store):self.command('uninstall')
        self.assertEqual((self.root/'memory.txt').read_text(),'User instructions')

    def run_session(self,name,goal='Original user goal',resume=False):
        session=self.root/'sessions'/name;captured=[]
        project=self.root/'user-project';project.mkdir(exist_ok=True)
        atomic(project/'memory.txt','Project text is not the ROOT source')
        args=['run','--server','unused.exe','--qwen','unused.js','--model','unused.gguf','--session',str(session)]
        if not resume:args+=[goal,'--project',str(project)]
        def inspect(h):captured.append(h);h.requirements();return 0
        with patch('harness.ROOT',self.root),patch('harness.gguf_metadata',return_value={'qwen35.context_length':32768,'qwen35.nextn_predict_layers':1}),patch('backend.connection',return_value=nullcontext(SimpleNamespace(url='http://unused',vision=True))),patch.object(harness.Harness,'run',inspect),patch('visualize.viewer_active',return_value=False):
            self.assertEqual(harness.main(args),0)
        return captured[0]

    def test_standalone_creation_snapshots_root_text_for_all_existing_role_contexts(self):
        atomic(self.root/'memory.txt','Keep the public API.\nPython 3.12.\n')
        h=self.run_session('a');memory=(h.session/'memory.txt').read_text()
        self.assertIn('Keep the public API.',h.plan_context())
        for role in ('ALGORITHM','TEST'):
            self.assertIn(memory,h.context(Task(1,0,role,'','Assigned scope'),h.session/'review'))
        self.assertNotIn('Project text',h.requirements())
        self.assertIn('Project text',(h.workspace/'memory.txt').read_text())
        self.assertEqual((h.session/'goal.txt').read_text(),'Original user goal\n')

    def test_root_changes_affect_only_new_sessions_and_resume_keeps_its_snapshot(self):
        atomic(self.root/'memory.txt','First rules');first=self.run_session('a');saved=(first.session/'memory.txt').read_bytes()
        atomic(self.root/'memory.txt','Second rules');second=self.run_session('b',goal='Another goal')
        resumed=self.run_session('a',resume=True)
        self.assertEqual((resumed.session/'memory.txt').read_bytes(),saved)
        self.assertIn('First rules',resumed.requirements());self.assertNotIn('Second rules',resumed.requirements())
        self.assertIn('Second rules',second.requirements());self.assertEqual((resumed.session/'goal.txt').read_text(),'Original user goal\n')

    def test_existing_size_bound_refuses_oversized_shared_instructions_without_truncation(self):
        atomic(self.root/'memory.txt','x'*20000)
        with self.assertRaisesRegex(Refused,'too large'):self.run_session('large')
        self.assertIn('x'*20000,(self.root/'sessions/large/memory.txt').read_text())


if __name__=='__main__':unittest.main()
