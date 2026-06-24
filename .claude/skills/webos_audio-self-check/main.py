"""audio-self-check CLI 入口：通过 subprocess 调用各 control_* skill 完成全通道自检。

用法：
    python main.py --channels HDMI AV DTV-DVB ATV-PAL USB
    python main.py --channels HDMI AV --impedance 6 --power 10
"""
from __future__ import annotations

import argparse
import io
import json
import locale
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Windows 终端默认 GBK，强制 UTF-8 避免中文乱码
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _skills_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _global_skills_root() -> Path:
    return Path.home() / ".claude" / "skills"


def _module_entry(skill: str) -> str:
    return f"{skill}.adapter.main"


def _subprocess_env() -> dict:
    """构建 subprocess 环境变量，PYTHONPATH 包含项目级和全局 skills 目录。"""
    import os
    env = os.environ.copy()
    paths = [str(_skills_root()), str(_global_skills_root())]
    existing = env.get("PYTHONPATH", "")
    if existing:
        paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _subprocess_encoding() -> str:
    return "utf-8"


def _load_config() -> dict:
    cfg_path = Path(__file__).resolve().parent / "config.json"
    return json.loads(cfg_path.read_text(encoding="utf-8"))


ROUTING_TO_SKILL = {
    "DTVPlayer": "control_dtvplayer",
    "TG39": "control_tg39",
    "AP": "control_ap",
}


class APProcess:
    """管理 AP 音频分析仪的 stdin 服务进程。"""

    READ_TIMEOUT_S = 120

    def __init__(self, impedance: float, no_relay: bool = False):
        cmd = [
            sys.executable,
            "-m",
            _module_entry("control_ap"),
            "serve",
            "--impedance",
            str(impedance),
        ]
        if no_relay:
            cmd.append("--no-relay")
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding=_subprocess_encoding(), errors="replace",
            env=_subprocess_env(),
        )
        self._stderr_thread = threading.Thread(target=self._forward_stderr, daemon=True)
        self._stderr_thread.start()
        _log("[AP] 正在初始化 AP 进程 (impedance={}Ω)...".format(impedance))
        init_resp = self._read_response(timeout=self.READ_TIMEOUT_S)
        if init_resp.get("status") != "OK":
            raise RuntimeError(f"AP 初始化失败: {init_resp.get('message', '')}")
        _log("[AP] 初始化完成: {}".format(init_resp.get("message", "")))

    def send(self, cmd_obj: dict) -> dict:
        line = json.dumps(cmd_obj, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()
        return self._read_response(timeout=self.READ_TIMEOUT_S)

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

    def _forward_stderr(self) -> None:
        """将 AP 子进程的 stderr 透传到本进程 stderr，便于排查。"""
        try:
            for line in self._proc.stderr:
                text = line.rstrip()
                if text:
                    _log(f"[AP:stderr] {text}")
        except Exception:
            pass

    def _read_response(self, timeout: float = 0) -> dict:
        result: list = []
        exc: list = []

        def _reader():
            try:
                line = self._proc.stdout.readline()
                result.append(line)
            except Exception as e:
                exc.append(e)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=timeout if timeout > 0 else None)
        if t.is_alive():
            raise RuntimeError(f"AP 进程 {timeout}s 内无响应（可能卡死）")
        if exc:
            raise RuntimeError(f"AP 进程读取异常: {exc[0]}")
        line = result[0] if result else ""
        if not line:
            raise RuntimeError("AP 进程已退出（stdout 为空）")
        return json.loads(line.strip())


def _run_subprocess(skill: str, args: List[str]) -> dict:
    """调用子 skill 的 main.py 并返回 JSON 结果。"""
    cmd = [sys.executable, "-m", _module_entry(skill)] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding=_subprocess_encoding(),
        errors="replace",
        env=_subprocess_env(),
    )
    if result.returncode != 0 and not result.stdout.strip():
        return {"status": "ERROR", "message": result.stderr.strip() or f"退出码 {result.returncode}"}
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return {"status": "ERROR", "message": f"输出非 JSON: {result.stdout[:200]}"}


def _run_tv(args: List[str]) -> dict:
    return _run_subprocess("control_webos_wee", args)


_tv_adapter = None


def _get_tv():
    global _tv_adapter
    if _tv_adapter is None:
        skills_paths = [str(_skills_root()), str(_global_skills_root())]
        for p in skills_paths:
            if p not in sys.path:
                sys.path.insert(0, p)
        from control_webos_wee.adapter.webos import WebOSTVAdapter
        cfg_path = _skills_root() / "control_webos_wee" / "adapter" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        _tv_adapter = WebOSTVAdapter(cfg)
    return _tv_adapter


def _tv_set_volume(level: int) -> dict:
    try:
        ok = _get_tv().set_volume(level)
        return {"status": "OK"} if ok else {"status": "ERROR", "message": f"set-volume {level} 失败"}
    except Exception as e:
        return {"status": "ERROR", "message": str(e)}


def _tv_switch_channel(channel: str, test_case: dict = None) -> dict:
    try:
        ok = _get_tv().switch_channel(channel, test_case)
        return {"status": "OK"} if ok else {"status": "ERROR", "message": f"切源 {channel} 失败"}
    except Exception as e:
        return {"status": "ERROR", "message": str(e)}


def _tv_set_sound_output(output: str) -> dict:
    try:
        ok = _get_tv().set_sound_output(output)
        return {"status": "OK"} if ok else {"status": "ERROR", "message": f"set-sound-output {output} 失败"}
    except Exception as e:
        return {"status": "ERROR", "message": str(e)}


def _tv_cleanup():
    global _tv_adapter
    if _tv_adapter is not None:
        try:
            _tv_adapter.cleanup()
        except Exception:
            pass
        _tv_adapter = None


def _play_signal(routing: dict, channel_name: str, test_case: dict, ap: APProcess) -> dict:
    """根据路由表调用对应信号源。"""
    source = routing.get(channel_name)
    if not source:
        return {"status": "ERROR", "message": f"未知通道路由: {channel_name}"}

    if source == "AP":
        connector = "HDMI" if channel_name == "HDMI" else "AV"
        freq = float(test_case.get("frequency", 1000))
        if channel_name == "HDMI":
            level = float(test_case.get("db_level", -12))
        else:
            voltage_str = str(test_case.get("voltage", "500mV"))
            level = _parse_voltage(voltage_str)
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


def _build_report_measurements(measurements: list) -> list:
    """将 main.py 格式的测量数据转为 reporter.py 期望的格式。"""
    result = []
    for m in measurements:
        channels_data = m.get("summary", {}).get("channels", {})
        test_case = {"kind": m.get("case", "standard").replace("max_input", "max")}
        if "standard" in m.get("case", ""):
            test_case["kind"] = "standard"
        elif "max" in m.get("case", ""):
            test_case["kind"] = "max"
        result.append({
            "channel": m["channel"],
            "test_case": test_case,
            "measurement": channels_data,
        })
    return result


LR_IMBALANCE_RATIO = 0.15


def _summarize_hp(measurement: dict, config: dict) -> dict:
    """耳机模式：AP 返回 Vrms，转换为 mVrms 判定 130-150 mV 范围。"""
    hp_cfg = config.get("headphone", {})
    target_min_mv = float(hp_cfg.get("target_vrms_mv_min", 130))
    target_max_mv = float(hp_cfg.get("target_vrms_mv_max", 150))
    thd_limit = float(hp_cfg.get("thd_n_max_percent", 10))

    powers_v = [float(v.get("power", 0)) for v in measurement.values()]
    powers_mv = [p * 1000.0 for p in powers_v]
    thd_values = [float(v.get("thd_n", 0)) for v in measurement.values()]
    avg_mv = sum(powers_mv) / len(powers_mv) if powers_mv else 0.0
    max_thd = max(thd_values) if thd_values else 0.0

    all_in_range = all(target_min_mv <= mv <= target_max_mv for mv in powers_mv)
    thd_ok = max_thd <= thd_limit
    passed = all_in_range and thd_ok

    return {
        "avg_vrms_mv": round(avg_mv, 2),
        "target_range_mv": [target_min_mv, target_max_mv],
        "max_thd_n_percent": round(max_thd, 4),
        "thd_limit_percent": thd_limit,
        "passed": passed,
        "channels": {k: {"vrms_mv": round(float(v.get("power", 0)) * 1000, 2), "thd_n": round(float(v.get("thd_n", 0)), 4)} for k, v in measurement.items()},
    }


def _summarize(measurement: dict, config: dict, test_case: dict) -> dict:
    powers = [float(v.get("power", 0)) for v in measurement.values()]
    thd_values = [float(v.get("thd_n", 0)) for v in measurement.values()]
    target_power = float(test_case.get("target_power_w", config.get("rated_power", 0)))
    tolerance = float(config.get("power_tolerance_percent", 10)) / 100.0
    target_min = target_power * (1 - tolerance)
    target_max = target_power * (1 + tolerance)
    max_thd = max(thd_values) if thd_values else 0.0
    max_thd_limit = float(test_case.get("thd_n_max_percent", config.get("thd_n_max_percent", 10)))

    # L/R 平衡检查（用目标功率的百分比作为阈值）
    lr_imbalance = (max(powers) - min(powers)) if len(powers) >= 2 else 0.0
    lr_limit_w = target_power * LR_IMBALANCE_RATIO
    lr_balanced = lr_imbalance <= lr_limit_w

    # 每个声道都必须在规格范围内
    all_channels_in_range = all(target_min <= p <= target_max for p in powers)
    thd_ok = max_thd <= max_thd_limit

    passed = lr_balanced and all_channels_in_range and thd_ok

    fail_reasons = []
    if not lr_balanced:
        fail_reasons.append(
            f"左右声道功率差异过大 (差值={lr_imbalance:.3f}W, 限值{lr_limit_w:.1f}W)，请检查测试环境和接线"
        )
    if not all_channels_in_range:
        oob = [f"{k}={v.get('power', 0):.3f}W" for k, v in measurement.items()
               if not (target_min <= float(v.get("power", 0)) <= target_max)]
        fail_reasons.append(f"功率不在范围 [{target_min:.1f}, {target_max:.1f}]W: {', '.join(oob)}")
    if not thd_ok:
        fail_reasons.append(f"THD+N={max_thd:.3f}% 超过限值 {max_thd_limit:.1f}%")

    return {
        "target_power_w": target_power,
        "target_range": [round(target_min, 4), round(target_max, 4)],
        "max_thd_n_percent": round(max_thd, 4),
        "thd_limit_percent": max_thd_limit,
        "lr_imbalance_w": round(lr_imbalance, 4),
        "lr_limit_w": round(lr_limit_w, 4),
        "lr_balanced": lr_balanced,
        "passed": passed,
        "fail_reasons": fail_reasons,
        "channels": {k: {"power": round(float(v.get("power", 0)), 4), "thd_n": round(float(v.get("thd_n", 0)), 4)} for k, v in measurement.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="音频功率自检（仅测量，不修改参数）")
    parser.add_argument("--channels", nargs="*", help="要测的通道列表")
    parser.add_argument("--impedance", type=str, help="阻抗 (如 6R 或 6)")
    parser.add_argument("--power", type=float, help="额定功率 (W)")
    parser.add_argument("--preset", type=str, default="default", help="DTV 通道预设名 (如 default, 509-6m)")
    parser.add_argument("--output", type=str, default="spk", choices=["spk", "hp"], help="音频输出: spk=喇叭, hp=耳机")
    parser.add_argument("--no-relay", action="store_true", help="跳过水泥负载继电器自动切换（手动切换时使用）")
    args = parser.parse_args()

    config = _load_config()

    if args.impedance:
        config["impedance"] = args.impedance
    if args.power:
        config["rated_power"] = args.power

    impedance_ohm = _parse_impedance(str(config.get("impedance", "6R")))
    routing = config.get("routing", {})
    switch_wait = float(config.get("switch_channel_wait_s", 5.0))
    signal_wait = float(config.get("signal_play_wait_s", 10.0))

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
        result = {"status": "ERROR", "message": "未找到启用的测试通道"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    # 初始化 AP（服务模式）
    _log("=" * 50)
    _log("音频功率自检")
    _log(f"通道: {', '.join(n for n, _ in enabled_channels)}")
    _log(f"阻抗: {config.get('impedance')}  额定功率: {config.get('rated_power')}W")
    _log("=" * 50)
    try:
        ap = APProcess(impedance_ohm, no_relay=args.no_relay)
    except Exception as e:
        result = {"status": "ERROR", "message": f"AP 启动失败: {e}"}
        _log(f"ERROR: {result['message']}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    is_hp = args.output == "hp"

    # 耳机模式：切换 AP 到电压模式 + TV 输出到耳机
    if is_hp:
        ap.set_mode("voltage")
        tv_resp = _tv_set_sound_output("hp")
        if tv_resp.get("status") != "OK":
            ap.quit()
            result = {"status": "ERROR", "message": f"切换耳机输出失败: {tv_resp.get('message', '')}"}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1

    # 设置 TV 音量 100
    _log("设置 TV 音量为 100...")
    tv_resp = _tv_set_volume(100)
    if tv_resp.get("status") != "OK":
        ap.quit()
        result = {"status": "ERROR", "message": f"设置音量失败: {tv_resp.get('message', '')}"}
        _log(f"ERROR: {result['message']}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    _log("TV 音量设置完成")

    # 逐通道/用例测量
    measurements = []
    all_passed = True
    steps = []
    total_cases = sum(len(cfg.get("test_cases", [])) for _, cfg in enabled_channels)
    case_idx = 0

    try:
        for channel_name, channel_cfg in enabled_channels:
            test_cases = channel_cfg.get("test_cases", [])
            if not test_cases:
                continue

            for test_case in test_cases:
                if is_hp and test_case.get("kind") == "max":
                    continue
                case_idx += 1
                case_name = test_case.get("name", "unknown")
                _log(f"[{case_idx}/{total_cases}] === {channel_name}/{case_name} ===")
                effective_case = dict(test_case)
                presets = channel_cfg.get("presets")
                if presets:
                    preset_params = presets.get(args.preset) or presets.get("default", {})
                    for k, v in preset_params.items():
                        if k not in effective_case:
                            effective_case[k] = v
                if args.power is not None:
                    # CLI 显式指定功率时，覆盖各用例内的 target_power_w，确保按目标规格判定
                    effective_case["target_power_w"] = float(config.get("rated_power", args.power))

                # 切源
                _log(f"[{case_idx}/{total_cases}] 切源到 {channel_name}...")
                resp = _tv_switch_channel(channel_name, effective_case)
                if resp.get("status") != "OK":
                    msg = f"{channel_name} 切源失败: {resp.get('message', '')}"
                    _log(f"[{case_idx}/{total_cases}] ERROR: {msg}")
                    steps.append(f"ERROR: {msg}")
                    all_passed = False
                    continue
                _log(f"[{case_idx}/{total_cases}] 切源完成，等待 {switch_wait}s...")
                time.sleep(switch_wait)

                # 播放信号
                signal_source = routing.get(channel_name, "?")
                _log(f"[{case_idx}/{total_cases}] 播放信号 ({signal_source})...")
                resp = _play_signal(routing, channel_name, effective_case, ap)
                if resp.get("status") != "OK":
                    msg = f"{channel_name}/{case_name} 信号播放失败: {resp.get('message', '')}"
                    _log(f"[{case_idx}/{total_cases}] ERROR: {msg}")
                    steps.append(f"ERROR: {msg}")
                    all_passed = False
                    continue
                _log(f"[{case_idx}/{total_cases}] 信号已播放，等待 {signal_wait}s 稳定...")
                time.sleep(signal_wait)

                # AP 测量
                _log(f"[{case_idx}/{total_cases}] AP 测量中...")
                resp = ap.measure()
                if resp.get("status") != "OK":
                    msg = f"{channel_name}/{case_name} 测量失败: {resp.get('message', '')}"
                    _log(f"[{case_idx}/{total_cases}] ERROR: {msg}")
                    steps.append(f"ERROR: {msg}")
                    all_passed = False
                    continue

                measurement = resp["data"]
                if is_hp:
                    summary = _summarize_hp(measurement, config)
                else:
                    summary = _summarize(measurement, config, effective_case)
                measurements.append({
                    "channel": channel_name,
                    "case": case_name,
                    "summary": summary,
                })
                verdict = "PASS" if summary["passed"] else "FAIL"
                if is_hp:
                    steps.append(f"{channel_name}/{case_name}: {verdict} avg={summary['avg_vrms_mv']:.1f}mV thd={summary['max_thd_n_percent']:.3f}%")
                    _log(f"[{case_idx}/{total_cases}] {verdict} avg={summary['avg_vrms_mv']:.1f}mV thd={summary['max_thd_n_percent']:.3f}%")
                else:
                    ch = summary["channels"]
                    l_w = ch.get("L", {}).get("power", 0)
                    r_w = ch.get("R", {}).get("power", 0)
                    steps.append(f"{channel_name}/{case_name}: {verdict} L={l_w:.3f}W R={r_w:.3f}W thd={summary['max_thd_n_percent']:.3f}%")
                    _log(f"[{case_idx}/{total_cases}] {verdict} L={l_w:.3f}W R={r_w:.3f}W thd={summary['max_thd_n_percent']:.3f}%")
                    for reason in summary.get("fail_reasons", []):
                        steps.append(f"  -> {reason}")
                        _log(f"[{case_idx}/{total_cases}]   -> {reason}")
                if not summary["passed"]:
                    all_passed = False

    finally:
        _log("清理：关闭 AP 输出...")
        ap.generator_off()
        if is_hp:
            _tv_set_sound_output("spk")
        ap.quit()
        _tv_cleanup()
        _log("AP 进程已退出")

    # 输出结果
    _log("=" * 50)
    _log(f"自检完成: {'全部通过' if all_passed else '存在未达标项'}")
    _log("=" * 50)
    result = {
        "status": "SUCCESS" if all_passed else "FAILED",
        "message": "全部通过" if all_passed else "存在未达标项",
        "all_passed": all_passed,
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
        from pathlib import Path as _Path
        import importlib.util as _ilu
        reporter_path = _Path(__file__).resolve().parent / "reporter.py"
        spec = _ilu.spec_from_file_location("_reporter", reporter_path)
        reporter = _ilu.module_from_spec(spec)
        spec.loader.exec_module(reporter)
        report_path = reporter.generate_report(
            report_measurements,
            power_spec=power_spec,
            mode="self_check",
            os_type=config.get("os_type"),
            output_type=args.output,
        )
        result["report_path"] = report_path
        steps.append(f"报告已生成: {report_path}")
    except Exception as e:
        steps.append(f"报告生成失败（不影响测试结果）: {e}")
        result["report_path"] = ""

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
