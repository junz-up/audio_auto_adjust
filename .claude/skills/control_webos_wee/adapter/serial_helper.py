from __future__ import annotations

import time
from typing import List, Tuple


class SerialHelper:
    def __init__(self, config: dict):
        self.port = config["port"]
        self.baud_rate = int(config.get("baud_rate", 115200))
        self.timeout = float(config.get("timeout", 5))
        self._serial = None

    def open(self) -> None:
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("缺少 pyserial，请先安装: pip install pyserial") from exc

        if self._serial and self._serial.is_open:
            return

        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baud_rate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
            write_timeout=self.timeout,
        )

    def close(self) -> None:
        if self._serial and self._serial.is_open:
            self._serial.close()
        self._serial = None

    def set_baud_rate(self, baud_rate: int) -> None:
        was_open = self.is_open()
        if was_open:
            self.close()
        self.baud_rate = int(baud_rate)
        if was_open:
            self.open()

    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def try_exit_debug_mode(self, response: str) -> bool:
        if "teminalmanager-h:help" in response or "v 01 NG" in response:
            self._serial.write(b"x\r\n")
            self._serial.flush()
            time.sleep(0.5)
            return True
        if "/ #" in response or "/bin/sh" in response:
            self._serial.write(b"exit\r\n")
            self._serial.flush()
            time.sleep(1.0)
            self._serial.write(b"x\r\n")
            self._serial.flush()
            time.sleep(0.5)
            return True
        return False

    def send_command(self, cmd: str, wait_time: float = 1.0) -> Tuple[bool, str]:
        self.open()
        self._serial.reset_input_buffer()
        payload = (cmd + "\r\n").encode("ascii", errors="replace")
        self._serial.write(payload)
        self._serial.flush()
        lines = self._collect_response(wait_time)
        response = "\n".join(lines)
        success = "OK" in response.upper() if response else False
        return success, response

    def wait_for_keywords(self, keywords: list[str], timeout: int = 180) -> bool:
        self.open()
        start_time = time.time()
        buf = ""
        while time.time() - start_time < timeout:
            if self._serial.in_waiting > 0:
                data = self._serial.read(self._serial.in_waiting)
                text = data.decode("utf-8", errors="ignore")
                buf += text
                for kw in keywords:
                    if kw in buf:
                        return True
            else:
                time.sleep(0.3)
        return False

    def _collect_response(self, window_s: float) -> List[str]:
        lines: List[str] = []
        end_ts = time.time() + window_s
        while time.time() < end_ts:
            if self._serial.in_waiting > 0:
                raw = self._serial.readline()
                if raw:
                    text = raw.decode("utf-8", errors="ignore").strip()
                    if text:
                        lines.append(text)
            else:
                time.sleep(0.1)
        return lines

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
