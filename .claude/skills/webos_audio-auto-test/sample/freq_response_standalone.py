"""独立频率响应测试 — 逐步执行，AP 保持运行。

参考 DQC-HT-QE-0061 §7.3.3 频率响应(AV通道)测试方法。

执行方式：
    python .claude/skills/webos_audio-auto-test/freq_response_standalone.py
"""
from __future__ import annotations
import io, json, math, subprocess, sys, threading, time
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

SKILL_DIR = Path(__file__).resolve().parent.parent  # sample/ -> webos_audio-auto-test/
SKILLS_ROOT = SKILL_DIR.parent
GLOBAL_SKILLS = Path.home() / ".claude" / "skills"

def log(msg):
    print(msg, flush=True)

def _subprocess_env():
    import os
    env = os.environ.copy()
    paths = [str(SKILLS_ROOT), str(GLOBAL_SKILLS)]
    existing = env.get("PYTHONPATH", "")
    if existing:
        paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONIOENCODING"] = "utf-8"
    return env

# ===== 加载 config =====
config = json.loads((SKILL_DIR / "config.json").read_text(encoding="utf-8"))
impedance_str = config.get("impedance", "6R")
impedance_ohm = float(impedance_str.upper().replace("R", ""))

log("=" * 60)
log("频率响应独立测试")
log(f"阻抗: {impedance_str} ({impedance_ohm}Ω)")
log("=" * 60)

# ===== 第 0 步：初始化 TV 适配器 =====
log("\n【步骤 0】初始化 TV 适配器 (control_webos_wee)")
sys.path.insert(0, str(SKILLS_ROOT))
sys.path.insert(0, str(GLOBAL_SKILLS))
from control_webos_wee.adapter.webos import WebOSTVAdapter
cfg_path = SKILLS_ROOT / "control_webos_wee" / "adapter" / "config.json"
tv_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
tv = WebOSTVAdapter(tv_cfg)
log("  TV 适配器初始化完成")

# ===== 第 1 步：启动 AP 进程 =====
log("\n【步骤 1】启动 AP 音频分析仪 (serve 模式)")
ap_cmd = [sys.executable, "-m", "control_ap.adapter.main", "serve", "--impedance", str(impedance_ohm), "--no-relay"]
ap_proc = subprocess.Popen(
    ap_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", errors="replace", env=_subprocess_env(),
)

def ap_send(cmd_obj):
    line = json.dumps(cmd_obj, ensure_ascii=False) + "\n"
    ap_proc.stdin.write(line)
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

# 转发 AP stderr 到日志（后台线程）
def _fwd_stderr():
    try:
        for line in ap_proc.stderr:
            log(f"  [AP:stderr] {line.rstrip()}")
    except Exception:
        pass
threading.Thread(target=_fwd_stderr, daemon=True).start()

# 读 AP 初始化响应
log("  等待 AP 初始化...")
init_resp = ap_read(timeout=300)
log(f"  AP 初始化响应: {json.dumps(init_resp, ensure_ascii=False)}")
if init_resp.get("status") != "OK":
    log(f"  ⚠️ AP 初始化返回非 OK，但尝试继续执行...")

# ===== 第 2 步：设置 TV 音频输出=喇叭, 音量=100 =====
log("\n【步骤 2】设置 TV: 音频输出=喇叭, 音量=100")
tv.set_sound_output("spk")
log("  set_sound_output('spk') -> 完成")
tv.set_volume(100)
log("  set_volume(100) -> 完成")
time.sleep(2.0)  # volume_settle_s
log("  等待 2.0s 音量稳定")

# ===== 第 3 步：切源到 AV =====
log("\n【步骤 3】TV 切源到 AV")
tv.switch_channel("AV")
log("  switch_channel('AV') -> 完成")
time.sleep(5.0)  # switch_channel_wait_s
log("  等待 5.0s 切源稳定")

# ===== 第 4 步：AP 开启 AV 信号发生器 =====
log("\n【步骤 4】AP 开启信号发生器: AV (AnalogUnbalanced), 1000 Hz, 0.5 Vrms")
ap_send({"cmd": "generator", "on": True, "connector": "AV", "freq": 1000, "level": 0.5})
gen_resp = ap_read(timeout=30)
log(f"  AP generator_on 响应: {json.dumps(gen_resp, ensure_ascii=False)}")
time.sleep(10.0)  # signal_play_wait_s
log("  等待 10.0s 信号稳定")

# ===== 第 5 步：二分搜索音量 → 目标 500mW（文档 7.3.3 标准） =====
# 500mW = 0.5W → V = sqrt(0.5 * R) ≈ 1.73V (6Ω)
target_w = 0.5
log(f"\n【步骤 5】二分搜索音量，使输出收敛到 500mW ({target_w}W into {impedance_ohm}Ω, ±5%)")
log(f"  注: 500mW into {impedance_ohm}Ω → V = √(P×R) = {math.sqrt(target_w * impedance_ohm)*1000:.0f}mV")
rel_tol = 0.05
lo, hi = 1, 100
best = None
converged = False

for iteration in range(14):
    if lo > hi:
        log(f"  迭代 {iteration+1}: lo({lo}) > hi({hi})，搜索空间耗尽")
        break
    mid = (lo + hi) // 2
    log(f"  迭代 {iteration+1}: 搜索范围 [{lo}, {hi}]，尝试音量 {mid}")

    tv.set_volume(mid)
    time.sleep(2.0)  # volume_settle_s

    ap_send({"cmd": "measure"})
    m = ap_read(timeout=30)
    if m.get("status") != "OK":
        log(f"    AP 测量失败: {m.get('message')}")
        break

    l_w = float(m["data"].get("L", {}).get("power", 0))
    r_w = float(m["data"].get("R", {}).get("power", 0))
    avg = (l_w + r_w) / 2.0
    # 换算 mV（V = sqrt(P * R)）
    l_mv = math.sqrt(l_w * impedance_ohm) * 1000.0
    r_mv = math.sqrt(r_w * impedance_ohm) * 1000.0
    avg_mv = math.sqrt(avg * impedance_ohm) * 1000.0
    best = {"volume": mid, "l_w": l_w, "r_w": r_w, "avg": avg}

    log(f"    L={l_w:.6f}W ({l_mv:.1f}mV)  R={r_w:.6f}W ({r_mv:.1f}mV)  avg={avg:.6f}W ({avg_mv:.1f}mV)  目标={target_w}W  偏差={abs(avg - target_w):.6f}W (容限={rel_tol * target_w:.4f}W)")

    if abs(avg - target_w) <= rel_tol * target_w:
        log(f"    ✅ 收敛！音量={mid}，avg={avg:.6f}W")
        converged = True
        break

    if avg < target_w:
        log(f"    avg < target → lo = {mid} + 1 = {mid + 1}")
        lo = mid + 1
    else:
        log(f"    avg > target → hi = {mid} - 1 = {mid - 1}")
        hi = mid - 1

if not converged and best:
    log(f"  ⚠️ 未收敛，使用最接近结果: 音量={best['volume']}, avg={best['avg']:.6f}W")
    # 继续执行（与 main.py 行为一致）

log(f"  最终音量: {best['volume'] if best else 'N/A'}")

# ===== 计算 dBr 参考电压 =====
if best:
    avg_power_w = best["avg"]
    dbr_ref_vrms = math.sqrt(avg_power_w * impedance_ohm)
    log(f"  dBr 参考电压: √({avg_power_w:.6f}W × {impedance_ohm}Ω) = {dbr_ref_vrms:.6f} Vrms")
else:
    dbr_ref_vrms = None
    log("  ⚠️ 无有效测量数据，不设置 dBr 参考")

# ===== 第 6 步：发送扫频命令（含 dBr 清零） =====
log("\n【步骤 6】发送扫频命令到 AP（含 dBrA 参考清零）")
# 生成报告保存路径
import datetime
output_dir = SKILL_DIR / "output" / "reports"
output_dir.mkdir(parents=True, exist_ok=True)
timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
report_filename = f"FreqResponse_{timestamp}.pdf"
report_path = str(output_dir / report_filename)

sweep_params = {
    "cmd": "measure_freq_response",
    "freq_start": 20,
    "freq_stop": 20000,
    "num_points": 100,
    "level": 0.5,
    "level_unit": "Vrms",
    "dbr_reference_vrms": round(dbr_ref_vrms, 6) if dbr_ref_vrms else None,
    "report_path": report_path,
}
log(f"  参数: {json.dumps(sweep_params, ensure_ascii=False)}")
log(f"  扫频范围: 20 Hz ~ 20000 Hz, 100 点, 0.5 Vrms")
log(f"  dBrA 参考: {dbr_ref_vrms:.4f} Vrms → 1kHz 处将显示为 ~0dBr" if dbr_ref_vrms else "  dBrA 参考: 未设置")
log(f"  ±3dB 限制线: 将显示在曲线上")
log(f"  报告保存: {report_path}")
log(f"  预计等待: max(5.0, 100*0.1) = 10.0 秒")

ap_send(sweep_params)
log("  等待 AP 扫频完成...")
fr_resp = ap_read(timeout=300)
log(f"  AP 扫频响应 status: {fr_resp.get('status')}")

if fr_resp.get("status") != "OK":
    log(f"  ❌ 扫频失败: {fr_resp.get('message')}")
    log("\nAP 未关闭，可手动检查。")
    sys.exit(1)

# ===== 第 7 步：解析结果 =====
log("\n【步骤 7】解析扫频数据")
l_data = fr_resp["data"]["L"]
r_data = fr_resp["data"]["R"]
l_low = l_data["freq_low"]
l_high = l_data["freq_high"]
r_low = r_data["freq_low"]
r_high = r_data["freq_high"]

log(f"  L 通道 -3dB 带宽: {l_low:.1f} Hz ~ {l_high:.1f} Hz")
log(f"  R 通道 -3dB 带宽: {r_low:.1f} Hz ~ {r_high:.1f} Hz")

# 如果有 graph_points 数据，打印关键频点
if "graph_points" in l_data:
    log("\n  L 通道关键频点 (部分):")
    pts = l_data["graph_points"]
    for p in pts[:5]:
        log(f"    {p['freq']:.1f} Hz → {p['level']:.2f} dBV")
    if len(pts) > 10:
        log(f"    ... (共 {len(pts)} 点)")
    for p in pts[-3:]:
        log(f"    {p['freq']:.1f} Hz → {p['level']:.2f} dBV")

# ===== 第 8 步：判定 =====
log("\n【步骤 8】判定通过/失败")
low_limit = float(config.get("speaker_index", {}).get("freq_response_low_limit_hz", 100))
high_limit = float(config.get("speaker_index", {}).get("freq_response_high_limit_hz", 15000))

log(f"  标准: freq_low ≤ {low_limit:.0f} Hz 且 freq_high ≥ {high_limit:.0f} Hz")

l_ok = l_low <= low_limit and l_high >= high_limit
r_ok = r_low <= low_limit and r_high >= high_limit

log(f"  L 通道: {l_low:.0f} ≤ {low_limit:.0f} ? {'✅' if l_low <= low_limit else '❌'}  |  {l_high:.0f} ≥ {high_limit:.0f} ? {'✅' if l_high >= high_limit else '❌'}  → {'PASS' if l_ok else 'FAIL'}")
log(f"  R 通道: {r_low:.0f} ≤ {low_limit:.0f} ? {'✅' if r_low <= low_limit else '❌'}  |  {r_high:.0f} ≥ {high_limit:.0f} ? {'✅' if r_high >= high_limit else '❌'}  → {'PASS' if r_ok else 'FAIL'}")

overall = "PASS" if (l_ok and r_ok) else "FAIL"
log(f"\n{'='*60}")
log(f"频率响应测试结果: {overall}")
log(f"{'='*60}")

# ===== AP 保持运行 10 分钟，等待用户检查 =====
log("\n⚠️ AP 保持运行中，请在 AP GUI 检查设置和曲线。")
log("10 分钟后自动关闭 AP（或 Ctrl+C 提前退出）...")

# 恢复 TV 音量到 100
tv.set_volume(100)
log("TV 音量已恢复到 100")

# 汇总 JSON
result = {
    "test": "frequency_response",
    "channel": "AV",
    "volume_at_sweep": best["volume"] if best else None,
    "converged": converged,
    "L": {"freq_low": round(l_low, 1), "freq_high": round(l_high, 1), "passed": l_ok},
    "R": {"freq_low": round(r_low, 1), "freq_high": round(r_high, 1), "passed": r_ok},
    "overall": overall,
    "report_path": report_path,
    "ap_status": "running (保持 10 分钟)",
}
print("\n" + json.dumps(result, ensure_ascii=False, indent=2))

# 保持运行，每 30 秒打印一次状态
try:
    for i in range(20):  # 20 × 30s = 600s = 10 分钟
        time.sleep(30)
        log(f"  AP 保持运行中... 剩余 {(20 - i - 1) * 30}s")
except KeyboardInterrupt:
    log("\n用户中断，正在关闭 AP...")
finally:
    log("正在关闭 AP...")
    try:
        ap_send({"cmd": "quit"})
        time.sleep(1)
    except Exception:
        pass
    try:
        ap_proc.terminate()
        ap_proc.wait(timeout=5)
    except Exception:
        try:
            ap_proc.kill()
        except Exception:
            pass
    log("AP 已关闭。")
