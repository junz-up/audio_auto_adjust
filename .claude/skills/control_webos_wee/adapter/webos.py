from __future__ import annotations

import ctypes
import math
import os
import re
import shutil
import time
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .interfaces import BaseTVAdapter
from .serial_helper import SerialHelper
from .ssh_helper import SSHHelper
from .uiat_controller import UIATController


SIGNAL_TYPE_TO_GAIN_COLUMNS = {
    "DTV-DVB": ["DTVDOLBY", "DTVELSE"],
    "DTV-ATSC": ["DTVDOLBY", "DTVELSE"],
    "DTV-ISDB": ["DTVDOLBY", "DTVELSE"],
    "ATV-PAL": ["PAL"],
    "ATV-N": ["NTSC"],
    "AV": ["AV"],
    "HDMI": ["HDMI"],
    "USB": ["EMF"],
}

DRC_THRESHOLD_FIELDS = [
    "LBTHRESHILD",
    "HBTHRESHILD",
    "MBTHRESHILD",
    "SUBTHRESHILD",
    "POSTTHRESHILD",
]

GAIN_ROW_ID = "OUTPUTTVSPK"
GAIN_ROW_ID_HP = "OUTPUTHP"


# ---------- WA15-3814 DRC 浮点编解码 ----------

def _wa15_reg_to_db(reg: int) -> float:
    """WA15-3814: 32-bit 寄存器 → DRC dB 值。"""
    e = (reg >> 24) & 0xFF
    m = reg & 0xFFFFFF
    x = (2.0 ** (e - 0x10)) * (0.5 + m / (2.0 ** 24))
    if x <= 0:
        return 0.0
    return -20.0 * math.log10(x)


def _wa15_db_to_reg(db: float) -> int:
    """WA15-3814: DRC dB 值 → 32-bit 寄存器。"""
    x = 10.0 ** (-db / 20.0)
    if x <= 0:
        return 0x10800000
    e = int(math.floor(math.log2(x))) + 0x11
    m = round((x / (2.0 ** (e - 0x10)) - 0.5) * (2.0 ** 24))
    e = max(0, min(0xFF, e))
    m = max(0, min(0xFFFFFF, m))
    return (e << 24) | m


class WebOSTVAdapter(BaseTVAdapter):
    def __init__(self, config: dict):
        self.config = config
        self.ssh = SSHHelper(config["ssh"])
        self.serial = SerialHelper(config["serial"])
        self.uiat = UIATController(config["uiat"])
        self.code_root = self._normalize_remote_code_root(config["code_root"])
        self._detected_baud_rate: Optional[int] = None
        self._remote_home_cache: Optional[str] = None
        self._audio_config_path: Optional[str] = None
        self._config_content_cache: Optional[str] = None
        self._boot_keyword_seen_during_upgrade: bool = False
        self._boot_keyword_text: str = ""
        self._expected_pak_version: Optional[str] = None

    def is_available(self) -> bool:
        return True

    def get_unavailable_reason(self) -> str:
        return ""

    def cleanup(self) -> None:
        self.serial.close()
        self.uiat.close()
        self.ssh.disconnect()

    def power_on(self) -> bool:
        self.uiat.open()
        return self.uiat.power_on()

    def power_off(self) -> bool:
        self.uiat.open()
        return self.uiat.power_off()

    def wait_boot_complete(self, timeout: int = 180) -> bool:
        if self._boot_keyword_seen_during_upgrade:
            # 升级流程里已经检测到启动关键字，避免重复等待。
            self._boot_keyword_seen_during_upgrade = False
            self._boot_keyword_text = ""
            return True
        self.serial.open()
        keywords = self.config.get("boot_complete_keywords", ["1st boot elapsed"])
        ok = self.serial.wait_for_keywords(keywords, timeout)
        if not ok:
            return False
        post_wait_s = int(self.config.get("post_boot_wait_seconds", 0))
        if post_wait_s > 0:
            time.sleep(post_wait_s)
        return True

    def _send_serial_with_baud_probe(self, cmd: str, wait_time: float = 3.0) -> Tuple[bool, str]:
        if self._detected_baud_rate is not None:
            self.serial.set_baud_rate(self._detected_baud_rate)
            self.serial.open()
            for _ in range(3):
                success, response = self.serial.send_command(cmd, wait_time=wait_time)
                if self.serial.try_exit_debug_mode(response):
                    time.sleep(0.5)
                    success, response = self.serial.send_command(cmd, wait_time=wait_time)
                if success and "OK" in response:
                    return True, response
                time.sleep(0.3)
            self._detected_baud_rate = None

        for baud_rate in (115200, 9600):
            self.serial.set_baud_rate(baud_rate)
            self.serial.open()
            for _ in range(3):
                success, response = self.serial.send_command(cmd, wait_time=wait_time)
                if self.serial.try_exit_debug_mode(response):
                    time.sleep(0.5)
                    success, response = self.serial.send_command(cmd, wait_time=wait_time)
                if success and "OK" in response:
                    self._detected_baud_rate = baud_rate
                    return True, response
                time.sleep(0.3)
            self.serial.close()
        return False, ""

    def set_volume(self, level: int) -> bool:
        if not isinstance(level, int) or not 0 <= level <= 100:
            raise ValueError(f"音量须在 0-100 之间，收到: {level!r}")
        success, _ = self._send_serial_with_baud_probe(f"kf 00 {level:02x}")
        return success

    def set_sound_output(self, output: str) -> bool:
        commands = {"spk": "es 00 60", "hp": "es 00 50"}
        cmd = commands.get(output)
        if not cmd:
            raise ValueError(f"不支持的音频输出: {output}, 可选: spk, hp")
        success, _ = self._send_serial_with_baud_probe(cmd)
        return success

    def switch_channel(
        self, channel_name: str, test_case: Optional[Dict[str, Any]] = None
    ) -> bool:
        if channel_name == "USB":
            return self._switch_usb_channel(test_case)

        channel_commands = {
            "ATV": "xb 00 10",
            "ATV-PAL": "xb 00 10",
            "ATV-N": "xb 00 10",
            "DTV": "xb 00 00",
            "DTV-DVB": "xb 00 00",
            "DTV-ATSC": "xb 00 00",
            "DTV-ISDB": "xb 00 00",
            "HDMI": "xb 00 91",
            "AV": "mc 00 5a",
        }
        cmd = channel_commands.get(channel_name)
        if not cmd:
            return False

        success, _ = self._send_serial_with_baud_probe(cmd, wait_time=3.0)
        return success

    def check_code_exists(self) -> bool:
        code_root = self.config["code_root"]
        exit_code, out, _ = self.ssh.execute_command(f"test -d '{code_root}' && echo OK || echo NG")
        return exit_code == 0 and "OK" in out

    def get_audio_config_file_path(self) -> str:
        if self._audio_config_path:
            return self._audio_config_path

        customer_h = f"{self.code_root}/customers/customer/customer.h"
        if not self._remote_file_exists(customer_h):
            raise FileNotFoundError(customer_h)
        content = self.ssh.read_file(customer_h)
        customer_id = self._parse_define(content, "CUSTOMER_ID")

        customer_id_lower = customer_id.lower()
        customer_id_h = (
            f"{self.code_root}/customers/customer/{customer_id_lower}/{customer_id_lower}.h"
        )
        if not self._remote_file_exists(customer_id_h):
            raise FileNotFoundError(customer_id_h)
        content = self.ssh.read_file(customer_id_h)

        model_id_match = re.search(r"#define\s+MODEL_ID\s+(\w+)", content)
        if not model_id_match:
            raise ValueError("未找到 MODEL_ID 定义")
        model_id = model_id_match.group(1)

        pattern = (
            rf"#(?:if|elif)\s*\(\s*IsModelID\(\s*{re.escape(model_id)}\s*\)\s*\)"
            r"(.*?)"
            r"(?=#elif|#else|#endif)"
        )
        block_match = re.search(pattern, content, re.DOTALL)
        if not block_match:
            raise ValueError(f"未找到 MODEL_ID={model_id} 对应的配置分支")

        block_content = block_match.group(1)
        sound_type_match = re.search(r"#define\s+CVT_DEF_SOUND_TYPE\s+(\w+)", block_content)
        if not sound_type_match:
            raise ValueError("未找到 CVT_DEF_SOUND_TYPE")

        sound_type = sound_type_match.group(1)
        self._audio_config_path = (
            f"{self.code_root}/customers/common/default_config/datac/{sound_type}.c"
        )
        return self._audio_config_path

    def backup_config(self) -> str:
        config_file = self.get_audio_config_file_path()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = f"{config_file}.backup_{timestamp}"
        self.ssh.copy_file(config_file, backup_file)
        return backup_file

    def set_sound_type(self, new_sound_type: str) -> str:
        customer_h = f"{self.code_root}/customers/customer/customer.h"
        content = self.ssh.read_file(customer_h)
        customer_id = self._parse_define(content, "CUSTOMER_ID")

        customer_id_lower = customer_id.lower()
        customer_id_h = (
            f"{self.code_root}/customers/customer/{customer_id_lower}/{customer_id_lower}.h"
        )
        content = self.ssh.read_file(customer_id_h)

        model_id_match = re.search(r"#define\s+MODEL_ID\s+(\w+)", content)
        if not model_id_match:
            raise ValueError("未找到 MODEL_ID 定义")
        model_id = model_id_match.group(1)

        pattern = (
            rf"(#(?:if|elif)\s*\(\s*IsModelID\(\s*{re.escape(model_id)}\s*\)\s*\)"
            r".*?)"
            r"(#define\s+CVT_DEF_SOUND_TYPE\s+)\w+"
        )
        match = re.search(pattern, content, re.DOTALL)
        if not match:
            raise ValueError(f"在 MODEL_ID={model_id} 块中未找到 CVT_DEF_SOUND_TYPE")

        old_text = match.group(0)
        new_text = match.group(1) + match.group(2) + new_sound_type
        new_content = content.replace(old_text, new_text, 1)
        if new_content == content:
            self._audio_config_path = ""
            return customer_id_h
        self.ssh.write_file(customer_id_h, new_content)
        self._audio_config_path = ""
        return customer_id_h

    def restore_config(self, backup_path: str) -> bool:
        config_file = self.get_audio_config_file_path()
        self.ssh.copy_file(backup_path, config_file)
        self._config_content_cache = None
        return True

    def read_gain_value(self, channel: str, signal_type: str, output: str = "spk") -> int:
        columns = SIGNAL_TYPE_TO_GAIN_COLUMNS.get(signal_type)
        if not columns:
            raise ValueError(f"不支持的信号类型: {signal_type}")
        row_id = GAIN_ROW_ID_HP if output == "hp" else GAIN_ROW_ID
        content = self._read_config_content()
        header_cols = self._parse_gain_header(content)
        col_name = columns[0]
        col_index = header_cols.index(col_name)
        row_values = self._parse_gain_row(content, row_id)
        return row_values[col_index]

    def modify_gain_value(self, channel: str, signal_type: str, new_gain_raw: int, output: str = "spk") -> bool:
        columns = SIGNAL_TYPE_TO_GAIN_COLUMNS.get(signal_type)
        if not columns:
            return False
        row_id = GAIN_ROW_ID_HP if output == "hp" else GAIN_ROW_ID
        content = self._read_config_content()
        header_cols = self._parse_gain_header(content)
        row_values = self._parse_gain_row(content, row_id)

        modified = False
        for col_name in columns:
            if col_name not in header_cols:
                continue
            col_index = header_cols.index(col_name)
            if col_index >= len(row_values):
                continue
            old_hex = row_values[col_index]
            content = self._replace_gain_in_row(content, row_id, col_index, old_hex, new_gain_raw)
            row_values[col_index] = new_gain_raw
            modified = True

        if modified:
            self._write_config_content(content)
        return modified

    def calculate_gain_offset(
        self, current_power: float, target_power: float, current_gain_raw: int
    ) -> Tuple[int, float]:
        delta_db = _power_delta_db(current_power, target_power)
        current_db = self._gain_to_db(current_gain_raw)
        new_db = current_db + delta_db
        new_raw = self._db_to_gain(new_db)
        return new_raw, delta_db

    def _detect_amp_type(self) -> str:
        """从 datac 文件中检测功放类型：SY6045 或 WA153814。"""
        content = self._read_config_content()
        match = re.search(r'"AMPTYPE"\s*,\s*MAC_S\((\w+)\)', content)
        if match:
            return match.group(1).upper()
        return "SY6045"

    def read_drc_value(self) -> int:
        content = self._read_config_content()
        amp_type = self._detect_amp_type()
        for field_name in DRC_THRESHOLD_FIELDS:
            match = re.search(rf'"\s*{field_name}\s*"\s*,\s*(0x[0-9a-fA-F]+)', content)
            if match:
                full_value = int(match.group(1), 16)
                if amp_type == "WA153814":
                    return full_value
                return full_value & 0x3FF
        raise ValueError("未找到 DRC Threshold 字段")

    def modify_drc_value(self, new_drc_raw: int) -> bool:
        content = self._read_config_content()
        amp_type = self._detect_amp_type()
        modified = False
        for field_name in DRC_THRESHOLD_FIELDS:
            match = re.search(
                rf'("\s*{field_name}\s*"\s*,\s*)(0x[0-9a-fA-F]+)',
                content,
            )
            if not match:
                continue
            if amp_type == "WA153814":
                new_full_value = new_drc_raw
                content = content.replace(match.group(2), f"0x{new_full_value:08X}", 1)
            else:
                old_full_value = int(match.group(2), 16)
                new_full_value = (old_full_value & ~0x3FF) | (new_drc_raw & 0x3FF)
                content = content.replace(match.group(2), f"0x{new_full_value:06X}", 1)
            modified = True
        if modified:
            self._write_config_content(content)
        return modified

    def calculate_drc_offset(
        self, current_power: float, target_power: float, current_drc_raw: int
    ) -> Tuple[int, float]:
        delta_db = _power_delta_db(current_power, target_power)
        amp_type = self._detect_amp_type()
        if amp_type == "WA153814":
            current_drc_db = _wa15_reg_to_db(current_drc_raw)
            new_drc_db = current_drc_db - 10.0 * math.log10(current_power / target_power)
            new_raw = _wa15_db_to_reg(new_drc_db)
            return new_raw, delta_db
        step_db = float(self.config.get("platform_rules", {}).get("drc_step_db", 0.125))
        step_count = round(delta_db / step_db)
        new_raw = max(0, min(0x3FF, (current_drc_raw & 0x3FF) + step_count))
        return new_raw, delta_db

    def compile_and_build(self) -> bool:
        build_cfg = self.config["ssh"]
        ok, message = self.ssh.execute_docker_build(
            code_path=self.config["code_root"],
            docker_cmd=build_cfg["docker_cmd"],
            build_cmd=build_cfg["build_cmd"],
        )
        if not ok:
            raise RuntimeError(message)
        return True

    def download_upgrade_package(self, local_path: str) -> bool:
        if str(local_path or "").strip().upper() == "AUTO":
            try:
                self.uiat.open()
                self.uiat.switch_usb_to_pc()
            except Exception:
                pass
        local_path = self._resolve_local_upgrade_path(local_path)
        if not local_path:
            return False
        if not self._prepare_local_usb_path(local_path):
            return False

        remote_bin_dir = f"{self.code_root}/bin"
        find_zip_cmd = (
            f"if [ -d '{remote_bin_dir}' ]; then "
            f"ls -1t '{remote_bin_dir}'/CP*_plugins_*.zip 2>/dev/null | head -n 1; "
            f"fi"
        )
        _, out, _ = self.ssh.execute_command(find_zip_cmd)
        latest_zip_remote = out.strip()
        if not latest_zip_remote:
            return False

        os.makedirs(local_path, exist_ok=True)
        for old_dir_name in ("sw_update", "sw_updata"):
            old_dir = os.path.join(local_path, old_dir_name)
            if os.path.isdir(old_dir):
                shutil.rmtree(old_dir)

        zip_dst = os.path.join(local_path, os.path.basename(latest_zip_remote))
        if os.path.exists(zip_dst):
            os.remove(zip_dst)
        self.ssh.download_file(latest_zip_remote, zip_dst)

        # 从 zip 文件名提取 PAK 版本号（如 CP..._plugins_605131515.zip → 605131515）
        zip_basename = os.path.basename(latest_zip_remote)
        ver_match = re.search(r'_plugins_(\d+)\.zip$', zip_basename)
        if ver_match:
            self._expected_pak_version = ver_match.group(1)

        with zipfile.ZipFile(zip_dst, "r") as zf:
            zf.extractall(local_path)

        sw_update_dst = os.path.join(local_path, "sw_update")
        return os.path.isdir(sw_update_dst)

    def trigger_upgrade(self) -> bool:
        self._boot_keyword_seen_during_upgrade = False
        self._boot_keyword_text = ""
        self.uiat.open()
        if not self.uiat.switch_usb_to_tv():
            return False
        usb_ready_wait_s = float(self.config.get("upgrade_usb_ready_wait_seconds", 8.0))
        if usb_ready_wait_s > 0:
            time.sleep(usb_ready_wait_s)

        command_wait_s = float(self.config.get("upgrade_command_wait_seconds", 10))
        detect_timeout_s = int(self.config.get("upgrade_detect_timeout_seconds", 20))
        success_keywords = self._get_upgrade_success_keywords()
        baud_rates = (115200, 9600)
        probe_cmd = str(self.config.get("upgrade_baud_probe_command", "AV 00 00"))
        probe_wait_s = float(self.config.get("upgrade_baud_probe_wait_seconds", 1.2))
        version_extra_wait_s = float(self.config.get("version_extra_wait_seconds", 20.0))

        # 最多进行 3 大轮，每大轮：发升级命令(retry3次) → 查版本号
        for major_round in range(3):
            # 探测波特率
            detected_baud = self._detect_upgrade_baud_rate(baud_rates, probe_cmd, probe_wait_s)
            if detected_baud is None:
                detected_baud = baud_rates[0]
            self.serial.set_baud_rate(detected_baud)
            self.serial.open()

            # 发送 WEE 06 06，最多 retry 3 次
            for attempt in range(3):
                success, response = self.serial.send_command("WEE 06 06", wait_time=command_wait_s)
                if self.serial.try_exit_debug_mode(response):
                    time.sleep(0.5)
                    success, response = self.serial.send_command("WEE 06 06", wait_time=command_wait_s)

                # 命令返回 OK
                if success:
                    self.serial.close()
                    return True

                # 监控 15 秒串口 log
                if self._response_has_keywords(response, success_keywords):
                    self._boot_keyword_seen_during_upgrade = True
                    self._boot_keyword_text = "response_keywords"
                    self.serial.close()
                    return True
                if self._wait_for_upgrade_signal(success_keywords, detect_timeout_s):
                    self._boot_keyword_seen_during_upgrade = True
                    self._boot_keyword_text = "stream_keywords"
                    self.serial.close()
                    return True
                # 15 秒内无任何响应，继续下一次 retry

            self.serial.close()

            # 3 次 retry 都没成功，额外等待后查版本号确认
            time.sleep(version_extra_wait_s)
            if self._query_version_match(baud_rates, probe_cmd, probe_wait_s):
                return True

            # 版本号不对，准备下一大轮（重新切 USB）
            if major_round < 2:
                self.uiat.open()
                self.uiat.switch_usb_to_tv()
                time.sleep(usb_ready_wait_s)

        return False

    def _query_version_match(self, baud_rates: tuple, probe_cmd: str, probe_wait_s: float) -> bool:
        """探测波特率后发送 WEE 00 00 查询版本号，与预期版本比对。"""
        if not self._expected_pak_version:
            return False
        version_wait_s = float(self.config.get("version_query_wait_seconds", 3.0))
        detected_baud = self._detect_upgrade_baud_rate(baud_rates, probe_cmd, probe_wait_s)
        if detected_baud is None:
            detected_baud = baud_rates[0]
        self.serial.set_baud_rate(detected_baud)
        self.serial.open()
        for _ in range(3):
            success, response = self.serial.send_command("WEE 00 00", wait_time=version_wait_s)
            if self.serial.try_exit_debug_mode(response):
                time.sleep(0.5)
                success, response = self.serial.send_command("WEE 00 00", wait_time=version_wait_s)
            if response and self._expected_pak_version in response:
                self._boot_keyword_seen_during_upgrade = True
                self._boot_keyword_text = "version_verified"
                self.serial.close()
                return True
            time.sleep(0.3)
        self.serial.close()
        return False

    def _detect_upgrade_baud_rate(
        self, baud_rates: Tuple[int, ...], probe_cmd: str, probe_wait_s: float
    ) -> Optional[int]:
        for baud_rate in baud_rates:
            self.serial.set_baud_rate(baud_rate)
            self.serial.open()
            try:
                # 每个波特率读取三次，因为用户模式下切换波特率后需要读取两次才能读取成功
                for _ in range(3):
                    success, response = self.serial.send_command(probe_cmd, wait_time=probe_wait_s)
                    if self.serial.try_exit_debug_mode(response):
                        time.sleep(0.3)
                        success, response = self.serial.send_command(probe_cmd, wait_time=probe_wait_s)
                    text = (response or "").upper()
                    # 快速探测仅用于判断串口是否通：命中 OK/NG 任一即可。
                    if success or "OK" in text or "NG" in text:
                        return baud_rate
                    time.sleep(0.3)
            finally:
                self.serial.close()
        return None

    def _switch_usb_channel(self, test_case: Optional[Dict[str, Any]] = None) -> bool:
        self.uiat.open()
        self.uiat.switch_usb_to_tv()
        key_sequence = self._get_usb_key_sequence(test_case)
        key_interval_s = float(self.config.get("usb_key_interval_seconds", 8))
        for baud_rate in (115200, 9600):
            self.serial.set_baud_rate(baud_rate)
            self.serial.open()
            for _ in range(2):
                if self._send_remote_key_sequence(key_sequence, key_interval_s):
                    return True
                time.sleep(0.5)
            self.serial.close()
        return False

    def _get_usb_key_sequence(self, test_case: Optional[Dict[str, Any]] = None) -> list[str]:
        standard_input_seq = [
            "5B", "7C","41","44","44","44","06","44",
        ]
        max_input_seq = [
            "5B", "7C","41","44","44","44","06","06","44",
        ]
        db_level = test_case.get("db_level") if isinstance(test_case, dict) else None
        try:
            db_val = float(db_level) if db_level is not None else -12.0
        except Exception:
            db_val = -12.0
        return max_input_seq if abs(db_val) < 0.5 else standard_input_seq

    def _send_remote_key_sequence(self, key_sequence: list[str], key_interval_s: float) -> bool:
        for idx, key_code in enumerate(key_sequence):
            cmd = f"mc 00 {key_code}"
            success, response = self.serial.send_command(cmd, wait_time=3.0)
            if self.serial.try_exit_debug_mode(response):
                time.sleep(0.5)
                success, response = self.serial.send_command(cmd, wait_time=3.0)
            if not (success and "OK" in response):
                return False
            if idx < len(key_sequence) - 1:
                time.sleep(key_interval_s)
        return True

    def _read_config_content(self, force: bool = False) -> str:
        if self._config_content_cache and not force:
            return self._config_content_cache
        config_file = self.get_audio_config_file_path()
        self._config_content_cache = self.ssh.read_file(config_file)
        return self._config_content_cache

    def _write_config_content(self, content: str) -> None:
        config_file = self.get_audio_config_file_path()
        self.ssh.write_file(config_file, content)
        self._config_content_cache = content

    def _prepare_local_usb_path(self, local_path: str) -> bool:
        try:
            self.uiat.open()
            # UIATController.switch_usb_to_pc 已包含切换后等待 (usb_to_pc_wait_s)
            self.uiat.switch_usb_to_pc()
        except Exception:
            pass

        if os.path.isdir(local_path):
            return True

        try:
            os.makedirs(local_path, exist_ok=True)
            return True
        except Exception:
            return False

    def _resolve_local_upgrade_path(self, local_path: str) -> str:
        text = str(local_path or "").strip()
        if text and text.upper() != "AUTO":
            return os.path.abspath(text)
        detected = self._detect_single_usb_drive()
        return detected or ""

    def _wait_for_upgrade_signal(self, keywords: List[str], timeout: int) -> bool:
        if timeout <= 0 or not keywords:
            return False
        return self.serial.wait_for_keywords(keywords, timeout=timeout)

    def _response_has_keywords(self, response: str, keywords: List[str]) -> bool:
        lowered = (response or "").lower()
        return any(isinstance(kw, str) and kw and kw.lower() in lowered for kw in keywords)

    def _get_upgrade_success_keywords(self) -> List[str]:
        keywords = self.config.get("upgrade_success_keywords")
        if isinstance(keywords, list) and keywords:
            return [str(item) for item in keywords if str(item).strip()]
        return [
            str(item)
            for item in self.config.get("boot_complete_keywords", [])
            if isinstance(item, str) and item.strip() and item.strip().lower() != "teminalmanager-h:help"
        ]

    def _detect_single_usb_drive(self) -> str | None:
        if os.name != "nt":
            return None

        removable_drives = []
        drive_mask = ctypes.windll.kernel32.GetLogicalDrives()
        for index in range(26):
            if not (drive_mask & (1 << index)):
                continue
            drive = f"{chr(ord('A') + index)}:/"
            if not os.path.isdir(drive):
                continue
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(f"{drive[0]}:\\")
            if drive_type == 2:
                removable_drives.append(drive)

        if len(removable_drives) == 1:
            return removable_drives[0]
        return None

    def _parse_define(self, content: str, define_name: str) -> str:
        pattern = rf"#define\s+{define_name}\s+(\w+)"
        match = re.search(pattern, content)
        if not match:
            raise ValueError(f"未找到定义: {define_name}")
        return match.group(1)

    def _parse_gain_header(self, content: str) -> List[str]:
        match = re.search(r'"SNDOUTPUTGAIN".*?\n\s*(.*?_END_)', content, re.DOTALL)
        if not match:
            return []
        header_line = match.group(1)
        return re.findall(r'"(\w+)"', header_line)

    def _parse_gain_row(self, content: str, row_id: str) -> List[int]:
        pattern = rf'"\s*{row_id}\s*"\s*,(.*?)_END_'
        match = re.search(pattern, content, re.DOTALL)
        if not match:
            return []
        data_part = match.group(1)
        hex_values = re.findall(r"0x([0-9a-fA-F]+)", data_part)
        return [int(v, 16) for v in hex_values]

    def _replace_gain_in_row(
        self, content: str, row_id: str, col_index: int, old_hex: int, new_hex: int
    ) -> str:
        pattern = rf'("\s*{row_id}\s*"\s*,)(.*?)(_END_)'
        match = re.search(pattern, content, re.DOTALL)
        if not match:
            return content
        data_part = match.group(2)
        hex_matches = list(re.finditer(r"0x[0-9a-fA-F]+", data_part))
        if col_index >= len(hex_matches):
            return content
        target_match = hex_matches[col_index]
        new_data_part = (
            data_part[: target_match.start()] + f"0x{new_hex:04X}" + data_part[target_match.end() :]
        )
        return content[: match.start(2)] + new_data_part + content[match.end(2) :]

    def _normalize_remote_code_root(self, code_root: str) -> str:
        root = str(code_root).strip()
        if root.startswith("~/"):
            remote_home = self._get_remote_home()
            if remote_home:
                return f"{remote_home.rstrip('/')}/{root[2:]}"
        return root

    def _get_remote_home(self) -> str:
        if hasattr(self, "_remote_home_cache") and self._remote_home_cache:
            return self._remote_home_cache
        code, out, _ = self.ssh.execute_command("echo $HOME")
        if code == 0:
            home = out.strip()
            if home.startswith("/"):
                self._remote_home_cache = home
                return home
        return ""

    def _remote_file_exists(self, remote_path: str) -> bool:
        cmd = f"test -f '{remote_path}' && echo OK || echo NG"
        code, out, _ = self.ssh.execute_command(cmd)
        return code == 0 and "OK" in out

    @staticmethod
    def _gain_to_db(hex_value: int) -> float:
        ab = (hex_value >> 8) & 0xFF
        cd = hex_value & 0xFF
        return (ab - 127) + cd / 16.0

    @staticmethod
    def _db_to_gain(gain_db: float) -> int:
        if gain_db >= 0:
            integer_part = int(gain_db)
            frac_part = gain_db - integer_part
        else:
            integer_part = int(math.floor(gain_db))
            frac_part = gain_db - integer_part

        ab = integer_part + 127
        cd = int(round(frac_part * 16))
        ab = max(0, min(255, ab))
        cd = max(0, min(15, cd))
        return (ab << 8) | cd


def _power_delta_db(current_power: float, target_power: float) -> float:
    if current_power <= 0 or target_power <= 0:
        raise ValueError("功率必须大于 0")
    return 10 * math.log10(target_power / current_power)
