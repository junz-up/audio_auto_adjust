# 音频测试自动化工具集

本目录包含 TV 音频功率测试与校准的自动化脚本。

## 快速使用

### 音频自检（只测不调）
```bash
python .claude/skills/audio-self-check/main.py --channels HDMI AV DTV-DVB ATV-PAL USB --impedance 6R --power 6
# 使用非默认 DTV 预设（如 509MHz 6M 带宽）：
python .claude/skills/audio-self-check/main.py --channels DTV-DVB HDMI AV --preset 509-6m --impedance 6R --power 8
# 耳机输出自检（测 mVrms，标准 130-150mV，THD+N<10%）：
python .claude/skills/audio-self-check/main.py --channels HDMI AV ATV-PAL --output hp
```

### 音频自动校准（闭环调试）
```bash
python .claude/skills/audio-auto-calibration/main.py --channels HDMI AV DTV-DVB ATV-PAL USB --impedance 6R --power 6 --tolerance 5
# 使用非默认 DTV 预设：
python .claude/skills/audio-auto-calibration/main.py --channels DTV-DVB HDMI AV --preset 509-6m --impedance 6R --power 8
# 耳机输出校准（调整 OUTPUTHP Gain 使输出达到 130-150mV）：
python .claude/skills/audio-auto-calibration/main.py --channels HDMI AV ATV-PAL --output hp
# 多规格批量校准（连续调试多个功放规格）：
python .claude/skills/audio-auto-calibration/main.py --channels HDMI AV DTV-DVB ATV-PAL USB --specs 4R5W=ID_SOUND_xxx_12V4R5W 4R3W=ID_SOUND_xxx_12V4R3W
```

## 意图识别规则

- 用户提到"自检"、"测功率"、"仅测量"、"看看功率" → 用 `audio-self-check/main.py`
- 用户提到"自动调试"、"校准"、"调到XXW"、"Gain/DRC调参" → 用 `audio-auto-calibration/main.py`
- 用户提到"连续调试"、"批量校准"多个规格 → 用 `audio-auto-calibration/main.py --specs`，需要用户提供每个规格对应的 SOUND_TYPE ID
- 用户提到具体设备控制（TG39/DTVPlayer/AP/UIAT/TV板卡）→ 用对应 `control_*/adapter/main.py`

## 参数解析规则

从用户自然语言中提取：
- **通道**：DTV-DVB、DTV-ATSC、DTV-ISDB、ATV-PAL、ATV-N、HDMI、AV、USB
- **阻抗**：用户说"6欧"/"6R"/"6Ω" → `--impedance 6R`
- **功率**：用户说"6瓦"/"6W" → `--power 6`
- **规格简写**：用户说"6R6W" → `--impedance 6R --power 6`；"8R8W" → `--impedance 8R --power 8`
- **DTV 预设**：用户说"DTV-DVB-T 509 6M"或"DTV 509-6M" → `--channels DTV-DVB --preset 509-6m`。预设名格式为 `{频率}-{带宽}`（小写），在 config.json 的 `channels.DTV-DVB.presets` 中定义。不指定时使用 `default` 预设。
- **多规格批量**：用户说"连续调试 4R5W 和 4R3W"并给出 SOUND_TYPE ID → `--specs 4R5W=ID_SOUND_xxx 4R3W=ID_SOUND_yyy`。每个规格会先切换 SOUND_TYPE、编译升级，再开始校准。
- **耳机输出**：用户说"耳机"/"HP"/"headphone" → `--output hp`。耳机标准：130-150mVrms，THD+N<10%，只测标准输入。Gain 写入 OUTPUTHP 行。

## 单独设备控制

```bash
# TG39 RF 信号源（全局 skill）
python -m control_tg39.adapter.main play ATV-PAL --freq 48.25 --modulation 30kHz
python -m control_tg39.adapter.main stop

# DTVPlayer 码流（全局 skill）
python -m control_dtvplayer.adapter.main play DTV-DVB --freq 482 --db-level -12
python -m control_dtvplayer.adapter.main stop

# AP 音频分析仪（全局 skill，stdin 服务模式）
python -m control_ap_ad2502.adapter.main serve --impedance 6

# UIAT 电源/USB（全局 skill）
python -m control_uiat.adapter.main power on|off
python -m control_uiat.adapter.main usb tv|pc
python -m control_uiat.adapter.main status

# webOS TV 板卡（项目 skill）
python -m control_webos_wee.adapter.main switch-channel HDMI
python -m control_webos_wee.adapter.main set-volume 100
python -m control_webos_wee.adapter.main set-sound-output spk|hp
python -m control_webos_wee.adapter.main read-gain --signal HDMI
python -m control_webos_wee.adapter.main read-gain --signal HDMI --output hp
python -m control_webos_wee.adapter.main write-gain --signal HDMI --value 0x7FFF
python -m control_webos_wee.adapter.main write-gain --signal HDMI --value 0x7FFF --output hp
python -m control_webos_wee.adapter.main read-drc
python -m control_webos_wee.adapter.main write-drc --value 0x2EF
python -m control_webos_wee.adapter.main calc-drc --current-power 12 --target-power 8
python -m control_webos_wee.adapter.main backup-config
python -m control_webos_wee.adapter.main build
python -m control_webos_wee.adapter.main download --usb-path AUTO
python -m control_webos_wee.adapter.main upgrade
python -m control_webos_wee.adapter.main power on|off
```

## 输出格式

所有命令输出 JSON 到 stdout：
- 成功：`{"status": "OK", "data": {...}}`
- 失败：`{"status": "ERROR", "message": "..."}`
- 自检/校准完整结果包含 `status`、`all_passed`、`steps`、`measurements`、`report_path`

## 注意事项

- 所有测量必须在 TV 音量 100 下进行（main.py 内部会自动设置）
- DRC 计算自动检测功放类型（SY6045 步进模式 / WA153814 浮点编码模式）
- 不要手动修改等待时间参数（switch_channel_wait_s / signal_play_wait_s）
