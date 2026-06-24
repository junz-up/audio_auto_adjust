"""audio-report-test CLI：测试部自动测试，按公司模板 QR-DQC-HT-202 填写测试报告。

支持的子命令：
    python main.py all                          # 全套测试（喇叭性能+曲线+耳机性能+耳机曲线）
    python main.py speaker-performance          # 中高音喇叭性能（多通道，音量100）
    python main.py speaker-curve                # 中高音喇叭音量曲线（AV/ATV/HDMI）
    python main.py headphone-performance        # 耳机性能（AV/ATV/HDMI）
    python main.py headphone-curve              # 耳机音量曲线（AV）

所有命令输出 JSON 到 stdout:
    {"status": "OK"|"ERROR", "report_path": "...", "results": [...], ...}
"""
from __future__ import annotations

import argparse
import io
import json
import locale
import math
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


def _ensure_utf8_io() -> None:
    import io
    for stream_name in ("stderr", "stdout"):
        stream = getattr(sys, stream_name)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        elif hasattr(stream, "buffer"):
            wrapped = io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace")
            setattr(sys, stream_name, wrapped)


_ensure_utf8_io()


def _skills_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _global_skills_root() -> Path:
    return Path.home() / ".claude" / "skills"


def _module_entry(skill: str) -> str:
    return f"{skill}.adapter.main"


def _subprocess_env() -> dict:
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


def _run_subprocess(skill: str, args: List[str]) -> dict:
    cmd = [sys.executable, "-m", _module_entry(skill)] + args
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding=_subprocess_encoding(), errors="replace",
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


class APProcess:
    """AP 音频分析仪 stdin 服务进程封装（同 audio-self-check）。"""

    READ_TIMEOUT_S = 300

    def __init__(self, impedance: float, no_relay: bool = False):
        cmd = [
            sys.executable, "-m", _module_entry("control_ap"),
            "serve", "--impedance", str(impedance),
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
        _log(f"[AP] 初始化中 (impedance={impedance}Ω)...")
        init_resp = self._read_response(timeout=self.READ_TIMEOUT_S)
        if init_resp.get("status") != "OK":
            raise RuntimeError(f"AP 初始化失败: {init_resp.get('message', '')}")
        _log(f"[AP] 初始化完成: {init_resp.get('message', '')}")

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

    def measure_freq_response(self, freq_start: float, freq_stop: float,
                              num_points: int, level: float, level_unit: str,
                              dbr_reference_vrms: float = None,
                              report_path: str = None) -> dict:
        cmd_obj = {
            "cmd": "measure_freq_response",
            "freq_start": freq_start, "freq_stop": freq_stop,
            "num_points": num_points, "level": level, "level_unit": level_unit,
        }
        if dbr_reference_vrms is not None:
            cmd_obj["dbr_reference_vrms"] = dbr_reference_vrms
        if report_path is not None:
            cmd_obj["report_path"] = report_path
        return self.send(cmd_obj)

    def measure_crosstalk(self, freq: float, level: float, level_unit: str) -> dict:
        return self.send({
            "cmd": "measure_crosstalk",
            "freq": freq, "level": level, "level_unit": level_unit,
        })

    def measure_phase(self, freq: float, level: float, level_unit: str) -> dict:
        return self.send({
            "cmd": "measure_phase",
            "freq": freq, "level": level, "level_unit": level_unit,
        })

    def measure_snr_dbr(self, settle_s: float = 3.0) -> dict:
        return self.send({"cmd": "measure_snr_dbr", "settle_s": settle_s})

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
            raise RuntimeError(f"AP 进程 {timeout}s 内无响应")
        if exc:
            raise RuntimeError(f"AP 读取异常: {exc[0]}")
        line = result[0] if result else ""
        if not line:
            raise RuntimeError("AP 进程已退出")
        return json.loads(line.strip())


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


def _judge_power(power_w: float, target_w: float, tolerance_pct: float, thd: float, thd_limit: float) -> dict:
    """喇叭功率/THD 判定。"""
    low = target_w * (1 - tolerance_pct / 100.0)
    high = target_w * (1 + tolerance_pct / 100.0)
    power_ok = low <= power_w <= high
    thd_ok = thd <= thd_limit
    passed = power_ok and thd_ok
    reasons = []
    if not power_ok:
        reasons.append(f"功率 {power_w:.3f}W 不在 [{low:.2f},{high:.2f}]W")
    if not thd_ok:
        reasons.append(f"THD+N {thd:.3f}% > {thd_limit:.1f}%")
    return {"passed": passed, "reasons": reasons}


def _judge_vrms(vrms_mv: float, vmin: float, vmax: float, thd: float, thd_limit: float) -> dict:
    """耳机电压/THD 判定。"""
    vrms_ok = vmin <= vrms_mv <= vmax
    thd_ok = thd <= thd_limit
    passed = vrms_ok and thd_ok
    reasons = []
    if not vrms_ok:
        reasons.append(f"Vrms {vrms_mv:.1f}mV 不在 [{vmin},{vmax}]mV")
    if not thd_ok:
        reasons.append(f"THD+N {thd:.3f}% > {thd_limit:.1f}%")
    return {"passed": passed, "reasons": reasons}


def _switch_and_play(channel_name: str, test_case: dict, routing: dict, ap: APProcess, switch_wait: float, signal_wait: float) -> dict:
    """切源 + 播放 + 等待稳定。返回 {"status": "OK"} 或错误。"""
    resp = _tv_switch_channel(channel_name)
    if resp.get("status") != "OK":
        return {"status": "ERROR", "message": f"切源 {channel_name} 失败: {resp.get('message', '')}"}
    time.sleep(switch_wait)

    resp = _play_signal(routing, channel_name, test_case, ap)
    if resp.get("status") != "OK":
        return {"status": "ERROR", "message": f"播放信号失败: {resp.get('message', '')}"}
    time.sleep(signal_wait)
    return {"status": "OK"}


# ---------- 信噪比辅助（闭环搜索音量 + dBrA 参考法测量） ----------


def _binary_search_volume_for_target_power(
    ap: APProcess,
    target_w: float,
    min_vol: int,
    max_vol: int,
    max_iters: int,
    rel_tol: float,
    settle_s: float,
) -> dict:
    """二分搜索 TV 音量，使功放输出功率收敛到 target_w。

    返回 {
        "ok": bool,
        "volume": int,              # 最终音量
        "signal_power_w": (L, R),   # 信号功率 (W)
        "thd": (L, R),              # THD+N (%)
        "reason": str,              # 失败时说明
    }
    """
    lo, hi = int(min_vol), int(max_vol)
    best = None
    for _ in range(int(max_iters)):
        if lo > hi:
            break
        mid = (lo + hi) // 2
        _tv_set_volume(mid)
        time.sleep(settle_s)
        m = ap.measure()
        if m.get("status") != "OK":
            return {"ok": False, "reason": f"AP 测量失败: {m.get('message')}"}
        l_w = float(m["data"].get("L", {}).get("power", 0))
        r_w = float(m["data"].get("R", {}).get("power", 0))
        l_thd = float(m["data"].get("L", {}).get("thd_n", 0))
        r_thd = float(m["data"].get("R", {}).get("thd_n", 0))
        avg = (l_w + r_w) / 2.0
        best = {
            "volume": mid,
            "signal_power_w": (l_w, r_w),
            "thd": (l_thd, r_thd),
            "avg_w": avg,
        }
        if abs(avg - target_w) <= rel_tol * target_w:
            return {"ok": True, **best}
        if avg < target_w:
            lo = mid + 1
        else:
            hi = mid - 1
    # 未收敛，返回最接近的一次
    if best is not None:
        return {"ok": False, "reason": f"音量搜索未收敛 (avg={best['avg_w']:.4f}W, target={target_w}W)", **best}
    return {"ok": False, "reason": "音量搜索无结果"}


def _measure_snr(
    ap: APProcess,
    routing: dict,
    channel_name: str,
    test_case: dict,
    snr_cfg: dict,
    target_w: float,
    switch_wait: float,
    signal_wait: float,
) -> dict:
    """完整 SNR 测量流程（DQC-HT-QE-0061 §7.3.1）：
    1) 切源 + 播放信号
    2) 二分搜索音量使输出≈target_w（W 模式，500mW）
    3) 调用 AP measure_snr_dbr（Signal Path Setup 下 Set A 清零 + Elliptic 滤波 + 读 dBrA）
    4) SNR = |RMS dBrA| = signal_dBrA - noise_dBrA
    """
    noise_settle = float(snr_cfg.get("noise_settle_s", 3.0))
    volume_settle = float(snr_cfg.get("volume_settle_s", 2.0))

    resp = _switch_and_play(channel_name, test_case, routing, ap, switch_wait, signal_wait)
    if resp["status"] != "OK":
        return {"ok": False, "reason": resp["message"]}

    search = _binary_search_volume_for_target_power(
        ap=ap,
        target_w=target_w,
        min_vol=int(snr_cfg.get("volume_search_min", 1)),
        max_vol=int(snr_cfg.get("volume_search_max", 100)),
        max_iters=int(snr_cfg.get("volume_search_max_iters", 14)),
        rel_tol=float(snr_cfg.get("power_relative_tolerance", 0.05)),
        settle_s=volume_settle,
    )
    if not search["ok"] or "volume" not in search:
        return {"ok": False, "reason": search.get("reason", "音量搜索失败")}

    l_sig, r_sig = search["signal_power_w"]

    # 调用 AP dBrA 参考法 SNR 测量（Set A 清零 + Elliptic 滤波 + 读 RMS dBrA）
    snr_resp = ap.measure_snr_dbr(settle_s=noise_settle)
    if snr_resp.get("status") != "OK":
        return {"ok": False, "reason": f"AP dBrA SNR 测量失败: {snr_resp.get('message', '')}"}

    data = snr_resp.get("data", {})
    l_snr = float(data.get("L", 0))
    r_snr = float(data.get("R", 0))

    return {
        "ok": True,
        "volume": search["volume"],
        "signal_power_w": (l_sig, r_sig),
        "snr_db": (l_snr, r_snr),
        "signal_dBrA_L": float(data.get("signal_dBrA_L", 0)),
        "signal_dBrA_R": float(data.get("signal_dBrA_R", 0)),
        "noise_dBrA_L": float(data.get("noise_dBrA_L", 0)),
        "noise_dBrA_R": float(data.get("noise_dBrA_R", 0)),
        "converged": search["ok"],
    }


# ---------- 中高音喇叭性能测试 ----------

def run_speaker_performance(config: dict, ap: APProcess, channel_filter: Optional[List[str]] = None) -> List[dict]:
    """中高音喇叭性能测试：音量=100，多通道，每通道 4 项 (标准功率/THD + 最大输入功率/THD)。"""
    spk_cfg = config["speaker_performance"]
    routing = config["routing"]
    switch_wait = float(config["switch_channel_wait_s"])
    signal_wait = float(config["signal_play_wait_s"])
    target_power = float(config["rated_power_w"])
    power_tol = float(config["power_tolerance_percent"])
    thd_limit = float(config["thd_n_max_percent"])

    _log("切换 TV 音频输出到喇叭")
    _tv_set_sound_output("spk")
    _log("设置 TV 音量=100")
    _tv_set_volume(100)

    results = []
    for ch_name, ch_cfg in spk_cfg["channels"].items():
        if not ch_cfg.get("enabled", False):
            continue
        if channel_filter and ch_name not in channel_filter:
            _log(f"跳过通道: {ch_name} (不在 --channels 过滤列表中)")
            continue
        _log(f"\n=== 喇叭性能: {ch_name} (行 {ch_cfg['row_start']}) ===")
        ch_results = {"channel": ch_name, "row_start": ch_cfg["row_start"], "cases": []}
        for i, case in enumerate(ch_cfg["cases"]):
            _log(f"  [{i+1}/4] {case['kind']}")
            resp = _switch_and_play(ch_name, case, routing, ap, switch_wait, signal_wait)
            if resp["status"] != "OK":
                _log(f"    ERROR: {resp['message']}")
                ch_results["cases"].append({"kind": case["kind"], "error": resp["message"]})
                continue

            m = ap.measure()
            if m.get("status") != "OK":
                _log(f"    测量失败: {m.get('message')}")
                ch_results["cases"].append({"kind": case["kind"], "error": m.get("message")})
                continue

            l_data = m["data"].get("L", {})
            r_data = m["data"].get("R", {})
            l_power = float(l_data.get("power", 0))
            r_power = float(r_data.get("power", 0))
            l_thd = float(l_data.get("thd_n", 0))
            r_thd = float(r_data.get("thd_n", 0))

            is_power = case["metric"] == "power"
            if is_power:
                l_val, r_val = l_power, r_power
                l_j = _judge_power(l_power, target_power, power_tol, l_thd, thd_limit)
                r_j = _judge_power(r_power, target_power, power_tol, r_thd, thd_limit)
            else:
                l_val, r_val = l_thd, r_thd
                l_j = {"passed": l_thd <= thd_limit, "reasons": [] if l_thd <= thd_limit else [f"THD {l_thd:.3f}%"]}
                r_j = {"passed": r_thd <= thd_limit, "reasons": [] if r_thd <= thd_limit else [f"THD {r_thd:.3f}%"]}

            case_result = {
                "kind": case["kind"],
                "row": ch_cfg["row_start"] + i,
                "L": round(l_val, 4),
                "R": round(r_val, 4),
                "L_passed": l_j["passed"],
                "R_passed": r_j["passed"],
                "passed": l_j["passed"] and r_j["passed"],
                "reasons": l_j["reasons"] + r_j["reasons"],
            }
            ch_results["cases"].append(case_result)
            _log(f"    L={l_val:.4f} R={r_val:.4f} -> {'PASS' if case_result['passed'] else 'FAIL'}")

        # 喇叭信噪比（仅 AV 通道，音量闭环搜索到 500mW，dBrA 参考法测量）
        if ch_name == "AV":
            snr_cfg = config.get("snr", {})
            snr_target_w = float(snr_cfg.get("speaker_target_power_w", 0.5))
            snr_threshold = float(snr_cfg.get("speaker_threshold_db", 58))
            snr_row = int(snr_cfg.get("speaker_row", 59))
            _log(f"  [SNR] AV 通道信噪比 (目标 {snr_target_w}W, 阈值 >{snr_threshold}dB)")
            # 先把音量拉回 100，避免上一轮 max 测试残留影响搜索起点
            _tv_set_volume(100)
            time.sleep(float(config["volume_settle_s"]))
            # 重新切源 + 播放标准信号（SNR 需要标准输入 500mVrms 1kHz）
            snr_case = next((c for c in ch_cfg["cases"] if c["kind"] == "standard_power"), ch_cfg["cases"][0])
            snr_result = _measure_snr(
                ap=ap,
                routing=routing,
                channel_name=ch_name,
                test_case=snr_case,
                snr_cfg=snr_cfg,
                target_w=snr_target_w,
                switch_wait=switch_wait,
                signal_wait=signal_wait,
            )
            if not snr_result["ok"]:
                _log(f"    SNR 测量失败: {snr_result.get('reason')}")
                ch_results["cases"].append({"kind": "snr", "row": snr_row, "error": snr_result.get("reason")})
            else:
                l_snr, r_snr = snr_result["snr_db"]
                l_ok = l_snr >= snr_threshold
                r_ok = r_snr >= snr_threshold
                reasons = []
                if not l_ok:
                    reasons.append(f"L SNR {l_snr:.2f}dB < {snr_threshold}dB")
                if not r_ok:
                    reasons.append(f"R SNR {r_snr:.2f}dB < {snr_threshold}dB")
                ch_results["cases"].append({
                    "kind": "snr",
                    "row": snr_row,
                    "L": round(l_snr, 2),
                    "R": round(r_snr, 2),
                    "L_passed": l_ok,
                    "R_passed": r_ok,
                    "passed": l_ok and r_ok,
                    "reasons": reasons,
                    "volume_at_target": snr_result["volume"],
                    "signal_power_w": [round(x, 6) for x in snr_result["signal_power_w"]],
                    "signal_dBrA_L": round(snr_result["signal_dBrA_L"], 2),
                    "signal_dBrA_R": round(snr_result["signal_dBrA_R"], 2),
                    "noise_dBrA_L": round(snr_result["noise_dBrA_L"], 2),
                    "noise_dBrA_R": round(snr_result["noise_dBrA_R"], 2),
                    "converged": snr_result["converged"],
                })
                _log(f"    vol={snr_result['volume']} L={l_snr:.2f}dB R={r_snr:.2f}dB -> {'PASS' if (l_ok and r_ok) else 'FAIL'}")
            # 恢复音量=100，供后续通道使用
            _tv_set_volume(100)
            time.sleep(float(config["volume_settle_s"]))

            # ---- 频率响应（AV 通道，音量闭环到 500mW 后扫频） ----
            idx_cfg = config.get("speaker_index", {})
            fr_row = int(idx_cfg.get("freq_response_row", 63))
            _log(f"  [FreqResp] AV 通道频率响应 ({idx_cfg.get('freq_sweep_start_hz', 20)}-{idx_cfg.get('freq_sweep_stop_hz', 20000)}Hz)")
            _tv_set_volume(100)
            time.sleep(float(config["volume_settle_s"]))
            resp = _switch_and_play(ch_name, snr_case, routing, ap, switch_wait, signal_wait)
            if resp["status"] != "OK":
                _log(f"    FreqResp 切源失败: {resp['message']}")
                ch_results["cases"].append({"kind": "freq_response", "row": fr_row, "error": resp["message"]})
            else:
                search = _binary_search_volume_for_target_power(
                    ap=ap, target_w=snr_target_w,
                    min_vol=int(snr_cfg.get("volume_search_min", 1)),
                    max_vol=int(snr_cfg.get("volume_search_max", 100)),
                    max_iters=int(snr_cfg.get("volume_search_max_iters", 14)),
                    rel_tol=float(snr_cfg.get("power_relative_tolerance", 0.05)),
                    settle_s=float(snr_cfg.get("volume_settle_s", 2.0)),
                )
                if not search.get("ok") and "volume" not in search:
                    _log(f"    FreqResp 音量搜索失败: {search.get('reason')}")
                    ch_results["cases"].append({"kind": "freq_response", "row": fr_row, "error": search.get("reason")})
                else:
                    # 计算 dBr 参考电压（二分搜索收敛后的实测输出电压）
                    dbr_ref = math.sqrt(search["avg_w"] * _parse_impedance(str(config.get("impedance", "6R"))))
                    # 频率响应 PDF 保存到 session 子文件夹
                    _fr_report_path = str(Path(config["_session_dir"]) / f"FreqResponse_AV_{config['_session_ts']}.pdf")
                    fr_resp = ap.measure_freq_response(
                        freq_start=float(idx_cfg.get("freq_sweep_start_hz", 20)),
                        freq_stop=float(idx_cfg.get("freq_sweep_stop_hz", 20000)),
                        num_points=int(idx_cfg.get("freq_sweep_points", 100)),
                        level=float(idx_cfg.get("signal_level_vrms", 0.5)),
                        level_unit="Vrms",
                        dbr_reference_vrms=round(dbr_ref, 6),
                        report_path=_fr_report_path,
                    )
                    if fr_resp.get("status") != "OK":
                        _log(f"    FreqResp 测量失败: {fr_resp.get('message')}")
                        ch_results["cases"].append({"kind": "freq_response", "row": fr_row, "error": fr_resp.get("message")})
                    else:
                        l_data = fr_resp["data"]["L"]
                        r_data = fr_resp["data"]["R"]
                        l_low = l_data["freq_low"]
                        l_high = l_data["freq_high"]
                        r_low = r_data["freq_low"]
                        r_high = r_data["freq_high"]
                        low_limit = float(idx_cfg.get("freq_response_low_limit_hz", 100))
                        high_limit = float(idx_cfg.get("freq_response_high_limit_hz", 15000))
                        l_ok = l_low <= low_limit and l_high >= high_limit
                        r_ok = r_low <= low_limit and r_high >= high_limit
                        reasons = []
                        if not l_ok:
                            reasons.append(f"L {l_low:.0f}-{l_high:.0f}Hz 未覆盖 {low_limit:.0f}-{high_limit:.0f}Hz")
                        if not r_ok:
                            reasons.append(f"R {r_low:.0f}-{r_high:.0f}Hz 未覆盖 {low_limit:.0f}-{high_limit:.0f}Hz")
                        fr_report = fr_resp["data"].get("report_path", _fr_report_path)
                        ch_results["cases"].append({
                            "kind": "freq_response", "row": fr_row,
                            "L": round(l_low, 1), "R": round(l_high, 1),
                            "L_passed": l_ok, "R_passed": r_ok,
                            "passed": l_ok and r_ok, "reasons": reasons,
                            "report_path": fr_report,
                        })
                        _log(f"    L={l_low:.0f}-{l_high:.0f}Hz R={r_low:.0f}-{r_high:.0f}Hz -> {'PASS' if (l_ok and r_ok) else 'FAIL'}")
            _tv_set_volume(100)
            time.sleep(float(config["volume_settle_s"]))

            # ---- 串扰比（AV 通道，音量=100，测 1kHz 和 10kHz 取较小值） ----
            ct_row = int(idx_cfg.get("crosstalk_row", 65))
            ct_threshold = float(idx_cfg.get("crosstalk_threshold_db", 50))
            ct_level = float(idx_cfg.get("signal_level_vrms", 0.5))
            _log(f"  [Crosstalk] AV 通道串扰比 (阈值 >{ct_threshold}dB)")
            _tv_set_volume(100)
            time.sleep(float(config["volume_settle_s"]))
            resp = _switch_and_play(ch_name, snr_case, routing, ap, switch_wait, signal_wait)
            if resp["status"] != "OK":
                _log(f"    Crosstalk 切源失败: {resp['message']}")
                ch_results["cases"].append({"kind": "crosstalk", "row": ct_row, "error": resp["message"]})
            else:
                ct_freqs = [float(idx_cfg.get("crosstalk_freq_hz", 1000)), 10000.0]
                ct_results = {}
                for freq in ct_freqs:
                    ct_resp = ap.measure_crosstalk(freq=freq, level=ct_level, level_unit="Vrms")
                    if ct_resp.get("status") == "OK":
                        ct_results[freq] = ct_resp["data"]
                        _log(f"    {freq:.0f}Hz: L={ct_resp['data']['L']:.1f}dB R={ct_resp['data']['R']:.1f}dB")
                    else:
                        _log(f"    {freq:.0f}Hz 测量失败: {ct_resp.get('message')}")
                if not ct_results:
                    ch_results["cases"].append({"kind": "crosstalk", "row": ct_row, "error": "所有频率串扰测量失败"})
                else:
                    worst_l = min(v["L"] for v in ct_results.values())
                    worst_r = min(v["R"] for v in ct_results.values())
                    l_ok = worst_l >= ct_threshold
                    r_ok = worst_r >= ct_threshold
                    reasons = []
                    if not l_ok:
                        reasons.append(f"L crosstalk {worst_l:.1f}dB < {ct_threshold}dB")
                    if not r_ok:
                        reasons.append(f"R crosstalk {worst_r:.1f}dB < {ct_threshold}dB")
                    ch_results["cases"].append({
                        "kind": "crosstalk", "row": ct_row,
                        "L": round(worst_l, 1), "R": round(worst_r, 1),
                        "L_passed": l_ok, "R_passed": r_ok,
                        "passed": l_ok and r_ok, "reasons": reasons,
                    })
                    _log(f"    worst L={worst_l:.1f}dB R={worst_r:.1f}dB -> {'PASS' if (l_ok and r_ok) else 'FAIL'}")
            _tv_set_volume(100)
            time.sleep(float(config["volume_settle_s"]))

            # ---- 相位差（AV 通道，Interchannel Phase 单频测量） ----
            ph_row = int(idx_cfg.get("phase_row", 66))
            ph_max_deg = float(idx_cfg.get("phase_max_deg", 10))
            ph_freq = float(idx_cfg.get("crosstalk_freq_hz", 1000))
            _log(f"  [Phase] AV 通道相位差 ({ph_freq:.0f}Hz)")
            resp = _switch_and_play(ch_name, snr_case, routing, ap, switch_wait, signal_wait)
            if resp["status"] != "OK":
                _log(f"    Phase 切源失败: {resp['message']}")
                ch_results["cases"].append({"kind": "phase", "row": ph_row, "error": resp["message"]})
            else:
                ap.generator_off()
                time.sleep(1.0)
                ph_resp = ap.measure_phase(
                    freq=ph_freq,
                    level=float(idx_cfg.get("signal_level_vrms", 0.5)),
                    level_unit="Vrms",
                )
                if ph_resp.get("status") != "OK":
                    _log(f"    Phase 测量失败: {ph_resp.get('message')}")
                    ch_results["cases"].append({"kind": "phase", "row": ph_row, "error": ph_resp.get("message")})
                else:
                    ph_val = ph_resp["data"]["value"]
                    ok = abs(ph_val) <= ph_max_deg
                    reasons = [] if ok else [f"phase {ph_val:.2f}° exceeds ±{ph_max_deg}°"]
                    ch_results["cases"].append({
                        "kind": "phase", "row": ph_row,
                        "L": round(ph_val, 2), "R": round(ph_val, 2),
                        "passed": ok, "reasons": reasons,
                    })
                    _log(f"    phase={ph_val:.2f}° -> {'PASS' if ok else 'FAIL'}")
            _tv_set_volume(100)
            time.sleep(float(config["volume_settle_s"]))

        results.append(ch_results)
    return results


# ---------- 中高音喇叭音量曲线 ----------

def run_speaker_curve(config: dict, ap: APProcess, channel_filter: Optional[List[str]] = None) -> List[dict]:
    """中高音喇叭音量曲线：AV/ATV/HDMI 通道，音量 5..100 扫点。"""
    curve_cfg = config["speaker_curve"]
    routing = config["routing"]
    switch_wait = float(config["switch_channel_wait_s"])
    signal_wait = float(config["signal_play_wait_s"])
    volumes = curve_cfg["volumes"]

    _log("切换 TV 音频输出到喇叭")
    _tv_set_sound_output("spk")

    results = []
    for ch_name, ch_cfg in curve_cfg["channels"].items():
        if not ch_cfg.get("enabled", False):
            continue
        if channel_filter and ch_name not in channel_filter:
            _log(f"跳过通道: {ch_name} (不在 --channels 过滤列表中)")
            continue
        target_sheet = ch_cfg.get("target_sheet", ch_name)
        _log(f"\n=== 喇叭音量曲线: {ch_name} (写入 sheet: {curve_cfg['sheet'][target_sheet]}) ===")

        _log(f"  切源到 {ch_name}")
        resp = _tv_switch_channel(ch_name)
        if resp.get("status") != "OK":
            _log(f"  切源失败: {resp.get('message')}")
            results.append({"channel": ch_name, "sheet": target_sheet, "error": resp.get("message")})
            continue
        time.sleep(switch_wait)

        # 先按音量=100 设置 AP 输出并稳定一次
        resp = _play_signal(routing, ch_name, ch_cfg, ap)
        if resp.get("status") != "OK":
            _log(f"  信号播放失败: {resp.get('message')}")
            results.append({"channel": ch_name, "sheet": target_sheet, "error": resp.get("message")})
            continue
        time.sleep(signal_wait)

        curve_points = []
        for vol in volumes:
            _log(f"  音量={vol}")
            _tv_set_volume(vol)
            time.sleep(float(config["volume_settle_s"]))
            m = ap.measure()
            if m.get("status") != "OK":
                _log(f"    测量失败: {m.get('message')}")
                curve_points.append({"volume": vol, "error": m.get("message")})
                continue
            l_data = m["data"].get("L", {})
            r_data = m["data"].get("R", {})
            l_w = float(l_data.get("power", 0))
            r_w = float(r_data.get("power", 0))
            # 保留 AP 原始精度（最多 6 位小数），报告模板对 W 值精度要求严格
            curve_points.append({
                "volume": vol,
                "L": l_w,
                "R": r_w,
            })
            _log(f"    L={l_w:.6f}W R={r_w:.6f}W")
        results.append({"channel": ch_name, "sheet": target_sheet, "points": curve_points})
    return results


# ---------- 耳机性能测试 ----------

def run_headphone_performance(config: dict, ap: APProcess, channel_filter: Optional[List[str]] = None) -> List[dict]:
    """耳机性能测试：切到 hp 输出，电压模式，多通道。"""
    hp_cfg = config["headphone_performance"]
    hp_std = config["headphone"]
    routing = config["routing"]
    switch_wait = float(config["switch_channel_wait_s"])
    signal_wait = float(config["signal_play_wait_s"])
    vmin = float(hp_std["target_vrms_mv_min"])
    vmax = float(hp_std["target_vrms_mv_max"])
    thd_limit = float(hp_std["thd_n_max_percent"])

    _log("切换 AP 到电压模式")
    ap.set_mode("voltage")
    _log("切换 TV 输出到耳机")
    _tv_set_sound_output("hp")
    _log("设置 TV 音量=100")
    _tv_set_volume(100)

    results = []
    for ch_name, ch_cfg in hp_cfg["sections"].items():
        if not ch_cfg.get("enabled", False):
            continue
        if channel_filter and ch_name not in channel_filter:
            _log(f"跳过通道: {ch_name} (不在 --channels 过滤列表中)")
            continue
        _log(f"\n=== 耳机性能: {ch_name} (行 {ch_cfg['row_start']}) ===")
        ch_results = {"channel": ch_name, "row_start": ch_cfg["row_start"], "cases": []}
        for i, case in enumerate(ch_cfg["cases"]):
            _log(f"  [{i+1}/4] {case['kind']}")
            resp = _switch_and_play(ch_name, case, routing, ap, switch_wait, signal_wait)
            if resp["status"] != "OK":
                ch_results["cases"].append({"kind": case["kind"], "error": resp["message"]})
                continue

            m = ap.measure()
            if m.get("status") != "OK":
                ch_results["cases"].append({"kind": case["kind"], "error": m.get("message")})
                continue

            l_data = m["data"].get("L", {})
            r_data = m["data"].get("R", {})
            l_v = float(l_data.get("power", 0)) * 1000.0
            r_v = float(r_data.get("power", 0)) * 1000.0
            l_thd = float(l_data.get("thd_n", 0))
            r_thd = float(r_data.get("thd_n", 0))

            is_vrms = case["metric"] == "vrms"
            if is_vrms:
                l_val, r_val = l_v, r_v
                l_j = _judge_vrms(l_v, vmin, vmax, l_thd, thd_limit)
                r_j = _judge_vrms(r_v, vmin, vmax, r_thd, thd_limit)
            else:
                l_val, r_val = l_thd, r_thd
                l_j = {"passed": l_thd <= thd_limit, "reasons": [] if l_thd <= thd_limit else [f"THD {l_thd:.3f}%"]}
                r_j = {"passed": r_thd <= thd_limit, "reasons": [] if r_thd <= thd_limit else [f"THD {r_thd:.3f}%"]}

            case_result = {
                "kind": case["kind"],
                "row": ch_cfg["row_start"] + i,
                "L": round(l_val, 4),
                "R": round(r_val, 4),
                "L_passed": l_j["passed"],
                "R_passed": r_j["passed"],
                "passed": l_j["passed"] and r_j["passed"],
                "reasons": l_j["reasons"] + r_j["reasons"],
            }
            ch_results["cases"].append(case_result)
            _log(f"    L={l_val:.3f} R={r_val:.3f} -> {'PASS' if case_result['passed'] else 'FAIL'}")

        # 耳机信噪比（仅 AV 通道，音量闭环搜索到 100mVrms，dBrA 参考法测量）
        if ch_name == "AV":
            snr_cfg = config.get("snr", {})
            impedance_ohm = _parse_impedance(str(config.get("impedance", "6R")))
            target_mv = float(snr_cfg.get("headphone_target_vrms_mv", 100))
            snr_threshold = float(snr_cfg.get("headphone_threshold_db", 55))
            snr_row = int(snr_cfg.get("headphone_row", 8))
            # 把目标 mVrms 换算成 AP 电压模式下的功率值 (W = (V/1000)² / Z)
            target_w = ((target_mv / 1000.0) ** 2) / impedance_ohm
            _log(f"  [SNR] AV 通道耳机信噪比 (目标 {target_mv}mV ≈ {target_w:.6f}W, 阈值 >{snr_threshold}dB)")
            _tv_set_volume(100)
            time.sleep(float(config["volume_settle_s"]))
            snr_case = next((c for c in ch_cfg["cases"] if c["kind"] == "standard_vrms"), ch_cfg["cases"][0])
            snr_result = _measure_snr(
                ap=ap,
                routing=routing,
                channel_name=ch_name,
                test_case=snr_case,
                snr_cfg=snr_cfg,
                target_w=target_w,
                switch_wait=switch_wait,
                signal_wait=signal_wait,
            )
            if not snr_result["ok"]:
                _log(f"    SNR 测量失败: {snr_result.get('reason')}")
                ch_results["cases"].append({"kind": "snr", "row": snr_row, "error": snr_result.get("reason")})
            else:
                l_snr, r_snr = snr_result["snr_db"]
                l_ok = l_snr >= snr_threshold
                r_ok = r_snr >= snr_threshold
                reasons = []
                if not l_ok:
                    reasons.append(f"L SNR {l_snr:.2f}dB < {snr_threshold}dB")
                if not r_ok:
                    reasons.append(f"R SNR {r_snr:.2f}dB < {snr_threshold}dB")
                ch_results["cases"].append({
                    "kind": "snr",
                    "row": snr_row,
                    "L": round(l_snr, 2),
                    "R": round(r_snr, 2),
                    "L_passed": l_ok,
                    "R_passed": r_ok,
                    "passed": l_ok and r_ok,
                    "reasons": reasons,
                    "volume_at_target": snr_result["volume"],
                    "signal_dBrA_L": round(snr_result["signal_dBrA_L"], 2),
                    "signal_dBrA_R": round(snr_result["signal_dBrA_R"], 2),
                    "noise_dBrA_L": round(snr_result["noise_dBrA_L"], 2),
                    "noise_dBrA_R": round(snr_result["noise_dBrA_R"], 2),
                    "converged": snr_result["converged"],
                })
                _log(f"    vol={snr_result['volume']} L={l_snr:.2f}dB R={r_snr:.2f}dB -> {'PASS' if (l_ok and r_ok) else 'FAIL'}")
            _tv_set_volume(100)
            time.sleep(float(config["volume_settle_s"]))

        results.append(ch_results)

    _log("恢复 TV 输出到喇叭")
    _tv_set_sound_output("spk")
    return results


# ---------- 耳机音量曲线 ----------

def run_headphone_curve(config: dict, ap: APProcess, channel_filter: Optional[List[str]] = None) -> List[dict]:
    """耳机音量曲线：AV 通道，音量 5..100 扫点，输出 Vrms (mV)。"""
    curve_cfg = config["headphone_curve"]
    ch_cfg = curve_cfg["channel"]
    ch_name = ch_cfg["name"]
    routing = config["routing"]
    switch_wait = float(config["switch_channel_wait_s"])
    signal_wait = float(config["signal_play_wait_s"])
    volumes = curve_cfg["volumes"]

    if channel_filter and ch_name not in channel_filter:
        _log(f"耳机曲线固定通道 {ch_name}，不在 --channels 过滤列表中，跳过")
        return []

    _log("切换 AP 到电压模式")
    ap.set_mode("voltage")
    _log("切换 TV 输出到耳机")
    _tv_set_sound_output("hp")

    _log(f"切源到 {ch_name}")
    resp = _tv_switch_channel(ch_name)
    if resp.get("status") != "OK":
        _tv_set_sound_output("spk")
        return [{"error": f"切源失败: {resp.get('message')}"}]
    time.sleep(switch_wait)

    resp = _play_signal(routing, ch_name, ch_cfg, ap)
    if resp.get("status") != "OK":
        _tv_set_sound_output("spk")
        return [{"error": f"信号播放失败: {resp.get('message')}"}]
    time.sleep(signal_wait)

    curve_points = []
    for vol in volumes:
        _log(f"  音量={vol}")
        _tv_set_volume(vol)
        time.sleep(float(config["volume_settle_s"]))
        m = ap.measure()
        if m.get("status") != "OK":
            curve_points.append({"volume": vol, "error": m.get("message")})
            continue
        l_data = m["data"].get("L", {})
        r_data = m["data"].get("R", {})
        # AP voltage模式返回的 power = V²/Z (W)，直接写入曲线 (参考报告单位是 W)
        l_w = float(l_data.get("power", 0))
        r_w = float(r_data.get("power", 0))
        # 同时换算 mVrms 便于日志
        l_mv = l_w * 1000.0
        r_mv = r_w * 1000.0
        curve_points.append({"volume": vol, "L": l_w, "R": r_w})
        _log(f"    L={l_mv:.3f}mV R={r_mv:.3f}mV  (W: {l_w:.6e}, {r_w:.6e})")

    _tv_set_sound_output("spk")
    return [{"channel": ch_name, "points": curve_points}]


# ---------- 顶层入口 ----------

def _cleanup(ap: APProcess, restore_spk: bool):
    _log("清理：关闭 AP 输出")
    try:
        ap.generator_off()
    except Exception:
        pass
    if restore_spk:
        _tv_set_sound_output("spk")
    ap.quit()
    _tv_cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="音频性能自动测试（测试部）")
    # 公共选项放在每个子命令上，避免必须先写 `--channels HDMI all` 这种别扭顺序
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--impedance", type=str, help="阻抗 (如 6R)")
    common.add_argument("--power", type=float, help="额定功率 (W)")
    common.add_argument("--channels", nargs="*", help="过滤测试通道 (如 HDMI AV)，可多选；不指定则跑全部")
    common.add_argument("--no-relay", action="store_true", help="跳过水泥负载继电器自动切换（手动切换时使用）")

    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("all", parents=[common], help="全套测试（喇叭性能+曲线+耳机性能+耳机曲线）")
    sub.add_parser("speaker-performance", parents=[common], help="中高音喇叭性能测试")
    sub.add_parser("speaker-curve", parents=[common], help="中高音喇叭音量曲线")
    sub.add_parser("headphone-performance", parents=[common], help="耳机性能测试")
    sub.add_parser("headphone-curve", parents=[common], help="耳机音量曲线")
    args = parser.parse_args()

    config = _load_config()
    if args.impedance:
        config["impedance"] = args.impedance
    if args.power:
        config["rated_power_w"] = float(args.power)

    channel_filter = args.channels if args.channels else None

    impedance_ohm = _parse_impedance(str(config.get("impedance", "6R")))

    _log("=" * 60)
    _log("音频性能自动测试")
    _log(f"命令: {args.cmd}  阻抗: {config.get('impedance')}  额定功率: {config.get('rated_power_w')}W")
    _log(f"通道过滤: {channel_filter if channel_filter else '(全部)'}")
    _log("=" * 60)

    # 创建本次测试 session 子文件夹
    import datetime as _dt
    _skill_dir = Path(__file__).resolve().parent
    _reports_dir = _skill_dir / config.get("output_dir", "output/reports")
    _session_ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    _session_dir = _reports_dir / f"test_{_session_ts}"
    _session_dir.mkdir(parents=True, exist_ok=True)
    config["_session_dir"] = str(_session_dir)
    config["_session_ts"] = _session_ts
    _log(f"测试报告目录: {_session_dir}")

    try:
        ap = APProcess(impedance_ohm, no_relay=args.no_relay)
    except Exception as e:
        _log(f"AP 启动失败: {e}")
        print(json.dumps({"status": "ERROR", "message": f"AP 启动失败: {e}"}, ensure_ascii=False, indent=2))
        return 1

    # 收集各子测试结果
    speaker_perf_results = None
    speaker_curve_results = None
    hp_perf_results = None
    hp_curve_results = None
    any_error = None
    restore_spk = False

    try:
        if args.cmd in ("all", "speaker-performance"):
            speaker_perf_results = run_speaker_performance(config, ap, channel_filter=channel_filter)
        if args.cmd in ("all", "speaker-curve"):
            ap.generator_off()
            speaker_curve_results = run_speaker_curve(config, ap, channel_filter=channel_filter)
        if args.cmd in ("all", "headphone-performance"):
            ap.generator_off()
            restore_spk = True
            hp_perf_results = run_headphone_performance(config, ap, channel_filter=channel_filter)
        if args.cmd in ("all", "headphone-curve"):
            ap.generator_off()
            restore_spk = True
            hp_curve_results = run_headphone_curve(config, ap, channel_filter=channel_filter)
    except Exception as e:
        any_error = f"测试异常: {e}"
        _log(f"ERROR: {any_error}")
    finally:
        _cleanup(ap, restore_spk)

    # 写 Excel 报告
    try:
        import importlib.util as _ilu
        _here = Path(__file__).resolve().parent
        _spec = _ilu.spec_from_file_location("_audio_report_excel", _here / "excel_reporter.py")
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        report_path = _mod.write_report(
            config=config,
            speaker_perf=speaker_perf_results,
            speaker_curve=speaker_curve_results,
            hp_perf=hp_perf_results,
            hp_curve=hp_curve_results,
        )
    except Exception as e:
        any_error = any_error or f"报告生成失败: {e}"
        report_path = ""
        _log(f"ERROR: {any_error}")

    final_status = "OK" if not any_error else "ERROR"
    output = {
        "status": final_status,
        "report_path": report_path,
        "speaker_performance": speaker_perf_results,
        "speaker_curve": speaker_curve_results,
        "headphone_performance": hp_perf_results,
        "headphone_curve": hp_curve_results,
    }
    if any_error:
        output["message"] = any_error

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if final_status == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
