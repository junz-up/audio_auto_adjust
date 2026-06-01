"""audio-auto-calibration CLI 入口：通过 subprocess 调用各 control_* skill 完成闭环自动校准。

用法：
    python main.py --channels HDMI AV DTV-DVB ATV-PAL --impedance 6R --power 6 --tolerance 5
"""
from __future__ import annotations

import argparse
import json
import locale
import math
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _log(msg: str):
    sys.stderr.write(f"[校准] {msg}\n")
    sys.stderr.flush()


def _skills_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _global_skills_root() -> Path:
    return Path.home() / ".claude" / "skills"


def _subprocess_env() -> dict:
    """构建 subprocess 环境变量，PYTHONPATH 包含项目级和全局 skills 目录。"""
    import os
    env = os.environ.copy()
    paths = [str(_skills_root()), str(_global_skills_root())]
    existing = env.get("PYTHONPATH", "")
    if existing:
        paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def _subprocess_encoding() -> str:
    return locale.getpreferredencoding(False) or "utf-8"


def _load_config() -> dict:
    cfg_path = Path(__file__).resolve().parent / "config.json"
    return json.loads(cfg_path.read_text(encoding="utf-8"))


ROUTING_TO_SKILL = {
    "DTVPlayer": "control_dtvplayer",
    "TG39": "control_tg39",
    "AP": "control_ap_ad2502",
}


class APProcess:
    """管理 AP AD2502 的 stdin 服务进程。"""

    def __init__(self, impedance: float):
        cmd = [sys.executable, "-m", "control_ap_ad2502.adapter.main", "serve", "--impedance", str(impedance)]
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding=_subprocess_encoding(), errors="replace",
            env=_subprocess_env(),
        )
        init_resp = self._read_response()
        if init_resp.get("status") != "OK":
            raise RuntimeError(f"AP 初始化失败: {init_resp.get('message', '')}")

    def send(self, cmd_obj: dict) -> dict:
        line = json.dumps(cmd_obj, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()
        return self._read_response()

    def generator_on(self, connector: str, freq: float, level: float) -> dict:
        return self.send({"cmd": "generator", "on": True, "connector": connector, "freq": freq, "level": level})

    def generator_off(self) -> dict:
        return self.send({"cmd": "generator", "on": False})

    def measure(self) -> dict:
        return self.send({"cmd": "measure"})

    def set_mode(self, mode: str, impedance: float = None) -> dict:
        cmd_obj = {"cmd": "set_mode", "mode": mode}
        if impedance is not None:
            cmd_obj["impedance"] = impedance
        return self.send(cmd_obj)

    def quit(self):
        try:
            self.send({"cmd": "quit"})
        except Exception:
            pass
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()

    def _read_response(self) -> dict:
        line = self._proc.stdout.readline()
        if not line:
            stderr = self._proc.stderr.read()
            raise RuntimeError(f"AP 进程无输出: {stderr}")
        return json.loads(line.strip())


def _run_subprocess(skill: str, args: List[str]) -> dict:
    module = f"{skill}.adapter.main"
    cmd = [sys.executable, "-m", module] + args
    result = subprocess.run(cmd, capture_output=True, text=True, encoding=_subprocess_encoding(), errors="replace", env=_subprocess_env())
    if result.returncode != 0 and not result.stdout.strip():
        return {"status": "ERROR", "message": result.stderr.strip() or f"退出码 {result.returncode}"}
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return {"status": "ERROR", "message": f"输出非 JSON: {result.stdout[:200]}"}


def _run_tv(args: List[str]) -> dict:
    return _run_subprocess("control_webos_wee", args)


def _play_signal(routing: dict, channel_name: str, test_case: dict, ap: APProcess) -> dict:
    source = routing.get(channel_name)
    if not source:
        return {"status": "ERROR", "message": f"未知通道路由: {channel_name}"}

    if source == "AP":
        connector = "HDMI" if channel_name == "HDMI" else "AV"
        freq = float(test_case.get("frequency", 1000))
        if channel_name == "HDMI":
            level = float(test_case.get("db_level", -12))
        else:
            level = _parse_voltage(str(test_case.get("voltage", "500mV")))
        return ap.generator_on(connector, freq, level)

    if source == "TG39":
        args = ["play", channel_name]
        if "rf_freq" in test_case:
            args += ["--freq", str(test_case["rf_freq"])]
        if "modulation" in test_case:
            args += ["--modulation", str(test_case["modulation"])]
        return _run_subprocess("control_tg39", args)

    if source == "DTVPlayer":
        args = ["play", channel_name]
        if "rf_freq" in test_case:
            args += ["--freq", str(test_case["rf_freq"])]
        if "db_level" in test_case:
            args += ["--db-level", str(test_case["db_level"])]
        for key in ("bandwidth", "modulation", "guard_interval", "fft_size", "code_rate"):
            if key in test_case:
                args += [f"--{key.replace('_', '-')}", str(test_case[key])]
        return _run_subprocess("control_dtvplayer", args)

    if source == "USB":
        return {"status": "OK"}

    return {"status": "ERROR", "message": f"不支持的信号源: {source}"}


def _parse_voltage(text: str) -> float:
    s = text.strip().upper().replace(" ", "")
    if s.endswith("MV"):
        return float(s[:-2]) / 1000.0
    if s.endswith("V"):
        return float(s[:-1])
    return float(s)


def _parse_impedance(value: str) -> float:
    text = value.strip().upper()
    if text.endswith("R"):
        text = text[:-1]
    return float(text)


MIN_VALID_POWER_W = 0.5
MIN_VALID_VRMS_V = 0.01
MAX_DELTA_DB = 6.0


def _power_delta_db(current: float, target: float) -> float:
    if current < MIN_VALID_POWER_W:
        raise ValueError(f"测量功率 {current:.3f}W 低于最小阈值 {MIN_VALID_POWER_W}W，疑似信号异常")
    if target <= 0:
        raise ValueError("目标功率必须 > 0")
    delta = 10 * math.log10(target / current)
    if delta > MAX_DELTA_DB:
        delta = MAX_DELTA_DB
    elif delta < -MAX_DELTA_DB:
        delta = -MAX_DELTA_DB
    return delta


def _voltage_delta_db(current_v: float, target_v: float) -> float:
    if current_v < MIN_VALID_VRMS_V:
        raise ValueError(f"测量电压 {current_v*1000:.1f}mV 低于最小阈值，疑似信号异常")
    if target_v <= 0:
        raise ValueError("目标电压必须 > 0")
    delta = 20 * math.log10(target_v / current_v)
    if delta > MAX_DELTA_DB:
        delta = MAX_DELTA_DB
    elif delta < -MAX_DELTA_DB:
        delta = -MAX_DELTA_DB
    return delta


def _gain_to_db(raw: int) -> float:
    ab = (raw >> 8) & 0xFF
    cd = raw & 0xFF
    return (ab - 127) + cd / 16.0


def _db_to_gain(db: float) -> int:
    if db >= 0:
        integer_part = int(db)
        frac_part = db - integer_part
    else:
        integer_part = int(math.floor(db))
        frac_part = db - integer_part
    ab = integer_part + 127
    cd = int(round(frac_part * 16))
    if cd >= 16:
        ab += 1
        cd = 0
    ab = max(0, min(255, ab))
    cd = max(0, min(15, cd))
    return (ab << 8) | cd


def _calc_new_gain(current_raw: int, delta_db: float) -> int:
    current_db = _gain_to_db(current_raw)
    return _db_to_gain(current_db + delta_db)


def _measure_one(tv_args: List[str], routing: dict, channel_name: str, test_case: dict,
                 ap: APProcess, config: dict, switch_wait: float, signal_wait: float,
                 is_hp: bool = False) -> dict:
    """切源 + 播信号 + 测量，返回 summary dict。"""
    resp = _run_tv(tv_args)
    if resp.get("status") != "OK":
        raise RuntimeError(f"切源失败: {resp.get('message', '')}")
    time.sleep(switch_wait)

    resp = _play_signal(routing, channel_name, test_case, ap)
    if resp.get("status") != "OK":
        for retry in range(3):
            time.sleep(10)
            resp = _play_signal(routing, channel_name, test_case, ap)
            if resp.get("status") == "OK":
                break
    if resp.get("status") != "OK":
        raise RuntimeError(f"信号播放失败: {resp.get('message', '')}")
    time.sleep(signal_wait)

    resp = ap.measure()
    if resp.get("status") != "OK":
        raise RuntimeError(f"测量失败: {resp.get('message', '')}")

    if is_hp:
        return _summarize_hp(resp["data"], config)
    return _summarize(resp["data"], config, test_case)


LR_IMBALANCE_LIMIT_W = 0.8


def _summarize(measurement: dict, config: dict, test_case: dict) -> dict:
    powers = [float(v.get("power", 0)) for v in measurement.values()]
    thd_values = [float(v.get("thd_n", 0)) for v in measurement.values()]
    avg_power = sum(powers) / len(powers) if powers else 0.0
    target_power = float(config.get("rated_power", 0)) or float(test_case.get("target_power_w", 0))
    tolerance = float(config.get("power_tolerance_percent", 10)) / 100.0
    target_min = target_power * (1 - tolerance)
    target_max = target_power * (1 + tolerance)
    max_thd = max(thd_values) if thd_values else 0.0
    max_thd_limit = float(test_case.get("thd_n_max_percent", config.get("thd_n_max_percent", 10)))

    # L/R 平衡检查
    lr_imbalance = (max(powers) - min(powers)) if len(powers) >= 2 else 0.0
    lr_balanced = lr_imbalance <= LR_IMBALANCE_LIMIT_W

    # 每个声道都必须在规格范围内
    all_channels_in_range = all(target_min <= p <= target_max for p in powers)
    thd_ok = max_thd <= max_thd_limit

    passed = lr_balanced and all_channels_in_range and thd_ok

    return {
        "avg_power_w": avg_power,
        "target_power_w": target_power,
        "target_min_w": target_min,
        "target_max_w": target_max,
        "max_thd_n_percent": max_thd,
        "thd_limit_percent": max_thd_limit,
        "lr_imbalance_w": round(lr_imbalance, 4),
        "lr_balanced": lr_balanced,
        "passed": passed,
        "channels": {k: {"power": round(float(v.get("power", 0)), 4), "thd_n": round(float(v.get("thd_n", 0)), 4)} for k, v in measurement.items()},
    }


def _summarize_hp(measurement: dict, config: dict) -> dict:
    """耳机模式：AP 返回 Vrms，转为 mV 判定 130-150 范围。"""
    hp_cfg = config.get("headphone", {})
    target_min_mv = float(hp_cfg.get("target_vrms_mv_min", 130))
    target_max_mv = float(hp_cfg.get("target_vrms_mv_max", 150))
    thd_limit = float(hp_cfg.get("thd_n_max_percent", 10))
    target_mv = (target_min_mv + target_max_mv) / 2.0

    powers_v = [float(v.get("power", 0)) for v in measurement.values()]
    powers_mv = [p * 1000.0 for p in powers_v]
    thd_values = [float(v.get("thd_n", 0)) for v in measurement.values()]
    avg_mv = sum(powers_mv) / len(powers_mv) if powers_mv else 0.0
    max_thd = max(thd_values) if thd_values else 0.0

    all_in_range = all(target_min_mv <= mv <= target_max_mv for mv in powers_mv)
    thd_ok = max_thd <= thd_limit
    passed = all_in_range and thd_ok

    return {
        "avg_power_w": avg_mv / 1000.0,
        "avg_vrms_mv": round(avg_mv, 2),
        "target_power_w": target_mv / 1000.0,
        "target_range_mv": [target_min_mv, target_max_mv],
        "max_thd_n_percent": round(max_thd, 4),
        "thd_limit_percent": thd_limit,
        "lr_imbalance_w": 0.0,
        "lr_balanced": True,
        "passed": passed,
        "channels": {k: {"power": round(float(v.get("power", 0)), 6), "vrms_mv": round(float(v.get("power", 0)) * 1000, 2), "thd_n": round(float(v.get("thd_n", 0)), 4)} for k, v in measurement.items()},
    }


def _pick_test_case(channel_cfg: dict, kind: str, preset: str = "default") -> Optional[dict]:
    for tc in channel_cfg.get("test_cases", []):
        if tc.get("kind", "").lower() == kind.lower():
            result = dict(tc)
            presets = channel_cfg.get("presets")
            if presets:
                preset_params = presets.get(preset) or presets.get("default", {})
                for k, v in preset_params.items():
                    if k not in result:
                        result[k] = v
            return result
    return None


def _build_report_measurements(measurements: list) -> list:
    """将 main.py 格式的测量数据转为 reporter.py 期望的格式。"""
    result = []
    for m in measurements:
        channels_data = m.get("summary", {}).get("channels", {})
        case_name = m.get("case", "standard")
        test_case = {"kind": "standard"}
        if "max" in case_name:
            test_case["kind"] = "max"
        result.append({
            "channel": m["channel"],
            "test_case": test_case,
            "measurement": channels_data,
        })
    return result


def _build_and_upgrade(config: dict, steps: List[str]):
    """编译 + 下载 + 升级。"""
    _log("开始编译...")
    resp = _run_tv(["build"])
    if resp.get("status") != "OK":
        raise RuntimeError(f"编译失败: {resp.get('message', '')}")
    steps.append("webOS 已编译"); _log("webOS 已编译")

    usb_path = str(config.get("usb_path", "")).strip()
    if not usb_path:
        steps.append("未配置 usb_path，跳过下载与升级"); _log("未配置 usb_path，跳过下载与升级")
        return

    _log("下载升级包...")
    resp = _run_tv(["download", "--usb-path", usb_path])
    if resp.get("status") != "OK":
        raise RuntimeError(f"下载升级包失败: {resp.get('message', '')}")
    steps.append(f"升级包已下载 ({usb_path})"); _log(f"升级包已下载 ({usb_path})")

    _log("触发升级...")
    resp = _run_tv(["upgrade"])
    if resp.get("status") != "OK":
        raise RuntimeError(f"升级失败: {resp.get('message', '')}")
    steps.append("TV 升级完成"); _log("TV 升级完成")

    # 升级后等待 TV 启动稳定
    upgrade_settle_s = float(config.get("upgrade_settle_wait_s", 60.0))
    _log(f"等待 TV 重启 ({upgrade_settle_s}s)...")
    time.sleep(upgrade_settle_s)

    # 升级后复位音量
    _run_tv(["set-volume", "100"])
    steps.append("升级后已复位音量 100"); _log("升级后已复位音量 100")


def _parse_specs(specs_list: List[str]) -> List[Tuple[str, float, str]]:
    """解析 '4R5W=ID_SOUND_xxx' → ('4R', 5.0, 'ID_SOUND_xxx')"""
    result = []
    for s in specs_list:
        if "=" not in s:
            raise ValueError(f"规格格式错误，需要 '阻抗R功率W=SOUND_TYPE_ID': {s}")
        spec_part, sound_type = s.split("=", 1)
        m = re.match(r"(\d+R)(\d+)W?", spec_part, re.IGNORECASE)
        if not m:
            raise ValueError(f"无法解析规格: {spec_part}")
        result.append((m.group(1).upper(), float(m.group(2)), sound_type))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="音频功率自动校准（闭环 Gain/DRC 调试）")
    parser.add_argument("--channels", nargs="*", help="要测的通道列表")
    parser.add_argument("--impedance", type=str, help="阻抗 (如 6R)")
    parser.add_argument("--power", type=float, help="额定功率 (W)")
    parser.add_argument("--tolerance", type=float, help="功率容差百分比")
    parser.add_argument("--preset", type=str, default="default", help="DTV 通道预设名 (如 default, 509-6m)")
    parser.add_argument("--specs", nargs="*", help="多规格批量校准，格式: 4R5W=SOUND_TYPE_ID 4R3W=SOUND_TYPE_ID")
    parser.add_argument("--output", type=str, default="spk", choices=["spk", "hp"], help="音频输出: spk=喇叭, hp=耳机")
    args = parser.parse_args()

    config = _load_config()
    if args.tolerance:
        config["power_tolerance_percent"] = args.tolerance

    # 构建规格列表
    if args.specs:
        specs = _parse_specs(args.specs)
    else:
        if args.impedance:
            config["impedance"] = args.impedance
        if args.power:
            config["rated_power"] = args.power
        specs = [(str(config.get("impedance", "6R")), float(config.get("rated_power", 6.0)), None)]

    requested_channels = set(args.channels) if args.channels else None
    channels_cfg = config.get("channels", {})
    enabled_channels = []
    for name, cfg in channels_cfg.items():
        if not cfg.get("enabled", False):
            continue
        if requested_channels and name not in requested_channels:
            continue
        enabled_channels.append((name, cfg))

    if not enabled_channels:
        print(json.dumps({"status": "ERROR", "message": "未找到启用的测试通道"}, ensure_ascii=False, indent=2))
        return 1

    # 多规格批量模式
    if len(specs) > 1:
        return _run_multi_spec(specs, config, enabled_channels, args)

    # 单规格模式（向后兼容）
    impedance_str, rated_power, sound_type_id = specs[0]
    config["impedance"] = impedance_str
    config["rated_power"] = rated_power
    impedance_ohm = _parse_impedance(impedance_str)

    try:
        ap = APProcess(impedance_ohm)
    except Exception as e:
        print(json.dumps({"status": "ERROR", "message": f"AP 启动失败: {e}"}, ensure_ascii=False, indent=2))
        return 1

    try:
        result = _run_single_calibration(config, ap, enabled_channels, args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("all_passed") else 1
    finally:
        ap.generator_off()
        ap.quit()


def _run_multi_spec(specs: List[Tuple[str, float, str]], config: dict,
                    enabled_channels: list, args) -> int:
    """多规格批量校准：每个规格切 SOUND_TYPE → 编译升级 → 校准。"""
    all_spec_results = []
    overall_passed = True
    current_ap: Optional[APProcess] = None
    current_impedance_ohm: Optional[float] = None

    try:
        for idx, (impedance_str, rated_power, sound_type_id) in enumerate(specs):
            spec_label = f"{impedance_str}{int(rated_power)}W"
            _log(f"===== 开始规格 [{idx+1}/{len(specs)}]: {spec_label} =====")

            # 切换 SOUND_TYPE + 编译升级
            if sound_type_id:
                _log(f"切换 SOUND_TYPE: {sound_type_id}")
                resp = _run_tv(["set-sound-type", sound_type_id])
                if resp.get("status") != "OK":
                    raise RuntimeError(f"设置 SOUND_TYPE 失败: {resp.get('message', '')}")

                _build_and_upgrade(config, [])

            # AP 阻抗切换
            impedance_ohm = _parse_impedance(impedance_str)
            if current_ap is None or current_impedance_ohm != impedance_ohm:
                if current_ap:
                    current_ap.generator_off()
                    current_ap.quit()
                current_ap = APProcess(impedance_ohm)
                current_impedance_ohm = impedance_ohm

            # 更新 config 中的规格参数
            spec_config = dict(config)
            spec_config["impedance"] = impedance_str
            spec_config["rated_power"] = rated_power

            result = _run_single_calibration(spec_config, current_ap, enabled_channels, args)
            result["spec"] = spec_label
            result["sound_type"] = sound_type_id
            all_spec_results.append(result)

            if not result.get("all_passed"):
                overall_passed = False

        final = {
            "status": "SUCCESS" if overall_passed else "FAILED",
            "message": "所有规格校准完成，全部通过" if overall_passed else "部分规格未达标",
            "all_passed": overall_passed,
            "specs": all_spec_results,
        }
        print(json.dumps(final, ensure_ascii=False, indent=2))
        return 0 if overall_passed else 1

    except Exception as e:
        final = {"status": "ERROR", "message": str(e), "specs": all_spec_results}
        print(json.dumps(final, ensure_ascii=False, indent=2))
        return 1
    finally:
        if current_ap:
            current_ap.generator_off()
            current_ap.quit()


def _run_single_calibration(config: dict, ap: APProcess, enabled_channels: list, args) -> dict:
    """执行单个规格的完整校准流程，返回结果 dict。"""
    routing = config.get("routing", {})
    switch_wait = float(config.get("switch_channel_wait_s", 5.0))
    signal_wait = float(config.get("signal_play_wait_s", 10.0))
    max_iterations = int(config.get("max_iterations", 5))
    max_drc_rounds = int(config.get("max_input_drc_rounds", 3))
    max_recal_rounds = int(config.get("max_input_recal_rounds", 3))
    headroom_ratio = float(config.get("drc_precheck_headroom_ratio", 1.2))
    rated_power = float(config.get("rated_power", 6.0))
    is_hp = getattr(args, "output", "spk") == "hp"

    steps: List[str] = []

    def _step(msg: str):
        steps.append(msg)
        _log(msg)
        _log(msg)

    try:
        # 设置 TV 音量
        _log("设置 TV 音量 100")
        resp = _run_tv(["set-volume", "100"])
        if resp.get("status") != "OK":
            raise RuntimeError(f"设置音量失败: {resp.get('message', '')}")
        _step("TV 音量已设为 100")

        # 耳机模式：切换 AP 到电压模式 + TV 输出到耳机
        if is_hp:
            ap.set_mode("voltage")
            resp = _run_tv(["set-sound-output", "hp"])
            if resp.get("status") != "OK":
                raise RuntimeError(f"切换耳机输出失败: {resp.get('message', '')}")
            _step("已切换到耳机输出 + AP 电压模式")

        # 备份配置
        resp = _run_tv(["backup-config"])
        if resp.get("status") != "OK":
            raise RuntimeError(f"备份配置失败: {resp.get('message', '')}")
        backup_path = resp.get("data", {}).get("backup_path", "")
        _step(f"配置已备份: {backup_path}")

        # === DRC 预校准 ===（耳机模式跳过）
        if not is_hp and max_drc_rounds > 0 and rated_power > 0:
            headroom_target = rated_power * headroom_ratio
            # 选 DRC 预检通道
            precheck_ch_name = config.get("drc_precheck_channel", "HDMI")
            precheck_cfg = None
            for name, cfg in enabled_channels:
                if name == precheck_ch_name:
                    precheck_cfg = cfg
                    break
            if not precheck_cfg:
                precheck_ch_name, precheck_cfg = enabled_channels[0]

            max_case = _pick_test_case(precheck_cfg, "max", args.preset)
            if max_case:
                for drc_round in range(1, max_drc_rounds + 1):
                    tv_args = ["switch-channel", precheck_ch_name, "--case", json.dumps(max_case, ensure_ascii=False)]
                    summary = _measure_one(tv_args, routing, precheck_ch_name, max_case, ap, config, switch_wait, signal_wait)
                    avg_w = summary["avg_power_w"]
                    _step(f"DRC预检 R{drc_round}: {precheck_ch_name}/max avg={avg_w:.3f}W (需>={headroom_target:.3f}W)")

                    if avg_w >= headroom_target:
                        _step("DRC 余量满足")
                        break

                    # 读 DRC → 通过 adapter 计算新值 → 写入 → 编译升级
                    resp = _run_tv(["calc-drc", "--current-power", str(avg_w), "--target-power", str(headroom_target)])
                    if resp.get("status") != "OK":
                        raise RuntimeError(f"计算 DRC 失败: {resp.get('message', '')}")
                    calc_data = resp["data"]
                    current_drc_hex = calc_data["current_drc"]
                    new_drc = calc_data["new_drc_raw"]
                    new_drc_hex = calc_data["new_drc"]
                    delta_db = calc_data["delta_db"]

                    if new_drc_hex == current_drc_hex:
                        raise RuntimeError("DRC 已达边界但功率仍不足")

                    resp = _run_tv(["write-drc", "--value", new_drc_hex])
                    if resp.get("status") != "OK":
                        raise RuntimeError(f"写 DRC 失败: {resp.get('message', '')}")
                    _step(f"DRC: {current_drc_hex} -> {new_drc_hex} (delta={delta_db:+.2f}dB)")

                    _build_and_upgrade(config, steps)
                else:
                    _step("警告: DRC 预校准达到最大轮次")

        # === Gain 闭环 ===
        skipped_channels = set()
        for iteration in range(1, max_iterations + 1):
            pending_updates = []
            for channel_name, channel_cfg in enabled_channels:
                if channel_name in skipped_channels:
                    continue
                std_case = _pick_test_case(channel_cfg, "standard", args.preset)
                if not std_case:
                    continue
                tv_args = ["switch-channel", channel_name, "--case", json.dumps(std_case, ensure_ascii=False)]
                try:
                    summary = _measure_one(tv_args, routing, channel_name, std_case, ap, config, switch_wait, signal_wait, is_hp=is_hp)
                except RuntimeError as e:
                    _step(f"警告: {channel_name} 测量异常，跳过 ({e})")
                    skipped_channels.add(channel_name)
                    continue

                if not summary.get("lr_balanced", True):
                    raise RuntimeError(
                        f"左右声道功率差异过大: {channel_name} "
                        f"L={summary['channels'].get('L', {}).get('power', 0):.3f}W "
                        f"R={summary['channels'].get('R', {}).get('power', 0):.3f}W "
                        f"差值={summary['lr_imbalance_w']:.3f}W (限值{LR_IMBALANCE_LIMIT_W}W)，请检查AP阻抗设置"
                    )

                verdict = "PASS" if summary["passed"] else "ADJUST"
                if is_hp:
                    _step(f"Gain R{iteration}: {channel_name}/std avg={summary.get('avg_vrms_mv', 0):.1f}mV -> {verdict}")
                else:
                    _step(f"Gain R{iteration}: {channel_name}/std avg={summary['avg_power_w']:.3f}W -> {verdict}")

                if summary["passed"]:
                    continue

                # 读当前 Gain
                gain_args = ["read-gain", "--signal", channel_name]
                if is_hp:
                    gain_args += ["--output", "hp"]
                resp = _run_tv(gain_args)
                if resp.get("status") != "OK":
                    raise RuntimeError(f"读 Gain 失败: {resp.get('message', '')}")
                current_gain = resp["data"]["raw"]
                try:
                    if is_hp:
                        delta_db = _voltage_delta_db(summary["avg_power_w"], summary["target_power_w"])
                    else:
                        delta_db = _power_delta_db(summary["avg_power_w"], summary["target_power_w"])
                except ValueError as e:
                    _step(f"警告: {channel_name} 信号异常，跳过 ({e})")
                    skipped_channels.add(channel_name)
                    continue
                pending_updates.append((channel_name, current_gain, delta_db))

            if not pending_updates:
                _step(f"Gain 收敛 (第 {iteration} 轮)")
                break

            # 批量写 Gain
            for channel_name, current_gain, delta_db in pending_updates:
                new_gain = _calc_new_gain(current_gain, delta_db)
                write_args = ["write-gain", "--signal", channel_name, "--value", f"0x{new_gain:04X}"]
                if is_hp:
                    write_args += ["--output", "hp"]
                resp = _run_tv(write_args)
                if resp.get("status") != "OK":
                    raise RuntimeError(f"写 Gain 失败: {resp.get('message', '')}")
                _step(f"Gain {channel_name}: 0x{current_gain:04X} -> 0x{new_gain:04X} (delta={delta_db:+.2f}dB)")

            _build_and_upgrade(config, steps)

        # === 最终 DRC 回调 ===（耳机模式跳过）
        if not is_hp:
            for recal_round in range(1, max_recal_rounds + 1):
                out_of_range = []
                for channel_name, channel_cfg in enabled_channels:
                    if channel_name in skipped_channels:
                        continue
                    max_case = _pick_test_case(channel_cfg, "max", args.preset)
                    if not max_case:
                        continue
                    tv_args = ["switch-channel", channel_name, "--case", json.dumps(max_case, ensure_ascii=False)]
                    try:
                        summary = _measure_one(tv_args, routing, channel_name, max_case, ap, config, switch_wait, signal_wait)
                    except RuntimeError as e:
                        _step(f"警告: {channel_name}/max 测量异常，跳过 ({e})")
                        skipped_channels.add(channel_name)
                        continue
                    verdict = "PASS" if summary["passed"] else "ADJUST"
                    _step(f"最终DRC R{recal_round}: {channel_name}/max avg={summary['avg_power_w']:.3f}W -> {verdict}")
                    if not summary["passed"]:
                        out_of_range.append((channel_name, summary))

                if not out_of_range:
                    _step("最终 DRC 回调通过")
                    break

                worst_ch, worst_summary = max(out_of_range, key=lambda x: abs(_power_delta_db(x[1]["avg_power_w"], x[1]["target_power_w"])))
                resp = _run_tv(["calc-drc", "--current-power", str(worst_summary["avg_power_w"]), "--target-power", str(worst_summary["target_power_w"])])
                if resp.get("status") != "OK":
                    raise RuntimeError(f"计算 DRC 失败: {resp.get('message', '')}")
                calc_data = resp["data"]
                current_drc_hex = calc_data["current_drc"]
                new_drc = calc_data["new_drc_raw"]
                new_drc_hex = calc_data["new_drc"]

                if new_drc_hex == current_drc_hex:
                    raise RuntimeError(f"DRC 已达边界但 {worst_ch}/max 仍未达标")

                resp = _run_tv(["write-drc", "--value", new_drc_hex])
                if resp.get("status") != "OK":
                    raise RuntimeError(f"写 DRC 失败: {resp.get('message', '')}")
                _step(f"最终DRC: {current_drc_hex} -> {new_drc_hex} (参考 {worst_ch})")
                _build_and_upgrade(config, steps)

        # === 最终验收测量 ===
        measurements = []
        all_passed = True
        for channel_name, channel_cfg in enabled_channels:
            if channel_name in skipped_channels:
                _step(f"验收: {channel_name} 已跳过（信号异常）")
                all_passed = False
                continue
            for test_case in channel_cfg.get("test_cases", []):
                if is_hp and test_case.get("kind") == "max":
                    continue
                case_name = test_case.get("name", "unknown")
                effective_case = dict(test_case)
                presets = channel_cfg.get("presets")
                if presets:
                    preset_params = presets.get(args.preset) or presets.get("default", {})
                    for k, v in preset_params.items():
                        if k not in effective_case:
                            effective_case[k] = v
                tv_args = ["switch-channel", channel_name, "--case", json.dumps(effective_case, ensure_ascii=False)]
                try:
                    summary = _measure_one(tv_args, routing, channel_name, effective_case, ap, config, switch_wait, signal_wait, is_hp=is_hp)
                except RuntimeError as e:
                    _step(f"验收: {channel_name}/{case_name} 测量异常 ({e})")
                    all_passed = False
                    continue
                measurements.append({"channel": channel_name, "case": case_name, "summary": summary})
                verdict = "PASS" if summary["passed"] else "FAIL"
                if is_hp:
                    _step(f"验收: {channel_name}/{case_name} avg={summary.get('avg_vrms_mv', 0):.1f}mV -> {verdict}")
                else:
                    _step(f"验收: {channel_name}/{case_name} avg={summary['avg_power_w']:.3f}W -> {verdict}")
                if not summary["passed"]:
                    all_passed = False

        result = {
            "status": "SUCCESS" if all_passed else "FAILED",
            "message": "自动校准完成，全部通过" if all_passed else "自动校准完成，存在未达标项",
            "all_passed": all_passed,
            "backup_path": backup_path,
            "steps": steps,
            "measurements": measurements,
        }

        # 生成 Excel 报告
        try:
            report_measurements = _build_report_measurements(measurements)
            power_spec = {
                "impedance": config.get("impedance"),
                "rated_power_w": config.get("rated_power"),
            }
            import importlib.util as _ilu
            reporter_path = Path(__file__).resolve().parent / "reporter.py"
            spec = _ilu.spec_from_file_location("_reporter", reporter_path)
            reporter = _ilu.module_from_spec(spec)
            spec.loader.exec_module(reporter)
            report_path = reporter.generate_report(
                report_measurements,
                power_spec=power_spec,
                mode="auto_calibration",
                os_type=config.get("os_type"),
                output_type=getattr(args, "output", "spk"),
            )
            result["report_path"] = report_path
            _step(f"报告已生成: {report_path}")
        except Exception as e:
            _step(f"报告生成失败（不影响测试结果）: {e}")
            result["report_path"] = ""

        if is_hp:
            _run_tv(["set-sound-output", "spk"])

        return result

    except Exception as e:
        if is_hp:
            _run_tv(["set-sound-output", "spk"])
        return {"status": "ERROR", "message": str(e), "steps": steps}


if __name__ == "__main__":
    raise SystemExit(main())
