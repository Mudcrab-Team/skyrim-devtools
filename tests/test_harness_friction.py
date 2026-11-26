import asyncio
import base64
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from skyrim_devtools import livetest, smoke
from skyrim_devtools.client import ref_command
from skyrim_devtools.use import manager as manager_module
from skyrim_devtools.use.manager import GameManager

from test_use import FakeHost, FakeSocket, FakeTunnel
from test_livetest import FakeManager, write

SCRIPT = 'Write-Output "$env:USERPROFILE `"$x`" \'q\' $(Get-Date)"'


def decode(argv):
    return base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")


def load_pc_build():
    path = Path(__file__).resolve().parent.parent / "tools" / "pc-build"
    loader = SourceFileLoader("pc_build", str(path))
    module = module_from_spec(spec_from_loader("pc_build", loader))
    loader.exec_module(module)
    return module


class FakeProcess:
    def __init__(self, code, out=b"{}"):
        self.returncode = code
        self.out = out

    async def communicate(self):
        return self.out, b""

    async def wait(self):
        return self.returncode


class EncodedCommandTests(unittest.TestCase):
    def test_dollar_command_round_trips(self):
        argv = smoke.powershell_argv(SCRIPT)
        self.assertEqual(decode(argv), SCRIPT)
        self.assertNotIn("$", "".join(argv))

    def test_pc_build_round_trips_and_keeps_exit_code(self):
        module = load_pc_build()
        argv = module.powershell_argv(SCRIPT)
        self.assertEqual(decode(argv), SCRIPT)
        script = module.remote_script("-Action lock -Agent $name")
        self.assertIn("exit $LASTEXITCODE", script)
        self.assertEqual(decode(module.powershell_argv(script)), script)

    def test_host_command_sends_the_script_encoded(self):
        seen = []

        async def fake_exec(*argv, **kwargs):
            seen.append(argv)
            return FakeProcess(0)

        host = smoke.WindowsHost({"sshDestination": "pc"})
        with mock.patch("asyncio.create_subprocess_exec", fake_exec):
            asyncio.run(host.command("status", "a" * 32))
        self.assertIn("$env:USERPROFILE", decode(list(seen[0])))
        self.assertNotIn("$", " ".join(seen[0]))

    def test_read_only_host_calls_settle_and_retry(self):
        codes = [255, 255, 0]

        async def fake_exec(*argv, **kwargs):
            return FakeProcess(codes.pop(0))

        host = smoke.WindowsHost({"sshDestination": "pc"})
        with mock.patch("asyncio.create_subprocess_exec", fake_exec), mock.patch.object(smoke, "READ_SETTLE", 0):
            self.assertEqual(asyncio.run(host.command("status", "a" * 32)), {})
        self.assertEqual(codes, [])

    def test_start_is_never_repeated(self):
        calls = []

        async def fake_exec(*argv, **kwargs):
            calls.append(1)
            return FakeProcess(255)

        host = smoke.WindowsHost({"sshDestination": "pc"})
        with mock.patch("asyncio.create_subprocess_exec", fake_exec), mock.patch.object(smoke, "READ_SETTLE", 0):
            with self.assertRaises(RuntimeError):
                asyncio.run(host.command("start", mode="headless"))
        self.assertEqual(len(calls), 1)

    def test_wait_idle_polls_until_released(self):
        states = [{"idle": False}, {"idle": False}, {"idle": True}]
        host = smoke.WindowsHost({"sshDestination": "pc"})

        async def command(action, *args, **kwargs):
            return states.pop(0)

        host.command = command
        with mock.patch("asyncio.sleep", mock.AsyncMock()):
            self.assertTrue(asyncio.run(host.wait_idle())["idle"])


class RefCommandTests(unittest.TestCase):
    def test_ref_prefix(self):
        self.assertEqual(ref_command(0xA1B2C, "setpos x 0"), "000a1b2c.setpos x 0")
        self.assertEqual(ref_command("000A1B2C", "disable"), "000A1B2C.disable")
        with self.assertRaises(ValueError):
            ref_command("player; quit", "x")


class StalledStartTests(unittest.TestCase):
    def test_hung_start_is_restarted_once_after_release(self):
        host = FakeHost()
        idle_calls = []

        async def wait_idle(timeout=40):
            idle_calls.append(1)

        host.wait_idle = wait_idle
        sockets = []

        async def connect(port, token):
            socket = FakeSocket()
            if not sockets:
                socket.menus = set()  # the first launch never shows the main menu
            sockets.append(socket)
            return socket

        manager = GameManager({"menuTimeout": 0.3}, host=host, connect=connect, keepalive=60)
        result = asyncio.run(manager.start())
        self.assertEqual(result["phase"], "running")
        self.assertEqual(len(result["startRetries"]), 1)
        self.assertIn("stalled", result["startRetries"][0])
        self.assertTrue(host.stopped)
        self.assertEqual(len(sockets), 2)
        self.assertGreaterEqual(len(idle_calls), 3)

    def test_second_stall_is_reported(self):
        host = FakeHost()

        async def connect(port, token):
            socket = FakeSocket()
            socket.menus = set()
            return socket

        manager = GameManager({"menuTimeout": 0.2}, host=host, connect=connect, keepalive=60)
        with self.assertRaises(manager_module.StartStalled):
            asyncio.run(manager.start())
        self.assertEqual(manager_module.STALL_RETRIES, 1)


class ScriptCleanupTests(unittest.TestCase):
    def setUp(self):
        livetest.START_SETTLE = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        write(self.dir, "t/crash.py", "raise SystemExit('script crashed')\n")
        write(self.dir, "t/slow.py", "import time\ntime.sleep(30)\n")

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, manager):
        return livetest.Context(manager, self.dir / "art", "r", None)

    def test_crashing_script_releases_the_game(self):
        manager = FakeManager()
        manager.strays = 0

        async def stray():
            manager.strays += 1

        manager.stop_stray = stray
        result = asyncio.run(livetest.run_script(self.ctx(manager), "t/crash.py", self.dir, 20))
        self.assertFalse(result["pass"])
        self.assertEqual(manager.strays, 1)

    def test_timeout_kills_the_script_and_releases_the_game(self):
        manager = FakeManager()
        manager.strays = 0

        async def stray():
            manager.strays += 1

        manager.stop_stray = stray
        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(livetest.run_script(self.ctx(manager), "t/slow.py", self.dir, 0.5))
        self.assertEqual(manager.strays, 1)

    def test_interrupt_releases_the_game(self):
        manager = FakeManager()
        manager.strays = 0

        async def stray():
            manager.strays += 1

        manager.stop_stray = stray

        async def scenario():
            task = asyncio.create_task(livetest.run_script(self.ctx(manager), "t/slow.py", self.dir, 20))
            await asyncio.sleep(0.5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        self.assertEqual(manager.strays, 1)


if __name__ == "__main__":
    unittest.main()
