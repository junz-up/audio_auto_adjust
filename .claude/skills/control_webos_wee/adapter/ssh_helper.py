from __future__ import annotations

import os
import re
import time
from typing import Optional, Tuple


ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


class SSHHelper:
    def __init__(self, config: dict):
        self.host = config["host"]
        self.port = int(config.get("port", 22))
        self.user = config["user"]
        self.password = config.get("password", "")
        self.build_timeout = int(config.get("build_timeout", 14400))
        self._client = None

    def connect(self) -> None:
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError("缺少 paramiko，请先安装: pip install paramiko") from exc

        if self._client is not None:
            return

        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            password=self.password,
            timeout=20,
            auth_timeout=20,
            look_for_keys=False,
            allow_agent=False,
        )

    def disconnect(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def execute_command(self, command: str, timeout: int = 30) -> Tuple[int, str, str]:
        self.connect()
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return exit_code, out, err

    def read_file(self, remote_path: str) -> str:
        self.connect()
        sftp = self._client.open_sftp()
        try:
            with sftp.open(remote_path, "r") as fh:
                return fh.read().decode("utf-8", errors="replace")
        finally:
            sftp.close()

    def write_file(self, remote_path: str, content: str) -> None:
        self.connect()
        sftp = self._client.open_sftp()
        try:
            with sftp.open(remote_path, "w") as fh:
                fh.write(content.encode("utf-8"))
        finally:
            sftp.close()

    def copy_file(self, src_path: str, dst_path: str) -> None:
        exit_code, _, err = self.execute_command(f"cp '{src_path}' '{dst_path}'")
        if exit_code != 0:
            raise RuntimeError(f"复制远端文件失败: {err}")

    def download_file(self, remote_path: str, local_path: str) -> None:
        self.connect()
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        sftp = self._client.open_sftp()
        try:
            sftp.get(remote_path, local_path)
        finally:
            sftp.close()

    def execute_docker_build(
        self,
        code_path: str,
        docker_cmd: str = "docker_tizen_build",
        build_cmd: str = "make ocs",
        stream: bool = True,
    ) -> Tuple[bool, str]:
        self.connect()
        channel = self._client.invoke_shell()
        time.sleep(1.0)
        if channel.recv_ready():
            channel.recv(4096)

        channel.send(f"cd {code_path} && pwd\n".encode())
        ok, out = self._read_until(channel, [r"\$\s*$"], timeout_s=30, stream_label="cd" if stream else None)
        if not ok:
            return False, "进入代码目录失败"

        channel.send(f"{docker_cmd}\n".encode())
        ok, out = self._read_until(
            channel,
            [r"command not found", r"builder@[^:]+:"],
            timeout_s=180,
            stream_label="docker" if stream else None,
        )
        if not ok or "command not found" in out:
            return False, f"Docker 启动失败: {docker_cmd}"

        start_ts = time.time()
        channel.send(f"{build_cmd}\n".encode())
        ok, out = self._read_until(
            channel,
            [r"builder@[^:]+:", r"make:\s\*\*\*"],
            timeout_s=self.build_timeout,
            stream_label="build" if stream else None,
        )
        elapsed = int(time.time() - start_ts)

        if not ok:
            return False, f"编译超时 ({elapsed}s)"
        if re.search(r"make:\s\*\*\*", out):
            return False, f"编译失败 ({elapsed}s)"
        return True, f"编译成功 ({elapsed}s)"

    def _read_until(
        self,
        channel,
        patterns: list[str],
        timeout_s: int = 120,
        stream_label: Optional[str] = None,
        stream_interval_s: float = 5.0,
    ) -> Tuple[bool, str]:
        import sys as _sys
        end_time = time.time() + timeout_s
        buf = ""
        printed_clean_len = 0
        last_stream = time.time()
        start_ts = time.time()
        regexes = [re.compile(p, re.MULTILINE) for p in patterns]
        while time.time() < end_time:
            if channel.recv_ready():
                data = channel.recv(4096).decode("utf-8", errors="replace")
                buf += data
                scan_buf = strip_ansi(buf)
                for rx in regexes:
                    if rx.search(scan_buf):
                        if stream_label and len(scan_buf) > printed_clean_len:
                            elapsed = int(time.time() - start_ts)
                            tail = scan_buf[printed_clean_len:].splitlines()[-3:]
                            for ln in tail:
                                print(f"[{stream_label} +{elapsed}s] {ln}", file=_sys.stderr, flush=True)
                        return True, scan_buf
                # 定时把新收到的行流到 stderr（基于去 ANSI 后的长度计算增量）
                if stream_label and (time.time() - last_stream) >= stream_interval_s and len(scan_buf) > printed_clean_len:
                    elapsed = int(time.time() - start_ts)
                    new_lines = scan_buf[printed_clean_len:].splitlines()
                    tail = new_lines[-3:] if len(new_lines) > 3 else new_lines
                    for ln in tail:
                        print(f"[{stream_label} +{elapsed}s] {ln}", file=_sys.stderr, flush=True)
                    printed_clean_len = len(scan_buf)
                    last_stream = time.time()
            else:
                time.sleep(0.3)
        return False, strip_ansi(buf)
