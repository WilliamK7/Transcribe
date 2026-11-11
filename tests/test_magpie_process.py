import io
import os
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from subtitle_tool.common import Cancelled, ToolError, atomic_json
from subtitle_tool.config import Settings
from subtitle_tool.magpie_process import MagpieProcess, _probe
from subtitle_tool.main import main
from subtitle_tool.mt_control import MTControl
from tests.fixtures import TemporaryTest
from tests.mt_fixtures import PoolPipelineFixture


@unittest.skipUnless(os.name == 'nt', 'Windows Magpie process management required')
class MagpieProcessTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.data = self.path / 'Magpie Portable' / 'data'
        atomic_json(self.data / 'settings.json', {'port': 3498})
        (self.data.parent / '.portable').touch()
        self.executable = self.data.parent / 'magpie-windows-amd64.exe'
        self.executable.touch()
        self.url = 'http://127.0.0.1:3498/v1'
        self.magpie = MagpieProcess(Settings(magpie_data_dir=str(self.data)))
        self.addCleanup(self.magpie.close)
        self.control = MTControl(threading.Event())
        self.probe = self.enterContext(mock.patch('subtitle_tool.magpie_process._probe'))
        self.popen = self.enterContext(mock.patch('subtitle_tool.magpie_process.subprocess.Popen'))
        self.enterContext(mock.patch('sys.stderr', new_callable=io.StringIO))
        self.child = self.popen.return_value
        self.child.pid = 901
        self.child.poll.return_value = None

    def test_workers_share_one_process_until_cli_cleanup(self):
        self.probe.side_effect = [(False, None), (False, None), (True, self.child.pid)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(self.magpie.ensure_ready, self.url, self.control) for _ in range(2)]
            for call in calls:
                call.result(timeout=5)
        self.popen.assert_called_once()
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0], [str(self.executable), 'serve'])
        self.assertEqual(kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)
        self.assertEqual(kwargs['env']['MAGPIE_ADDR'], '127.0.0.1:3498')
        self.child.terminate.assert_not_called()
        self.magpie.close()
        self.magpie.close()
        self.child.terminate.assert_called_once()
        self.child.wait.assert_called_once()

    def test_existing_gateway_is_reused(self):
        self.probe.return_value = True, 902
        self.magpie.ensure_ready(self.url, self.control)
        self.magpie.close()
        self.popen.assert_not_called()

    def test_gateway_that_wins_startup_race_is_reused(self):
        self.probe.side_effect = [(False, None), (True, 902)]
        self.magpie.ensure_ready(self.url, self.control)
        self.magpie.ensure_ready(self.url, self.control)
        self.popen.assert_called_once()
        self.child.terminate.assert_called_once()
        self.assertIsNone(self.magpie.process)

    def test_port_occupied_by_another_service_prevents_launch(self):
        self.probe.return_value = True, None
        with self.assertRaises(ToolError) as error:
            self.magpie.ensure_ready(self.url, self.control)
        self.assertEqual(error.exception.code, 'magpie_start')
        self.popen.assert_not_called()

    def test_remote_and_unbound_endpoints_skip_local_probe(self):
        for url in ('https://gateway.example/v1', 'http://127.0.0.1:3425/v1'):
            self.magpie.ensure_ready(url, self.control)
        self.probe.assert_not_called()
        self.popen.assert_not_called()

    def test_startup_timeout_closes_child(self):
        self.probe.return_value = False, None
        with mock.patch('subtitle_tool.magpie_process.STARTUP_TIMEOUT', 0), self.assertRaises(ToolError) as error:
            self.magpie.ensure_ready(self.url, self.control)
        self.assertEqual(error.exception.code, 'magpie_start')
        self.child.terminate.assert_called_once()

    def test_cancellation_during_startup_closes_child(self):
        self.probe.return_value = False, None

        def launch(*args, **kwargs):
            self.control.cancel.set()
            return self.child

        self.popen.side_effect = launch
        with self.assertRaises(Cancelled):
            self.magpie.ensure_ready(self.url, self.control)
        self.child.terminate.assert_called_once()

    def test_windows_connection_timeout_differs_from_connected_read_timeout(self):
        with mock.patch('subtitle_tool.magpie_process.http.client.HTTPConnection') as connect:
            connection = connect.return_value
            connection.connect.side_effect = TimeoutError()
            self.assertEqual(_probe(urlsplit(self.url)), (False, None))
            connection.connect.side_effect = None
            connection.getresponse.side_effect = TimeoutError()
            self.assertEqual(_probe(urlsplit(self.url)), (True, None))


@unittest.skipUnless(os.name == 'nt', 'Windows Magpie process management required')
class MagpieCliTests(PoolPipelineFixture):
    def setUp(self):
        super().setUp()
        registry = Path(self.config.settings.magpie_data_dir) / 'providers.json'
        data = self.path / 'Magpie Portable' / 'data'
        data.mkdir(parents=True)
        (data / 'providers.json').write_bytes(registry.read_bytes())
        (data.parent / '.portable').touch()
        (data.parent / 'magpie.exe').touch()
        self.config.settings.magpie_data_dir = str(data)
        self.enterContext(mock.patch('subtitle_tool.main.Config', return_value=self.config))
        self.enterContext(mock.patch('sys.stdout', new_callable=io.StringIO))
        self.enterContext(mock.patch('sys.stderr', new_callable=io.StringIO))
        self.probe = self.enterContext(mock.patch('subtitle_tool.magpie_process._probe',
            side_effect=[(False, None), (True, 901)]))
        self.popen = self.enterContext(mock.patch('subtitle_tool.magpie_process.subprocess.Popen'))
        self.popen.return_value.pid = 901
        self.popen.return_value.poll.return_value = None

    def test_resume_uses_saved_gateway_and_closes_it_after_translation(self):
        self.install()
        self.stage_hook = lambda *_: self.popen.return_value.terminate.assert_not_called()
        argv = ['--resume', self.job_id, '--non-interactive']
        with mock.patch.dict(os.environ, MT_BASE_URL='https://other.example/v1'):
            self.assertEqual(self.run_pool(run=lambda: main(argv)), 0)
            self.assertEqual(main(argv), 0)  # Completed resume only uses local data.
        self.assertEqual({url for _, url, _ in self.calls}, {'http://127.0.0.1:3425/v1/chat/completions'})
        self.popen.assert_called_once()
        self.popen.return_value.terminate.assert_called_once()

    def test_check_mt_closes_child_on_interrupt(self):
        with mock.patch('subtitle_tool.mt_transport.ProcessTransport') as transport:
            transport.return_value.request.side_effect = KeyboardInterrupt()
            self.assertEqual(main(['--check-mt']), 130)
        self.popen.assert_called_once()
        self.popen.return_value.terminate.assert_called_once()
