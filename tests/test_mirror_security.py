import ast
import email.message
import io
import os
import pathlib
import runpy
import sys
import tempfile
import threading
import types
import unittest
import urllib.request
import urllib.response
from unittest import mock


class MirrorSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        source = pathlib.Path(__file__).parents[1] / 'scripts' / 'mirror.py'
        tree = ast.parse(source.read_text())
        # The legacy script crawls at import time; load only definitions and setup.
        tree.body = tree.body[:next(i for i, node in enumerate(tree.body)
                                    if isinstance(node, ast.Assign) and any(
                                        isinstance(t, ast.Name) and t.id == 'frontier'
                                        for t in node.targets))]
        self.mirror = types.ModuleType('mirror_under_test')
        with mock.patch.object(sys, 'argv', ['mirror.py', str(self.root / 'out')]):
            exec(compile(tree, str(source), 'exec'), self.mirror.__dict__)

    def test_encoded_traversal_cannot_write_outside_output(self):
        response = mock.MagicMock()
        response.read.return_value = b'escaped'
        with mock.patch.object(self.mirror.opener, 'open', return_value=response):
            _, _, error = self.mirror.fetch(self.mirror.BASE + '%2e%2e/escaped.txt')
        self.assertIsNotNone(error)
        self.assertFalse((self.root / 'escaped.txt').exists())

    def test_cross_host_redirect_is_never_requested(self):
        visited = []
        def fake_https(handler, request):
            visited.append(request.full_url)
            headers = email.message.Message()
            if len(visited) == 1:
                headers['Location'] = 'https://evil.example/secret'
                response = urllib.response.addinfourl(io.BytesIO(b''), headers, request.full_url, 302)
                response.msg = 'Found'
            else:
                response = urllib.response.addinfourl(io.BytesIO(b'evil'), headers, request.full_url, 200)
                response.msg = 'OK'
            return response
        with mock.patch.object(urllib.request.HTTPSHandler, 'https_open', fake_https), \
             mock.patch.object(urllib.request, '_opener', None):
            self.mirror.opener = urllib.request.build_opener(self.mirror.SameHostRedirect())
            _, _, error = self.mirror.fetch(self.mirror.BASE)
        self.assertIsNotNone(error)
        self.assertEqual(visited, [self.mirror.BASE])
        self.assertFalse((self.root / 'out' / 'index.html').exists())

    def test_failure_report_cannot_follow_symlink(self):
        out = self.root / 'out'
        out.mkdir()
        victim = self.root / 'victim'
        victim.write_text('keep')
        (out / '_fails.txt').symlink_to(victim)
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b''
        source = pathlib.Path(__file__).parents[1] / 'scripts' / 'mirror.py'
        with mock.patch.object(sys, 'argv', ['mirror.py', str(out)]), \
             mock.patch.object(urllib.request, 'build_opener', return_value=opener):
            try:
                runpy.run_path(str(source), run_name='__main__')
            except OSError:
                pass
        self.assertEqual(victim.read_text(), 'keep')

    def test_failure_report_cannot_truncate_hardlink(self):
        out = self.root / 'out'
        out.mkdir()
        victim = self.root / 'victim'
        victim.write_text('keep')
        os.link(victim, out / '_fails.txt')
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b''
        source = pathlib.Path(__file__).parents[1] / 'scripts' / 'mirror.py'
        with mock.patch.object(sys, 'argv', ['mirror.py', str(out)]), \
             mock.patch.object(urllib.request, 'build_opener', return_value=opener):
            runpy.run_path(str(source), run_name='__main__')
        self.assertEqual(victim.read_text(), 'keep')

    def test_encoded_absolute_and_nested_traversal_are_rejected(self):
        for path in ('%2Fetc/passwd', 'nested/%2e%2e/escape', '%2e%2e%2fescape',
                     'nested%2f..%2fescape', '%5c..%5cescape'):
            with self.subTest(path=path):
                with mock.patch.object(self.mirror.opener, 'open') as request:
                    _, _, error = self.mirror.fetch(self.mirror.BASE + path)
                self.assertIsNotNone(error)
                request.assert_not_called()

    def test_symlinked_directory_and_file_are_not_followed(self):
        out = self.root / 'out'
        out.mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'victim.txt').write_text('keep')
        (out / 'linked').symlink_to(outside, target_is_directory=True)
        (out / 'victim.txt').symlink_to(outside / 'victim.txt')
        for path in ('linked/victim.txt', 'victim.txt'):
            with self.subTest(path=path):
                _, _, error = self.mirror.fetch(self.mirror.BASE + path)
                self.assertIsNotNone(error)
        self.assertEqual((outside / 'victim.txt').read_text(), 'keep')

    def test_normal_crawl_discovers_and_saves_local_resources(self):
        out = self.root / 'out'
        pages = {
            self.mirror.BASE: b'<a href="/page.html">page</a><img src="/img/a.png">',
            self.mirror.BASE + 'page.html': b'<a href="https://evil.example/">bad</a>',
            self.mirror.BASE + 'img/a.png': b'png',
        }
        opener = mock.MagicMock()
        def open_fixture(request, timeout):
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = pages[request.full_url]
            return response
        opener.open.side_effect = open_fixture
        source = pathlib.Path(__file__).parents[1] / 'scripts' / 'mirror.py'
        with mock.patch.object(sys, 'argv', ['mirror.py', str(out)]), \
             mock.patch.object(urllib.request, 'build_opener', return_value=opener):
            runpy.run_path(str(source), run_name='__main__')
        self.assertEqual((out / 'index.html').read_bytes(), pages[self.mirror.BASE])
        self.assertEqual((out / 'page.html').read_bytes(), pages[self.mirror.BASE + 'page.html'])
        self.assertEqual((out / 'img/a.png').read_bytes(), b'png')
        self.assertEqual((out / '_fails.txt').read_text(), '')
        self.assertEqual({call.args[0].full_url for call in opener.open.call_args_list}, set(pages))

    def test_failed_write_does_not_publish_partial_cache(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'complete contents'
        real_fdopen = os.fdopen

        def fdopen(fd, mode, *args, **kwargs):
            file = real_fdopen(fd, mode, *args, **kwargs)
            if mode != 'wb':
                return file
            class FailingWriter:
                def __enter__(self): return self
                def __exit__(self, *exc): file.close()
                def write(self, data):
                    file.write(data[:4])
                    file.flush()
                    raise OSError('simulated write failure')
            return FailingWriter()

        with mock.patch.object(self.mirror.opener, 'open', return_value=response), \
             mock.patch.object(self.mirror.os, 'fdopen', side_effect=fdopen):
            _, _, error = self.mirror.fetch(self.mirror.BASE + 'page.html')
        self.assertIn('simulated write failure', error)
        self.assertFalse((self.root / 'out/page.html').exists())
        with mock.patch.object(self.mirror.opener, 'open', return_value=response):
            _, _, error = self.mirror.fetch(self.mirror.BASE + 'page.html')
        self.assertIsNone(error)
        self.assertEqual((self.root / 'out/page.html').read_bytes(), b'complete contents')

    def test_concurrent_alias_never_reads_partial_publication(self):
        data = b'<a href="/complete.html">complete</a>'
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = data
        started, resume = threading.Event(), threading.Event()
        real_fdopen = os.fdopen
        first_thread = None

        def fdopen(fd, mode, *args, **kwargs):
            file = real_fdopen(fd, mode, *args, **kwargs)
            if mode != 'wb' or threading.current_thread() is not first_thread:
                return file
            class PausedWriter:
                def __enter__(self): return self
                def __exit__(self, *exc): file.close()
                def write(self, content):
                    file.write(content[:4])
                    file.flush()
                    started.set()
                    if not resume.wait(5):
                        raise TimeoutError('writer not resumed')
                    file.write(content[4:])
            return PausedWriter()

        results = []
        first_thread = threading.Thread(target=lambda: results.append(
            self.mirror.fetch(self.mirror.BASE + 'page.html')))
        with mock.patch.object(self.mirror.opener, 'open', return_value=response), \
             mock.patch.object(self.mirror.os, 'fdopen', side_effect=fdopen):
            first_thread.start()
            try:
                self.assertTrue(started.wait(5), 'writer did not start')
                alias = self.mirror.fetch(self.mirror.BASE + '%70age.html')
            finally:
                resume.set()
                first_thread.join(5)
        self.assertFalse(first_thread.is_alive())
        self.assertIsNone(alias[2])
        self.assertIn(self.mirror.BASE + 'complete.html', alias[1])
        self.assertIsNone(results[0][2])
        self.assertEqual((self.root / 'out/page.html').read_bytes(), data)

    def test_same_origin_redirect_is_followed(self):
        visited = []
        def fake_https(handler, request):
            visited.append(request.full_url)
            headers = email.message.Message()
            if len(visited) == 1:
                headers['Location'] = '/final.html'
                response = urllib.response.addinfourl(io.BytesIO(b''), headers, request.full_url, 302)
                response.msg = 'Found'
            else:
                response = urllib.response.addinfourl(io.BytesIO(b'final'), headers, request.full_url, 200)
                response.msg = 'OK'
            return response
        with mock.patch.object(urllib.request.HTTPSHandler, 'https_open', fake_https):
            self.mirror.opener = urllib.request.build_opener(self.mirror.SameHostRedirect())
            _, _, error = self.mirror.fetch(self.mirror.BASE)
        self.assertIsNone(error)
        self.assertEqual(visited, [self.mirror.BASE, self.mirror.BASE + 'final.html'])
        self.assertEqual((self.root / 'out/index.html').read_bytes(), b'final')
