"""Own the optional local Magpie process for one CLI invocation."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .common import ToolError
from .magpie import bound_gateway


STARTUP_TIMEOUT = 20.0
EXECUTABLES = ('magpie.exe', 'magpie-windows-amd64.exe', 'magpie-cli-windows-amd64.exe')


def _probe(endpoint):
    """Return whether the address is occupied and the verified gateway PID, if any."""
    connection_type = http.client.HTTPSConnection if endpoint.scheme == 'https' else http.client.HTTPConnection
    connection = connection_type(endpoint.hostname, endpoint.port, timeout=0.75)
    try:
        try:
            connection.connect()
        except (ConnectionRefusedError, TimeoutError):
            # On Windows, connecting to an unused loopback port may time out first.
            return False, None
        connection.request('GET', '/')
        with connection.getresponse() as response:
            if response.status == 200:
                info = json.loads(response.read(4096))
                if isinstance(info, dict) and info.get('name') == 'magpie':
                    pid = info.get('pid')
                    if type(pid) is int and pid > 0:
                        return True, pid
    except (OSError, http.client.HTTPException, ValueError):
        pass
    finally:
        connection.close()
    return True, None


class MagpieProcess:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.RLock()
        self.ready_urls = set()
        self.process = None

    def ensure_ready(self, base_url, control):
        if os.name != 'nt' or not self.settings.magpie_data_dir:
            return
        data = Path(self.settings.magpie_data_dir).absolute()
        install = data.parent
        if data.name.lower() != 'data' or not (install / '.portable').is_file():
            return
        with self.lock:
            control.check_start()
            if self.process is not None and self.process.poll() is not None:
                self.close()
            if base_url in self.ready_urls or not bound_gateway(self.settings, base_url):
                return

            endpoint = urlsplit(base_url)
            occupied, pid = _probe(endpoint)
            control.check_start()
            if pid is None:
                if occupied or self.process is not None:
                    raise ToolError('无法确认本机翻译端口上的 Magpie 网关；请检查监听地址。', 'magpie_start')
                self._start(install, endpoint, control)
            self.ready_urls.add(base_url)

    def _start(self, install, endpoint, control):
        executable = next((install / name for name in EXECUTABLES if (install / name).is_file()), None)
        if executable is None:
            raise ToolError('找不到 Magpie 程序；请检查 magpie_data_dir 指向的便携安装。', 'magpie_start')

        print('正在启动本机 Magpie 网关…', file=sys.stderr, flush=True)
        try:
            self.process = subprocess.Popen([str(executable), 'serve'], cwd=install,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW, env={**os.environ, 'MAGPIE_ADDR': endpoint.netloc})
        except OSError:
            raise ToolError('无法启动本机 Magpie 网关；请检查便携安装。', 'magpie_start') from None

        deadline = time.monotonic() + STARTUP_TIMEOUT
        try:
            while time.monotonic() < deadline:
                control.check_start()
                _, pid = _probe(endpoint)
                control.check_start()
                if pid is not None:
                    # Another instance may have opened the port after the first probe.
                    if pid != self.process.pid:
                        self.close()
                    return
                if self.process.poll() is not None:
                    raise ToolError('本机 Magpie 在网关就绪前退出；请检查便携安装及网关设置。', 'magpie_start')
                control.cancel.wait(0.2)
            raise ToolError('等待本机 Magpie 网关就绪超时；请检查网关设置。', 'magpie_start')
        except BaseException:
            self.close()
            raise

    def close(self):
        with self.lock:
            if self.process is not None:
                try:
                    if self.process.poll() is None:
                        self.process.terminate()
                        self.process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    raise ToolError('无法确认本次启动的 Magpie 已退出。', 'magpie_stop') from None
                self.process = None
            self.ready_urls.clear()
