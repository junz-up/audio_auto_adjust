from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

from .interfaces import BaseTVAdapter


class StubTVAdapter(BaseTVAdapter):
    def __init__(self, os_type: str = "webOS", available: bool = False, reason: str = ""):
        self.os_type = os_type
        self._available = available
        self._reason = reason or f"{os_type} TV 适配器未接入真实设备"
        self._gain_values: Dict[Tuple[str, str], int] = {}
        self._drc_value = 0

    def is_available(self) -> bool:
        return self._available

    def get_unavailable_reason(self) -> str:
        return self._reason

    def power_on(self) -> bool:
        return self._available

    def power_off(self) -> bool:
        return self._available

    def wait_boot_complete(self, timeout: int = 180) -> bool:
        return self._available

    def set_volume(self, level: int) -> bool:
        return self._available

    def switch_channel(
        self, channel_name: str, test_case: Optional[Dict[str, Any]] = None
    ) -> bool:
        return self._available

    def read_gain_value(self, channel: str, signal_type: str) -> int:
        return self._gain_values.get((channel, signal_type), 0)

    def modify_gain_value(self, channel: str, signal_type: str, new_gain_raw: int) -> bool:
        self._gain_values[(channel, signal_type)] = new_gain_raw
        return self._available

    def calculate_gain_offset(
        self, current_power: float, target_power: float, current_gain_raw: int
    ) -> Tuple[int, float]:
        delta_db = _power_delta_db(current_power, target_power)
        return int(round(current_gain_raw + delta_db)), delta_db

    def read_drc_value(self) -> int:
        return self._drc_value

    def modify_drc_value(self, new_drc_raw: int) -> bool:
        self._drc_value = new_drc_raw
        return self._available

    def calculate_drc_offset(
        self, current_power: float, target_power: float, current_drc_raw: int
    ) -> Tuple[int, float]:
        delta_db = _power_delta_db(current_power, target_power)
        return int(round(current_drc_raw + delta_db)), delta_db

    def compile_and_build(self) -> bool:
        return self._available

    def download_upgrade_package(self, local_path: str) -> bool:
        return self._available

    def trigger_upgrade(self) -> bool:
        return self._available


def _power_delta_db(current_power: float, target_power: float) -> float:
    if current_power <= 0 or target_power <= 0:
        raise ValueError("功率必须大于 0")
    return 10 * math.log10(target_power / current_power)
