"""独立相位差 (Phase) 测试 — 逐步执行，AP 保持运行。

参考 DQC-HT-QE-0061 §7.3.6 相位差测试方法：
  1. 输入 500mVrms 1kHz，调音量到最大功率
  2. SFS → Phase, Level=500mV, 20Hz-20kHz
  3. Y轴 ±10deg 限制线
  4. 判定：100Hz-15kHz 范围内相位差在 ±10deg

执行方式：
    python .claude/skills/webos_audio-auto-test/sample/phase_standalone.py
"""
from __future__ import annotations
import io, json, math, subprocess, sys, threading, time, datetime
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent
SKILLS_ROOT = SKILL_DIR.parent

def log(msg):
    print(msg, flush=True)

def _subprocess_env():
    import os
    env = os.environ.copy()
    paths = [str(SKILLS_ROOT), str(Path.home() / ".claude" / "skills")]
    existing = env.get("PYTHONPATH", "")
    if existing:
        paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONIOENCODING"] = "utf-8"
    return env

config = json.loads((SKILL_DIR / "config.json").read_text(encoding="utf-8"))
impedance_str = config.get("impedance", "6R")
impedance_ohm = float(impedance_str.upper().replace("R", ""))

log("=" * 60)
log("相位差 (Phase) 独立测试")
log(f"阻抗: {impedance_str} ({impedance_ohm}Ω)  扫频: 20Hz-20kHz")
log("=" * 60)

# ===== 步骤 0：初始化 TV =====
log("\n【步骤 0】初始化 TV 适配器")
sys.path.insert(0, str(SKILLS_ROOT))
sys.path.insert(0, str(Path.home() / ".claude" / "skills"))
from control_webos_wee.adapter.webos import WebOSTVAdapter
tv_cfg = json.loads((SKILLS_ROOT / "control_webos_wee" / "adapter" / "config.json").read_text(encoding="utf-8"))
tv = WebOSTVAdapter(tv_cfg)
log("  TV 适配器初始化完成")

# ===== 步骤 1：启动 AP =====
log("\n【步骤 1】启动 AP 音频分析仪")
ap_proc = subprocess.Popen(
    [sys.executable, "-m", "control_ap.adapter.main", "serve", "--impedance", str(impedance_ohm), "--no-relay"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", errors="replace", env=_subprocess_env(),
)

def ap_send(cmd_obj):
    ap_proc.stdin.write(json.dumps(cmd_obj, ensure_ascii=False) + "\n")
    ap_proc.stdin.flush()

def ap_read(timeout=300):
    result = []
    def _reader():
        result.append(ap_proc.stdout.readline())
    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise RuntimeError(f"AP {timeout}s 无响应")
    return json.loads(result[0].strip())

def _fwd_stderr():
    try:
        for line in ap_proc.stderr:
            log(f"  [AP:stderr] {line.rstrip()}")
    except Exception:
        pass
threading.Thread(target=_fwd_stderr, daemon=True).start()

log("  等待 AP 初始化...")
init_resp = ap_read(timeout=300)
log(f"  AP 初始化响应: {json.dumps(init_resp, ensure_ascii=False)}")

# ===== 步骤 2：TV 设置 =====
log("\n【步骤 2】设置 TV: 音频输出=喇叭, 音量=100（最大功率）")
tv.set_sound_output("spk")
tv.set_volume(100)
time.sleep(2.0)
log("  完成")

# ===== 步骤 3：切源 AV =====
log("\n【步骤 3】TV 切源到 AV")
tv.switch_channel("AV")
time.sleep(5.0)
log("  完成，等待 5s 稳定")

# ===== 步骤 4：AP 信号发生器 =====
log("\n【步骤 4】AP 开启信号发生器: AV, 1000 Hz, 0.5 Vrms")
ap_send({"cmd": "generator", "on": True, "connector": "AV", "freq": 1000, "level": 0.5})
gen_resp = ap_read(timeout=30)
log(f"  响应: {json.dumps(gen_resp, ensure_ascii=False)}")
time.sleep(10.0)
log("  等待 10s 信号稳定")

# ===== 步骤 5：计算 dBr 参考 =====
log("\n【步骤 5】测量当前输出功率（音量=100，最大功率）")
ap_send({"cmd": "measure"})
m = ap_read(timeout=30)
if m.get("status") == "OK":
    l_w = float(m["data"].get("L", {}).get("power", 0))
    r_w = float(m["data"].get("R", {}).get("power", 0))
    avg_w = (l_w + r_w) / 2.0
    dbr_ref_vrms = math.sqrt(avg_w * impedance_ohm)
    log(f"  L={l_w:.6f}W  R={r_w:.6f}W  avg={avg_w:.6f}W")
    log(f"  dBr 参考电压: {dbr_ref_vrms:.6f} Vrms")
else:
    dbr_ref_vrms = None
    log("  ⚠️ 测量失败，跳过 dBr 清零")

# ===== 步骤 6：相位差 SFS 扫频 =====
log("\n【步骤 6】发送相位差 SFS 扫频命令（含 dBrA 清零 + ±10° 限制线）")
output_dir = SKILL_DIR / "output" / "reports"
output_dir.mkdir(parents=True, exist_ok=True)
ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
report_path = str(output_dir / f"Phase_{ts}.pdf")

phase_cmd = {
    "cmd": "measure_phase",
    "freq_start": 20,
    "freq_stop": 20000,
    "num_points": 100,
    "level": 0.5,
    "level_unit": "Vrms",
    "dbr_reference_vrms": round(dbr_ref_vrms, 6) if dbr_ref_vrms else None,
    "report_path": report_path,
}
log(f"  参数: {json.dumps(phase_cmd, ensure_ascii=False)}")
log(f"  报告保存: {report_path}")
log(f"  ±10° 限制线: 将显示在曲线上")
log("  等待 AP SFS 扫频...")

ap_send(phase_cmd)
phase_resp = ap_read(timeout=300)
log(f"  AP 相位响应 status: {phase_resp.get('status')}")

if phase_resp.get("status") != "OK":
    log(f"  ❌ 相位差测量失败: {phase_resp.get('message')}")
    sys.exit(1)

# ===== 步骤 7：解析结果 =====
log("\n【步骤 7】解析相位差数据")
max_phase = phase_resp["data"].get("max_phase_deg", phase_resp["data"].get("value", 0))
log(f"  100Hz-15kHz 范围内最大相位差: {max_phase:.2f}°")

# ===== 步骤 8：判定 =====
log("\n【步骤 8】判定 (100Hz-15kHz 内 ±10°)")
threshold = 10.0
ok = abs(max_phase) <= threshold
log(f"  |{max_phase:.2f}°| ≤ {threshold}° ? {'✅' if ok else '❌'}")

overall = "PASS" if ok else "FAIL"
log(f"\n{'='*60}")
log(f"相位差测试结果: {overall}")
log(f"{'='*60}")

log("\n⚠️ AP 保持运行中，请检查 AP GUI 上的 Phase 曲线。")
log("10 分钟后自动关闭 AP（或 Ctrl+C 提前退出）...")
tv.set_volume(100)

result = {
    "test": "phase", "channel": "AV",
    "max_phase_deg": max_phase,
    "threshold_deg": threshold,
    "overall": overall,
    "report_path": report_path,
}
print("\n" + json.dumps(result, ensure_ascii=False, indent=2))

try:
    for i in range(20):
        time.sleep(30)
        log(f"  AP 保持运行中... 剩余 {(20 - i - 1) * 30}s")
except KeyboardInterrupt:
    log("\n用户中断")
finally:
    log("正在关闭 AP...")
    try:
        ap_send({"cmd": "quit"}); time.sleep(1)
    except Exception:
        pass
    try:
        ap_proc.terminate(); ap_proc.wait(timeout=5)
    except Exception:
        try: ap_proc.kill()
        except Exception: pass
    log("AP 已关闭。")
