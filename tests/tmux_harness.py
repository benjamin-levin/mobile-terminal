import asyncio
import fcntl
import os
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
from pathlib import Path
from unittest import mock


class TmuxHarness:
    def __init__(self, test_case, *, prefix="mt-"):
        if not shutil.which("tmux"):
            test_case.skipTest("tmux is required")
        self.test_case = test_case
        self.environment = os.environ.copy()
        self.environment.pop("TMUX", None)
        self.environment.pop("TMUX_PANE", None)
        self.environment.setdefault("TERM", "xterm-256color")
        self.temporary = tempfile.TemporaryDirectory(
            prefix=prefix,
            dir=os.environ.get("TMUX_TMPDIR"),
        )
        test_case.addCleanup(self.temporary.cleanup)
        self.socket_path = str(Path(self.temporary.name) / "socket")
        self.socket_args = ("-S", self.socket_path)
        self.server_patch = mock.patch(
            "server.tmux_client_options",
            return_value=self.socket_args,
        )
        self.server_patch.start()
        test_case.addCleanup(self.server_patch.stop)
        self.closed = False
        test_case.addCleanup(self.close)

    def argv(self, *args):
        return ["tmux", *self.socket_args, *args]

    def run(self, *args, check=True, timeout=5):
        result = subprocess.run(
            self.argv(*args),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self.environment,
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                result.stdout,
                result.stderr,
            )
        return result.stdout.rstrip("\n")

    def start_session(self, session_name, *args):
        return self.run("new-session", "-d", "-s", session_name, *args)

    def register_async_close(self, resource):
        self.test_case.addAsyncCleanup(resource.close)
        return resource

    async def attach_ordinary_client(self, session_name, cols, rows):
        master_fd, slave_fd = os.openpty()
        self.test_case.addCleanup(self._close_fd, master_fd)
        fcntl.ioctl(
            slave_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, cols, 0, 0),
        )
        try:
            process = subprocess.Popen(
                self.argv("attach-session", "-t", session_name),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=self.environment,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)
        self.test_case.addAsyncCleanup(self._stop_process, process)
        await self.wait_for_ordinary_client(session_name, process.pid)
        return process, master_fd

    async def wait_for_ordinary_client(self, session_name, process_pid):
        deadline = asyncio.get_running_loop().time() + 3
        actual = ""
        while asyncio.get_running_loop().time() < deadline:
            actual = self.run(
                "list-clients",
                "-t",
                session_name,
                "-F",
                "#{client_pid}\t#{client_control_mode}",
                check=False,
            )
            for line in actual.splitlines():
                pid, separator, control = line.partition("\t")
                if separator and pid == str(process_pid) and control == "0":
                    return
            await asyncio.sleep(0.03)
        self.test_case.fail(
            f"ordinary tmux client {process_pid} did not attach: {actual!r}"
        )

    def assert_ordinary_client(self, session_name, process_pid):
        clients = self.run(
            "list-clients",
            "-t",
            session_name,
            "-F",
            "#{client_pid}\t#{client_control_mode}",
        )
        self.test_case.assertIn(f"{process_pid}\t0", clients.splitlines())

    async def wait_for(self, callback, expected, *, description):
        deadline = asyncio.get_running_loop().time() + 3
        actual = None
        while asyncio.get_running_loop().time() < deadline:
            actual = callback()
            if actual == expected:
                return
            await asyncio.sleep(0.03)
        self.test_case.fail(f"{description} remained {actual!r}, expected {expected!r}")

    @staticmethod
    def set_pty_size(master_fd, process, cols, rows):
        fcntl.ioctl(
            master_fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, cols, 0, 0),
        )
        os.kill(process.pid, signal.SIGWINCH)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.run("kill-server", check=False)
        Path(self.socket_path).unlink(missing_ok=True)

    @staticmethod
    async def _stop_process(process):
        if process.poll() is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=1)
        except asyncio.TimeoutError:
            process.kill()
            await asyncio.to_thread(process.wait)

    @staticmethod
    def _close_fd(fd):
        try:
            os.close(fd)
        except OSError:
            pass
