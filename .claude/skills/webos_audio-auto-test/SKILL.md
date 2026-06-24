---
name: audio-auto-test
description: 测试部自动测试，按公司模板 QR-DQC-HT-202 音频性能测试报告填写结果。支持中高音喇叭性能测试（音量100多通道）、中高音喇叭音量曲线（AV/ATV/HDMI 音量5-100扫点）、耳机性能测试（AV/ATV/HDMI 音量100）、耳机音量曲线（AV 音量5-100）。Use when the user asks for "自动测试", "填写测试报告", "音频性能测试", "音量曲线测试", or QR-DQC-HT-202 report auto-fill.
---

# audio-auto-test

测试部自动测试 skill。基于公司 Excel 模板 `QR-DQC-HT-202 音频性能测试报告(衍生)V5.0.xlsm`，
自动编排 AP/TG39/DTVPlayer/TV 板卡完成测量，并把功率、THD+N、音量曲线等结果按单元格映射填入报告，
判定列自动写 PASS/FAIL。

## 调用方式

```bash
python .claude/skills/audio-report-test/main.py all
python .claude/skills/audio-report-test/main.py speaker-performance
python .claude/skills/audio-report-test/main.py speaker-curve
python .claude/skills/audio-report-test/main.py headphone-performance
python .claude/skills/audio-report-test/main.py headphone-curve
python .claude/skills/audio-report-test/main.py speaker-performance --impedance 6R --power 10
```

### 子命令

| 子命令 | 说明 |
|--------|------|
| `all` | 全套（喇叭性能 + 喇叭曲线 + 耳机性能 + 耳机曲线） |
| `speaker-performance` | 中高音喇叭性能：多通道，音量=100，每通道标准/最大输入功率+THD+N |
| `speaker-curve` | 中高音喇叭音量曲线：AV/ATV/HDMI，音量 5..100 step 5 扫点 |
| `headphone-performance` | 耳机性能：AV/ATV/HDMI，音量=100，标准/最大输入 Vrms+THD+N |
| `headphone-curve` | 耳机音量曲线：AV，音量 5..100 step 5 扫点 |

### 参数

| 参数 | 说明 |
|------|------|
| `--impedance` | 阻抗，覆盖 config（如 `6R`、`8R`） |
| `--power` | 额定功率(W)，覆盖 config（如 `6`、`10`） |

## 测试项与 Excel 映射

### 中高音喇叭性能 (sheet: 音频性能测试（中高音喇叭）)

每通道 4 行，K/L 填测试值，M 填判定：

| 通道 | 起始行 | 信号 |
|------|--------|------|
| AV | 4 | 500mVrms/2Vrms 1kHz (AP HDMI/AV) |
| ATV-PAL | 30 | FM 27kHz / FM 100kHz (TG39) |
| DTV-DVB | 34 | -12dBFs / 0dBFs 1kHz (DTVPlayer) |
| HDMI | 38 | -12dBFs / 0dBFs 1kHz (AP HDMI) |
| USB | 42 | -12dBFs / 0dBFs 1kHz |

判定：功率 额定±10%；THD+N<10%。

### 喇叭音量曲线 (sheets: 中高音喇叭音量曲线（AV/ATV/HDMI）)

C 列 Volume (5,10,...,100)，D/E 列 Left/Right 功率(W)，F 列 dB 公式自动计算。

### 耳机性能 (sheet: 音频性能测试（耳机）)

切换 AP 到电压模式，TV 输出切到耳机。每通道 4 行：

| 通道 | 起始行 | 信号 |
|------|--------|------|
| AV | 4 | 500mVrms/2Vrms 1kHz |
| ATV-PAL | 22 | FM 27kHz / FM 100kHz |
| HDMI | 26 | -12dBFs / 0dBFs 1kHz |

判定：Vrms 130-150 mV；THD+N<10%。

### 耳机音量曲线 (sheet: 耳机音量曲线)

AV 通道，音量 5..100 step 5，D/E 列填 Vrms (mV)。

## 输出

- 报告路径：`.claude/skills/audio-auto-test/output/reports/AudioTestReport_<os>_<spec>_<ts>.xlsm`
- stdout JSON：`{"status": "OK"|"ERROR", "report_path": "...", "speaker_performance": [...], "speaker_curve": [...], "headphone_performance": [...], "headphone_curve": [...]}`

## 报告模板

- 模板文件：`.claude/skills/audio-auto-test/template.xlsm`（公司 QR-DQC-HT-202 模板副本）
- `config.json.report_template` 若是相对路径，自动解析到 skill 目录；绝对路径直接用
- 如模板有更新（测试部发新版），用新模板覆盖 `template.xlsm` 即可

## 约束

- 除音量曲线外，所有测试都在音量=100 下进行（main.py 内部自动设置）。
- 音量曲线扫点前会把音量恢复为 100 再切源。
- 测试结束会恢复 TV 输出到喇叭（set-sound-output spk）。

## 触发场景

用户说以下内容时使用本 skill：

- "自动测试"、"测试部测试"、"按报告模板测试"
- "填写测试报告"、"QR-DQC-HT-202"
- "喇叭性能测试"、"中高音喇叭测试"
- "音量曲线"、"喇叭音量曲线"
- "耳机性能测试"、"耳机音量曲线"
