import json
import tempfile
import unittest
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from exam_bench.common import tree_manifest
from tools.community_results import operator, validate_bundle, export_public, make_pr, write
from tools.serve_result_site import StaticHandler


def fixture(root, name='demo-test'):
    f = root / 'community/results' / name
    (f / 'src').mkdir(parents=True)
    (f / 'src/Configuration.xml').write_text('<Configuration/>')
    (f / 'solution.dt').write_bytes(b'fixture, not a platform DT')
    (f / 'README.txt').write_text('test fixture')
    for file in ('binding.json', 'calibration.json', 'assessment.json', 'grading-implementation.json'):
        write(f / file, {})
    write(f / 'measurement.json', {'operator': {'name': 'Test author', 'github': 'tester'},
          'environment': {'capture_timing': 'before_run', 'hardware': {'cpu': 'Test CPU'}, 'os': {'system': 'TestOS'}}})
    write(f / 'run.json', {'model': 'gpt-test'})
    write(f / 'evaluation.json', {'run_id': name, 'score': None, 'score_upper_bound': '0.5'})
    write(f / 'manifest.json', {'schema_version': 1, 'submission_id': name, 'run_id': name,
          'verification': 'contributor_reported', 'files': tree_manifest(f)})
    return f


class CommunityBundle(unittest.TestCase):
    def test_requires_complete_immutable_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            f = fixture(Path(td))
            validate_bundle(f)
            (f / 'solution.dt').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                validate_bundle(f)

    def test_missing_dt_cannot_be_hidden_by_rehashing(self):
        with tempfile.TemporaryDirectory() as td:
            f = fixture(Path(td))
            (f / 'solution.dt').unlink()
            m = json.loads((f / 'manifest.json').read_text())
            m['files'].pop('solution.dt')
            write(f / 'manifest.json', m)
            with self.assertRaisesRegex(ValueError, 'missing'):
                validate_bundle(f)

    def test_private_files_not_permitted_even_with_valid_hash(self):
        with tempfile.TemporaryDirectory() as td:
            f = fixture(Path(td))
            (f / 'auth.json').write_text('{"secret":"fixture"}')
            m = json.loads((f / 'manifest.json').read_text())
            m['files'] = tree_manifest(f); m['files'].pop('manifest.json')
            write(f / 'manifest.json', m)
            with self.assertRaisesRegex(ValueError, 'Unsupported artifact'):
                validate_bundle(f)

    def test_export_retains_upper_bound_and_attribution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); fixture(root)
            export_public(root, root / 'dist')
            d = json.loads((root / 'dist/community.json').read_text())['results'][0]
            self.assertIsNone(d['result']['score'])
            self.assertEqual(d['result']['score_upper_bound'], '0.5')
            self.assertEqual(d['measurement']['operator']['github'], 'tester')
            self.assertTrue((root / 'dist' / d['download']).is_file())

    def test_unclamped_negative_bound_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as td:
            f = fixture(Path(td))
            result = json.loads((f / 'evaluation.json').read_text()); result['score_upper_bound'] = '-2.0'
            write(f / 'evaluation.json', result)
            m = json.loads((f / 'manifest.json').read_text()); m['files'] = tree_manifest(f); m['files'].pop('manifest.json')
            write(f / 'manifest.json', m)
            validate_bundle(f)

    def test_pr_retry_returns_existing_without_pushing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); f = fixture(root)
            dest = root / '.local/community/prepared/demo-test'; dest.parent.mkdir(parents=True)
            f.rename(dest)
            url = 'https://github.com/zeegin/1c-agent-exam-benchmark/pull/123'
            with patch('tools.community_results.executable', side_effect=lambda x: x), patch('tools.community_results.command', side_effect=['tester', url]) as cmd:
                self.assertEqual(make_pr('demo-test', root), url)
                self.assertEqual(cmd.call_count, 2)

    def test_pr_pushes_only_bundle_in_separate_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); f = fixture(root)
            dest = root / '.local/community/prepared/demo-test'; dest.parent.mkdir(parents=True)
            f.rename(dest)
            url = 'https://github.com/zeegin/1c-agent-exam-benchmark/pull/123'
            outputs = ['tester', '', '', 'https', '', 'main', '', '', '', '', '', '', url]
            with patch('tools.community_results.executable', side_effect=lambda x:x), patch('tools.community_results.command', side_effect=outputs) as cmd:
                self.assertEqual(make_pr('demo-test', root), url)
                calls = [c.args for c in cmd.call_args_list]
                fork = next(c[0] for c in calls if c[0][1:3] == ['repo', 'fork'])
                self.assertIn('--clone=false', fork)
                self.assertFalse(any(arg.startswith('--remote') for arg in fork))
                clone = next(c[0] for c in calls if 'clone' in c[0])
                self.assertEqual(clone[0], 'git')
                self.assertLess(clone.index('-c'), clone.index('clone'))
                self.assertNotIn('-c', clone[clone.index('clone') + 1:])
                add = next(c for c in calls if 'add' in c[0])
                self.assertEqual(add[0][-4:], ['add', '-f', '--', 'community/results/demo-test'])
                self.assertEqual(add[1], root / '.local/community/checkouts/demo-test')
                self.assertTrue((add[1] / 'community/results/demo-test/solution.dt').is_file())

    def test_pr_clone_preserves_github_cli_transport(self):
        for protocol, expected in (('ssh', 'git@github.com:tester/1c-agent-exam-benchmark.git'),
                                   ('https', 'https://github.com/tester/1c-agent-exam-benchmark.git')):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as td:
                root = Path(td); f = fixture(root)
                dest = root / '.local/community/prepared/demo-test'; dest.parent.mkdir(parents=True)
                f.rename(dest)
                url = 'https://github.com/zeegin/1c-agent-exam-benchmark/pull/123'
                def run(args, cwd=root):
                    if args[1:3] == ['api', 'user']: return 'tester'
                    if args[1:3] == ['config', 'get']: return protocol
                    if args[1:3] == ['repo', 'view']: return 'main'
                    if args[1:3] == ['pr', 'create']: return url
                    return ''
                with patch('tools.community_results.executable', side_effect=lambda x:x), patch('tools.community_results.command', side_effect=run) as cmd:
                    self.assertEqual(make_pr('demo-test', root), url)
                    clone = next(c.args[0] for c in cmd.call_args_list if 'clone' in c.args[0])
                    self.assertIn(expected, clone)

    def test_operator_is_explicit_and_login_is_not_a_command(self):
        for data in ({'name': ''}, {'name': 'A', 'github': 'https://github.com/x'}, {'name': 'A', 'github': '--help'}, {'name': 'a\nb'}):
            with self.assertRaises(ValueError): operator(data)


class CommunityHTTP(unittest.TestCase):
    def test_csrf_and_origin_checked_before_any_worker_starts(self):
        handler = type('CommunityTestHandler', (StaticHandler,), {'controller': None, 'community_token': 'test-csrf', 'log_message': lambda *a: None})
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/api/community/prepare'
            for headers in ({}, {'X-Demo-Token': 'test-csrf', 'Origin': 'https://unrelated.example'}):
                req = urllib.request.Request(url, data=b'{"id":"demo-test"}', headers={'Content-Type':'application/json', **headers})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(req)
                self.assertEqual(error.exception.code, 403)
            with patch('tools.community_service.start', return_value={'started': True}) as start:
                req = urllib.request.Request(url, data=b'{"id":"demo-test"}', headers={'Content-Type':'application/json', 'X-Demo-Token': 'test-csrf'})
                with urllib.request.urlopen(req) as response: self.assertEqual(response.status, 202)
                start.assert_called_once()
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
