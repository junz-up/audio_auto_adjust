---
name: control_webos_wee
description: Drives the LG webOS (WEE) TV main board for source switching, gain/DRC read & write in datac sound config, USB key sequence routing, plus webOS Docker build, USB package download and PAK upgrade triggering. Use when the user mentions webOS, WEE, "TV板卡切源", switch TV input to HDMI/AV/DTV/ATV/USB, read/modify gain or DRC value, "编译webOS", build & download to U盘, "升级PAK", trigger TV firmware upgrade, or backup audio config datac file.
---

# control_webos_wee

控制 webOS TV 板卡（切源、音频参数、编译升级）。

## 调用方式

```bash
# 切源
python control_webos_wee/adapter/main.py switch-channel HDMI
python control_webos_wee/adapter/main.py switch-channel DTV-DVB
python control_webos_wee/adapter/main.py switch-channel ATV-PAL

# 音量
python control_webos_wee/adapter/main.py set-volume 100

# Gain 读写
python control_webos_wee/adapter/main.py read-gain --signal HDMI
python control_webos_wee/adapter/main.py write-gain --signal HDMI --value 0x7FFF

# DRC 读写与计算（自动检测 SY6045/WA153814 功放类型）
python control_webos_wee/adapter/main.py read-drc
python control_webos_wee/adapter/main.py write-drc --value 0x2EF
python control_webos_wee/adapter/main.py calc-drc --current-power 12 --target-power 8

# 编译升级
python control_webos_wee/adapter/main.py backup-config
python control_webos_wee/adapter/main.py build
python control_webos_wee/adapter/main.py download --usb-path AUTO
python control_webos_wee/adapter/main.py upgrade

# 电源
python control_webos_wee/adapter/main.py power on
python control_webos_wee/adapter/main.py power off
```

所有命令输出统一 JSON：`{"status": "OK", "data": {...}}` 或 `{"status": "ERROR", "message": "..."}`

### calc-drc 输出示例

```json
{"status": "OK", "data": {"current_drc": "0x11000001", "new_drc": "0x105105EC", "new_drc_raw": 274334188, "delta_db": 1.7609}}
```

## 触发场景

- "切源到 HDMI"、"TV 切 ATV-PAL"
- "读取 Gain 值"、"修改 DRC"
- "编译 webOS"、"升级 PAK"、"备份配置"

**不应触发**：AP/TG39/DTVPlayer 控制（各有专用 skill）。
