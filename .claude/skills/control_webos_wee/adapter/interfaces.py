from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple


class BaseTVAdapter(ABC):
    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def get_unavailable_reason(self) -> str: ...

    @abstractmethod
    def power_on(self) -> bool: ...

    @abstractmethod
    def power_off(self) -> bool: ...

    @abstractmethod
    def wait_boot_complete(self, timeout: int = 180) -> bool: ...

    @abstractmethod
    def set_volume(self, level: int) -> bool: ...

    @abstractmethod
    def switch_channel(
        self, channel_name: str, test_case: Optional[Dict[str, Any]] = None
    ) -> bool: ...

    @abstractmethod
    def read_gain_value(self, channel: str, signal_type: str) -> int: ...

    @abstractmethod
    def modify_gain_value(self, channel: str, signal_type: str, new_gain_raw: int) -> bool: ...

    @abstractmethod
    def calculate_gain_offset(
        self, current_power: float, target_power: float, current_gain_raw: int
    ) -> Tuple[int, float]: ...

    @abstractmethod
    def read_drc_value(self) -> int: ...

    @abstractmethod
    def modify_drc_value(self, new_drc_raw: int) -> bool: ...

    @abstractmethod
    def calculate_drc_offset(
        self, current_power: float, target_power: float, current_drc_raw: int
    ) -> Tuple[int, float]: ...

    @abstractmethod
    def compile_and_build(self) -> bool: ...

    @abstractmethod
    def download_upgrade_package(self, local_path: str) -> bool: ...

    @abstractmethod
    def trigger_upgrade(self) -> bool: ...

    def compile_and_upgrade(self, usb_path: str = "") -> bool:
        if not self.compile_and_build():
            return False
        if usb_path and not self.download_upgrade_package(usb_path):
            return False
        return self.trigger_upgrade()
