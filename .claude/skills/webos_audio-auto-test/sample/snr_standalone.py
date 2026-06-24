"""独立信噪比 (SNR) 测试 — dBrA 参考法（DQC-HT-QE-0061 §7.3.1）。

流程：
  1. TV 设置：喇叭输出，切源 AV，AP 输出 500mVrms 1kHz
  2. Level and Gain 下二分搜索音量到 500mW
  3. 调用 AP measure_snr_dbr（Signal Path Setup 下 Set A 清零 + Elliptic 滤波 + 读 RMS dBrA）
  4. SNR = signal_dBrA - noise_dBrA

执行：python .claude/skills/webos_audio-auto-test/sample/snr_standalone.py
"""
from __future__ import annotations
import io, json, subprocess, sys, threading, time
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
log("信噪比 (SNR) 独立测试 — dBrA 参考法")
log(f"阻抗: {impedance_str} ({impedance_ohm}Ω)")
log("=" * 60)

# ===== 初始化 =====
log("\n【初始化】TV + AP")
sys.path.insert(0, str(SKILLS_ROOT))
sys.path.insert(0, str(Path.home() / ".claude" / "skills"))
from control_webos_wee.adapter.webos import WebOSTVAdapter
tv_cfg = json.loads((SKILLS_ROOT / "control_webos_wee" / "adapter" / "config.json").read_text(encoding="utf-8"))
tv = WebOSTVAdapter(tv_cfg)

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
            log(f"  [AP] {line.rstrip()}")
    except Exception:
        pass
threading.Thread(target=_fwd_stderr, daemon=True).start()

init_resp = ap_read(timeout=300)
log(f"  AP: {init_resp.get('message', '')}")

# ===== 步骤 1：TV 设置 + 切源 AV + AP 输出 =====
log("\n【步骤 1】TV: 喇叭, 音量=100, AV 通道 | AP: 500mVrms 1kHz")
tv.set_sound_output("spk")
tv.set_volume(100)
tv.switch_channel("AV")
time.sleep(5.0)

ap_send({"cmd": "generator", "on": True, "connector": "AV", "freq": 1000, "level": 0.5})
ap_read(timeout=30)
time.sleep(10.0)
log("  信号已稳定")

# ===== 步骤 2：Level and Gain 下二分搜索到 500mW =====
log("\n【步骤 2】Level and Gain: 参考单位 W，调音量到 500mW")
log("  → AP GUI: Level and Gain 界面")
ap_send({"cmd": "show_measurement", "signal_path": "Signal Path1", "measurement": "Level and Gain"})
try:
    ap_read(timeout=10)
except Exception:
    pass
time.sleep(2.0)

lo, hi = 1, 100
best = None
converged = False
for iteration in range(14):
    if lo > hi:
        break
    mid = (lo + hi) // 2
    tv.set_volume(mid)
    time.sleep(2.0)
    ap_send({"cmd": "measure"})
    m = ap_read(timeout=30)
    if m.get("status") != "OK":
        break
    l_w = float(m["data"].get("L", {}).get("power", 0))
    r_w = float(m["data"].get("R", {}).get("power", 0))
    avg = (l_w + r_w) / 2.0
    best = {"volume": mid, "avg": avg}
    log(f"  音量 {mid}: L={l_w:.4f}W R={r_w:.4f}W avg={avg:.4f}W")
    if abs(avg - 0.5) <= 0.025:
        log(f"  ✅ 收敛！音量={mid}, 功率≈500mW")
        converged = True
        break
    if avg < 0.5:
        lo = mid + 1
    else:
        hi = mid - 1

if not converged and best:
    log(f"  ⚠️ 未完全收敛，音量={best['volume']}")

# ===== 步骤 3：dBrA 参考法 SNR 测量 =====
log("\n【步骤 3】Signal Path Setup: Set A 清零 + Elliptic 滤波 + 读 RMS dBrA")
ap_send({"cmd": "measure_snr_dbr", "settle_s": 3.0})
snr_resp = ap_read(timeout=60)

if snr_resp.get("status") == "OK":
    data = snr_resp["data"]
    l_snr = data["L"]
    r_snr = data["R"]
    log(f"  信号 dBrA: L={data.get('signal_dBrA_L', 0):.2f}, R={data.get('signal_dBrA_R', 0):.2f}")
    log(f"  噪声 dBrA: L={data.get('noise_dBrA_L', 0):.2f}, R={data.get('noise_dBrA_R', 0):.2f}")
    log(f"  SNR: L={l_snr:.2f}dB, R={r_snr:.2f}dB")
else:
    l_snr = r_snr = 0
    log(f"  ⚠️ SNR 测量失败: {snr_resp.get('message', '')}")

# ===== 判定 =====
threshold = 60.0
l_ok = l_snr >= threshold
r_ok = r_snr >= threshold
log(f"\n{'='*60}")
log(f"SNR 判定 (阈值 ≥ {threshold}dB):")
log(f"  L: {l_snr:.2f} ≥ {threshold} ? {'✅' if l_ok else '❌'}")
log(f"  R: {r_snr:.2f} ≥ {threshold} ? {'✅' if r_ok else '❌'}")
overall = "PASS" if (l_ok and r_ok) else "FAIL"
log(f"\n信噪比测试结果: {overall}")
log(f"{'='*60}")

tv.set_volume(100)

result = {
    "test": "snr", "channel": "AV",
    "volume": best["volume"] if best else None,
    "converged": converged,
    "L_snr_db": round(l_snr, 2), "R_snr_db": round(r_snr, 2),
    "overall": overall,
}
print("\n" + json.dumps(result, ensure_ascii=False, indent=2))

# ===== 保持 AP 进程供观察 =====
log("\n⚠️ AP 保持在 Signal Path Setup 界面。10 分钟后自动关闭...")
try:
    for i in range(20):
        time.sleep(30)
        log(f"  AP 保持中... 剩余 {(20-i-1)*30}s")
except KeyboardInterrupt:
    pass
finally:
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
