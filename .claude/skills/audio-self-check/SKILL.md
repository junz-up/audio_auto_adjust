---
name: audio-self-check
description: Read-only TV speaker audio power self-check workflow that measures per-channel power and THD+N against a target spec (e.g. 6R/6W) and reports PASS/FAIL without modifying any Gain/DRC, without compiling webOS and without triggering any firmware upgrade. Use when the user asks for "自检 音频功率", "仅测量 6R6W 功率", power/THD verification on DTV-DVB / ATV-PAL / HDMI / AV / USB without changing parameters.
---

# audio-self-check

电视扬声器音频功率 **仅测量** 自检。只测不调。

## 调用方式

```bash
python .claude/skills/audio-self-check/main.py --channels HDMI AV DTV-DVB ATV-PAL USB --impedance 6R --power 6
python .claude/skills/audio-self-check/main.py --channels HDMI AV --impedance 8R --power 8
python .claude/skills/audio-self-check/main.py  # 使用 config.json 默认配置
```

### 参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `--channels` | 要测的通道（空格分隔） | `HDMI AV DTV-DVB ATV-PAL USB` |
| `--impedance` | 阻抗 | `6R`、`8R`、`6` |
| `--power` | 额定功率 (W) | `6`、`8`、`10` |

可用通道：`DTV-DVB`、`DTV-ATSC`、`DTV-ISDB`、`ATV-PAL`、`ATV-N`、`HDMI`、`AV`、`USB`

## 触发场景

用户说以下内容时使用本 skill：
- "自检音频功率"、"测一下功率"、"仅测量 6R6W"
- "看看 HDMI/AV 通道功率是否达标"
- "测功率，不调整"

**不应触发**：涉及"调试"、"校准"、Gain/DRC 修改时用 `audio-auto-calibration`。

## 输出格式

JSON 输出到 stdout：

```json
{
  "status": "SUCCESS",
  "message": "全部通过",
  "all_passed": true,
  "steps": [
    "HDMI/standard: PASS avg=6.050W thd=0.800%",
    "HDMI/max_input: PASS avg=6.100W thd=0.900%"
  ],
  "measurements": [
    {"channel": "HDMI", "case": "standard", "summary": {"avg_power_w": 6.05, "passed": true, ...}}
  ]
}
```

`status` 为 `"SUCCESS"` 或 `"FAILED"`。退出码：0=全部通过，1=存在未达标项。

## 结果解读与后续建议

- 全部 PASS → 告知用户当前参数达标
- 存在 FAIL → 报告具体哪个通道/用例未达标，建议用 `audio-auto-calibration` 自动调试
