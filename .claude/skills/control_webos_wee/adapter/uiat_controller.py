from __future__ import annotations

import json
import time
from typing import Optional, Tuple

from .serial_helper import SerialHelper


class UIATController:
    """升级流程内部使用的精简 UIAT 客户端；高层 UIAT 操作请使用 control_uiat skill。"""

    def __init__(self, config: dict):
        self._serial = SerialHelper(
            {
                "port": config["port"],
                "baud_rate": config.get("baud_rate", 115200),
                "timeout": config.get("timeout", 2),
            }
        )
        self._last_send_ts = 0.0
        self._resp_window = float(config.get("resp_window", 1.0))
        self._usb_to_pc_wait_s = float(config.get("usb_to_pc_wait_s", 5.0))
        self._usb_to_tv_wait_s = float(config.get("usb_to_tv_wait_s", 10.0))

    def open(self) -> None:
        self._serial.open()

    def close(self) -> None:
        self._serial.close()

    def power_on(self) -> bool:
        current = self._query_status("12V_PW")
        if current and current.upper() == "ON":
            return True
        success, _ = self._send_and_collect("12V_PW ON")
        return success

    def power_off(self) -> bool:
        success, _ = self._send_and_collect("12V_PW OFF")
        return success

    def switch_usb_to_tv(self) -> bool:
        success, _ = self._send_and_collect("USBDISK TV")
        if not success:
            return False
        current = self._query_status("USBDISK")
        if not (current and current.upper() == "TV"):
            return False
        if self._usb_to_tv_wait_s > 0:
            time.sleep(self._usb_to_tv_wait_s)
        return True

    def switch_usb_to_pc(self) -> bool:
        success, _ = self._send_and_collect("USBDISK PC")
        if not success:
            return False
        current = self._query_status("USBDISK")
        if not (current and current.upper() == "PC"):
            return False
        if self._usb_to_pc_wait_s > 0:
            time.sleep(self._usb_to_pc_wait_s)
        return True

    def _send_and_collect(self, cmd_text: str) -> Tuple[bool, str]:
        now = time.time()
        if now - self._last_send_ts < 0.1:
            time.sleep(0.1 - (now - self._last_send_ts))
        success, response = self._serial.send_command(cmd_text, self._resp_window)
        self._last_send_ts = time.time()
        return success, response

    def _query_status(self, device: str) -> Optional[str]:
        success, response = self._send_and_collect(device)
        if not success and not response:
            return None
        for line in response.split("\n"):
            try:
                obj = json.loads(line.strip())
            except Exception:
                continue
            if isinstance(obj, dict) and str(obj.get("ACK", "")).upper() == device.upper():
                return str(obj.get("VAL", ""))
        return None
