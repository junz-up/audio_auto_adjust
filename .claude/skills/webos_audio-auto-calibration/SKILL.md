---
name: audio-auto-calibration
description: Closed-loop TV speaker audio power auto-calibration workflow - first validates max-input DRC headroom, then iteratively tunes per-channel Gain against a target power spec (e.g. 6R/6W), compiles webOS and triggers PAK upgrade on each parameter change, and finally re-validates max input. Use when the user asks for "自动调试 音频功率", "自动校准 6R6W 功率规格", Gain/DRC 闭环调参, or requests calculation of target Gain/DRC values for a given current power + target power.
---

# audio-auto-calibration

电视扬声器音频功率 **自动调试**。闭环调 Gain/DRC 直到达标。

## 调用方式

```bash
python audio-auto-calibration/main.py --channels HDMI AV DTV-DVB ATV-PAL USB --impedance 6R --power 6 --tolerance 5
python audio-auto-calibration/main.py --channels HDMI AV --impedance 8R --power 8
python audio-auto-calibration/main.py  # 使用 config.json 默认配置
```

### 参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `--channels` | 要调试的通道（空格分隔） | `HDMI AV DTV-DVB ATV-PAL USB` |
| `--impedance` | 阻抗 | `6R`、`8R` |
| `--power` | 额定功率 (W) | `6`、`8`、`10` |
| `--tolerance` | 功率容差百分比 | `5`（即 ±5%） |

可用通道：`DTV-DVB`、`DTV-ATSC`、`DTV-ISDB`、`ATV-PAL`、`ATV-N`、`HDMI`、`AV`、`USB`

## 触发场景

用户说以下内容时使用本 skill：
- "自动调试音频功率"、"自动校准 6R6W"、"调到 8R8W"
- "Gain/DRC 闭环调参"
- "自动调试 webOS 平台 DTV-DVB / HDMI / AV 通道 6R6W 功率规格"

**不应触发**：仅测量/自检时用 `audio-self-check`。

## 输出格式

JSON 输出到 stdout：

```json
{
  "status": "SUCCESS",
  "message": "自动校准完成，全部通过",
  "all_passed": true,
  "backup_path": "/path/to/sound_xxx.c.backup_20260514_100000",
  "steps": [
    "TV 音量已设为 100",
    "配置已备份: ...",
    "DRC预检 R1: HDMI/max avg=7.2W (需>=7.2W)",
    "Gain R1: HDMI/std avg=5.8W -> ADJUST",
    "Gain HDMI: 0x7F00 -> 0x7F30 (delta=+0.26dB)",
    "webOS 已编译",
    "验收: HDMI/standard avg=6.02W -> PASS"
  ],
  "measurements": [...]
}
```

`status` 为 `"SUCCESS"`、`"FAILED"` 或 `"ERROR"`。退出码：0=成功，1=失败。

## 结果解读与后续建议

- SUCCESS → 告知用户校准完成，所有通道达标
- FAILED → 报告哪些通道未达标，可能需要检查硬件或手动调试
- ERROR → 报告具体错误（编译失败 / 升级失败 / 设备不可达等）
