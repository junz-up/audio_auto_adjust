"""control_webos_wee CLI 入口：控制 webOS TV 板卡。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_config() -> dict:
    cfg_path = Path(__file__).resolve().parent / "config.json"
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def _ok(data=None):
    r = {"status": "OK"}
    if data is not None:
        r["data"] = data
    print(json.dumps(r, ensure_ascii=False))


def _error(msg: str):
    print(json.dumps({"status": "ERROR", "message": msg}, ensure_ascii=False))


def _get_adapter():
    from .webos import WebOSTVAdapter
    cfg = _load_config()
    return WebOSTVAdapter(cfg)


def cmd_switch_channel(args):
    tv = _get_adapter()
    try:
        test_case = {}
        if args.case:
            test_case = json.loads(args.case)
        ok = tv.switch_channel(args.channel, test_case)
        if ok:
            _ok()
        else:
            _error(f"切源 {args.channel} 失败")
    except Exception as e:
        _error(str(e))


def cmd_set_volume(args):
    tv = _get_adapter()
    try:
        tv.set_volume(args.level)
        _ok()
    except Exception as e:
        _error(str(e))


def cmd_set_sound_output(args):
    tv = _get_adapter()
    try:
        ok = tv.set_sound_output(args.output)
        if ok:
            _ok()
        else:
            _error(f"set_sound_output {args.output} 失败")
    except Exception as e:
        _error(str(e))


def cmd_read_gain(args):
    tv = _get_adapter()
    try:
        value = tv.read_gain_value(args.channel or "L", args.signal, output=getattr(args, "output", "spk") or "spk")
        _ok({"raw": value, "hex": f"0x{value:04X}"})
    except Exception as e:
        _error(str(e))


def cmd_write_gain(args):
    tv = _get_adapter()
    try:
        value = int(args.value, 16) if args.value.startswith("0x") else int(args.value)
        ok = tv.modify_gain_value(args.channel or "L", args.signal, value, output=getattr(args, "output", "spk") or "spk")
        if ok:
            _ok()
        else:
            _error("modify_gain_value 失败")
    except Exception as e:
        _error(str(e))


def cmd_read_drc(args):
    tv = _get_adapter()
    try:
        value = tv.read_drc_value()
        _ok({"raw": value, "hex": f"0x{value:08X}" if value > 0x3FF else f"0x{value:03X}"})
    except Exception as e:
        _error(str(e))


def cmd_write_drc(args):
    tv = _get_adapter()
    try:
        value = int(args.value, 16) if args.value.startswith("0x") else int(args.value)
        ok = tv.modify_drc_value(value)
        if ok:
            _ok()
        else:
            _error("modify_drc_value 失败")
    except Exception as e:
        _error(str(e))


def cmd_calc_drc(args):
    tv = _get_adapter()
    try:
        current_drc = tv.read_drc_value()
        new_drc, delta_db = tv.calculate_drc_offset(args.current_power, args.target_power, current_drc)
        _ok({
            "current_drc": f"0x{current_drc:08X}" if current_drc > 0x3FF else f"0x{current_drc:03X}",
            "new_drc": f"0x{new_drc:08X}" if new_drc > 0x3FF else f"0x{new_drc:03X}",
            "new_drc_raw": new_drc,
            "delta_db": round(delta_db, 4),
        })
    except Exception as e:
        _error(str(e))


def cmd_backup_config(args):
    tv = _get_adapter()
    try:
        path = tv.backup_config()
        _ok({"backup_path": path})
    except Exception as e:
        _error(str(e))


def cmd_restore_config(args):
    tv = _get_adapter()
    try:
        tv.restore_config(args.backup_path)
        _ok()
    except Exception as e:
        _error(str(e))


def cmd_set_sound_type(args):
    tv = _get_adapter()
    try:
        path = tv.set_sound_type(args.sound_type)
        _ok({"file": path, "sound_type": args.sound_type})
    except Exception as e:
        _error(str(e))


def cmd_build(args):
    tv = _get_adapter()
    try:
        ok = tv.compile_and_build()
        if ok:
            _ok()
        else:
            _error("编译失败")
    except Exception as e:
        _error(str(e))


def cmd_download(args):
    tv = _get_adapter()
    try:
        ok = tv.download_upgrade_package(args.usb_path)
        if ok:
            _ok()
        else:
            _error("下载升级包失败")
    except Exception as e:
        _error(str(e))


def cmd_upgrade(args):
    tv = _get_adapter()
    try:
        ok = tv.trigger_upgrade()
        if ok:
            _ok()
        else:
            _error("升级触发失败")
    except Exception as e:
        _error(str(e))


def cmd_power(args):
    tv = _get_adapter()
    try:
        if args.state == "on":
            ok = tv.power_on()
        else:
            ok = tv.power_off()
        if ok:
            _ok()
        else:
            _error(f"power {args.state} 失败")
    except Exception as e:
        _error(str(e))


def main():
    parser = argparse.ArgumentParser(description="webOS TV 板卡控制")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("switch-channel", help="切换 TV 输入源")
    p.add_argument("channel", help="通道名: HDMI, AV, DTV-DVB, ATV-PAL, USB 等")
    p.add_argument("--case", type=str, help="test_case JSON 字符串")
    p.set_defaults(func=cmd_switch_channel)

    p = sub.add_parser("set-volume", help="设置音量")
    p.add_argument("level", type=int, help="音量值 (0-100)")
    p.set_defaults(func=cmd_set_volume)

    p = sub.add_parser("set-sound-output", help="切换音频输出 (spk/hp)")
    p.add_argument("output", choices=["spk", "hp"], help="音频输出: spk=喇叭, hp=耳机")
    p.set_defaults(func=cmd_set_sound_output)

    p = sub.add_parser("read-gain", help="读取 Gain 值")
    p.add_argument("--signal", required=True, help="信号类型: HDMI, AV, DTV-DVB 等")
    p.add_argument("--channel", default="L", help="声道 L/R (默认 L)")
    p.add_argument("--output", default="spk", choices=["spk", "hp"], help="音频输出: spk=喇叭, hp=耳机 (默认 spk)")
    p.set_defaults(func=cmd_read_gain)

    p = sub.add_parser("write-gain", help="写入 Gain 值")
    p.add_argument("--signal", required=True, help="信号类型")
    p.add_argument("--value", required=True, help="Gain 原始值 (如 0x7FFF 或 32767)")
    p.add_argument("--channel", default="L", help="声道 L/R (默认 L)")
    p.add_argument("--output", default="spk", choices=["spk", "hp"], help="音频输出: spk=喇叭, hp=耳机 (默认 spk)")
    p.set_defaults(func=cmd_write_gain)

    p = sub.add_parser("read-drc", help="读取 DRC 值")
    p.set_defaults(func=cmd_read_drc)

    p = sub.add_parser("write-drc", help="写入 DRC 值")
    p.add_argument("--value", required=True, help="DRC 原始值 (如 0x2EF 或 0x11000001)")
    p.set_defaults(func=cmd_write_drc)

    p = sub.add_parser("calc-drc", help="计算 DRC 偏移（自动检测功放类型）")
    p.add_argument("--current-power", type=float, required=True, help="当前实测功率 (W)")
    p.add_argument("--target-power", type=float, required=True, help="目标功率 (W)")
    p.set_defaults(func=cmd_calc_drc)

    p = sub.add_parser("backup-config", help="备份音频配置")
    p.set_defaults(func=cmd_backup_config)

    p = sub.add_parser("restore-config", help="从备份恢复音频配置")
    p.add_argument("--backup-path", required=True, help="备份文件远程路径")
    p.set_defaults(func=cmd_restore_config)

    p = sub.add_parser("set-sound-type", help="修改 CVT_DEF_SOUND_TYPE")
    p.add_argument("sound_type", help="新的 sound type ID")
    p.set_defaults(func=cmd_set_sound_type)

    p = sub.add_parser("build", help="编译 webOS 代码")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("download", help="下载升级包到 U 盘")
    p.add_argument("--usb-path", default="AUTO", help="U 盘路径或 AUTO")
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("upgrade", help="触发 TV PAK 升级")
    p.set_defaults(func=cmd_upgrade)

    p = sub.add_parser("power", help="电源开关")
    p.add_argument("state", choices=["on", "off"])
    p.set_defaults(func=cmd_power)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
