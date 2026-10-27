#!/usr/bin/env python3
"""
Raspberry Pi を LINE Simple Beacon として発信するスクリプト。

BlueZ の D-Bus 広告API (LEAdvertisingManager1) を使うので、
非推奨の hcitool は不要です。Raspberry Pi OS / Debian 標準の
python3-dbus と python3-gi で動きます。

使い方:
    sudo python3 line_beacon.py --hwid 0123456789
    sudo python3 line_beacon.py --hwid 0123456789 --dm 01   # Device Message を指定
    sudo python3 line_beacon.py --hwid 0123456789 --no-ibeacon  # LINE 用 iBeacon を出さない（比較実験用）
    sudo python3 line_beacon.py --hwid 0123456789 --major 1 --minor 2  # LabHub iBeacon の部屋番号を変える

発信する広告は3種類です。
  1. LINE Simple Beacon フレーム
     仕様: https://github.com/line/line-simple-beacon
       - Service UUID 0xFE6F
       - Frame: FrameType(0x02) + HWID(5byte) + MeasuredTxPower(0x7F) + DeviceMessage(1-13byte)
       - Device Message を空にするとイベントが通知されないため、未使用時は 0x00 を送る
  2. iBeacon フレーム（iPhone のバックグラウンド検知用）
     LINE Beacon 端末の仕様で送信が必須とされている iBeacon。
     iOS のアプリはバックグラウンドでは iBeacon しか検知できないため、
     これがないと画面ロック中の iPhone で LINE が起動されない。
     仕様: https://developers.line.biz/ja/docs/messaging-api/beacon-device-spec/
       - UUID  D0D2CE24-9EFC-11E5-82C4-1C6A7A17EF38
       - Major 0x4C49 / Minor 0x4E45
  3. LabHub 専用の iBeacon フレーム（自作 iPhone アプリの入退室検知用）
       - UUID  LABHUB_UUID（LabHub 独自。iOS アプリの Config と同じ値にする）
       - Major = 研究室番号 / Minor = 部屋番号
     LINE 用の iBeacon を自作アプリで監視すると、学外の LINE Beacon にも反応してしまうため分けている。

BlueZ は複数の広告を登録すると、コントローラが対応していなくても
一定間隔（既定 2 秒）で交互に発信します。
"""

import argparse
import functools
import signal
import sys
import uuid

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

BLUEZ = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
ADV_MGR_IFACE = "org.bluez.LEAdvertisingManager1"
ADV_IFACE = "org.bluez.LEAdvertisement1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"

# LINE Simple Beacon
LINE_SERVICE_UUID = "fe6f"
FRAME_TYPE = 0x02
MEASURED_TX_POWER = 0x7F

# iBeacon（LINE Beacon 端末仕様で指定された値）
APPLE_COMPANY_ID = 0x004C
IBEACON_UUID = "D0D2CE24-9EFC-11E5-82C4-1C6A7A17EF38"
IBEACON_MAJOR = 0x4C49  # "LI"
IBEACON_MINOR = 0x4E45  # "NE"
IBEACON_MEASURED_POWER = -59  # 1m 地点の想定 RSSI(dBm)。一般的な値

# LabHub 専用 iBeacon（iOS アプリの Config.beaconUUID と一致させる）
LABHUB_UUID = "C4A814CF-5E48-4D16-A652-0F391BD74E05"


def build_frame(hwid_hex: str, dm_hex: str) -> bytes:
    """LINE Simple Beacon Frame を組み立てる。"""
    try:
        hwid = bytes.fromhex(hwid_hex)
        dm = bytes.fromhex(dm_hex)
    except ValueError as e:
        raise ValueError(f"16進数として解釈できません: {e}") from e

    if len(hwid) != 5:
        raise ValueError(f"HWID は 5 バイト（16進数10文字）です: {hwid_hex!r}")
    if not 1 <= len(dm) <= 13:
        raise ValueError(f"Device Message は 1〜13 バイトです: {dm_hex!r}")

    return bytes([FRAME_TYPE]) + hwid + bytes([MEASURED_TX_POWER]) + dm


def build_ibeacon(
    uuid_str: str = IBEACON_UUID,
    major: int = IBEACON_MAJOR,
    minor: int = IBEACON_MINOR,
    measured_power: int = IBEACON_MEASURED_POWER,
) -> bytes:
    """iBeacon の Manufacturer Data（Apple の Company ID を除く 23 バイト）を組み立てる。"""
    return (
        bytes([0x02, 0x15])  # iBeacon の type と length
        + uuid.UUID(uuid_str).bytes
        + major.to_bytes(2, "big")
        + minor.to_bytes(2, "big")
        + (measured_power & 0xFF).to_bytes(1, "big")
    )


class Advertisement(dbus.service.Object):
    """BlueZ に登録する広告 1 つ分。"""

    def __init__(self, bus, index: int, name: str, props: dict):
        self.path = f"/org/line/simplebeacon/advertisement{index}"
        self.name = name
        self._props = props
        super().__init__(bus, self.path)

    @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        if interface != ADV_IFACE:
            raise dbus.exceptions.DBusException(
                "org.freedesktop.DBus.Error.InvalidArgs", f"Unknown interface: {interface}"
            )
        return self._props

    @dbus.service.method(ADV_IFACE, in_signature="", out_signature="")
    def Release(self):
        print(f"[{self.name}] 広告が BlueZ により解放されました")


def line_props(frame: bytes) -> dict:
    return {
        # peripheral + Discoverable で Flags (LE General Discoverable) が付与される
        "Type": "peripheral",
        "ServiceUUIDs": dbus.Array([LINE_SERVICE_UUID], signature="s"),
        "ServiceData": dbus.Dictionary(
            {LINE_SERVICE_UUID: dbus.Array(list(frame), signature="y")},
            signature="sv",
        ),
        "Discoverable": dbus.Boolean(True),
    }


def ibeacon_props(data: bytes) -> dict:
    # iBeacon は非接続型の広告。BlueZ では Discoverable と組み合わせない。
    return {
        "Type": "broadcast",
        "ManufacturerData": dbus.Dictionary(
            {dbus.UInt16(APPLE_COMPANY_ID): dbus.Array(list(data), signature="y")},
            signature="qv",
        ),
    }


def find_adapter(bus):
    om = dbus.Interface(bus.get_object(BLUEZ, "/"), OM_IFACE)
    for path, ifaces in om.GetManagedObjects().items():
        if ADV_MGR_IFACE in ifaces:
            return path
    return None


def main():
    parser = argparse.ArgumentParser(description="LINE Simple Beacon for Raspberry Pi")
    parser.add_argument("--hwid", required=True, help="LINE Official Account Manager で発行した HWID（16進数10文字）")
    parser.add_argument("--dm", default="00", help="Device Message（16進数、1〜13バイト）。既定は 00")
    parser.add_argument("--no-ibeacon", action="store_true", help="LINE 用 iBeacon を発信しない（比較実験用）")
    parser.add_argument("--labhub-uuid", default=LABHUB_UUID, help="LabHub iBeacon の UUID")
    parser.add_argument("--major", type=int, default=1, help="LabHub iBeacon の Major（研究室番号, 0〜65535）")
    parser.add_argument("--minor", type=int, default=1, help="LabHub iBeacon の Minor（部屋番号, 0〜65535）")
    parser.add_argument("--no-labhub", action="store_true", help="LabHub iBeacon を発信しない")
    args = parser.parse_args()

    try:
        frame = build_frame(args.hwid, args.dm)
        labhub_data = None
        if not args.no_labhub:
            if not (0 <= args.major <= 0xFFFF and 0 <= args.minor <= 0xFFFF):
                raise ValueError("Major / Minor は 0〜65535 です")
            labhub_data = build_ibeacon(args.labhub_uuid, args.major, args.minor)
    except ValueError as e:
        sys.exit(f"エラー: {e}")

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    adapter_path = find_adapter(bus)
    if adapter_path is None:
        sys.exit("エラー: BLE 広告に対応した Bluetooth アダプタが見つかりません")

    adapter_obj = bus.get_object(BLUEZ, adapter_path)
    dbus.Interface(adapter_obj, PROPS_IFACE).Set(ADAPTER_IFACE, "Powered", dbus.Boolean(True))
    adv_mgr = dbus.Interface(adapter_obj, ADV_MGR_IFACE)

    try:
        mgr_props = dbus.Interface(adapter_obj, PROPS_IFACE).GetAll(ADV_MGR_IFACE)
        print(f"同時に登録できる広告の数: {int(mgr_props.get('SupportedInstances', -1))}")
    except dbus.exceptions.DBusException:
        pass

    ads = [Advertisement(bus, 0, "LINE", line_props(frame))]
    if not args.no_ibeacon:
        ads.append(Advertisement(bus, 1, "iBeacon", ibeacon_props(build_ibeacon())))
    if labhub_data is not None:
        ads.append(Advertisement(bus, 2, "LabHub", ibeacon_props(labhub_data)))

    loop = GLib.MainLoop()
    registered = []

    def on_registered(ad):
        registered.append(ad)
        print(f"[{ad.name}] 発信開始")
        if ad.name == "LINE":
            print(f"  HWID          = {args.hwid.lower()}")
            print(f"  DeviceMessage = {args.dm.lower()}")
            print(f"  ServiceData   = {frame.hex()}")
        elif ad.name == "iBeacon":
            print(f"  UUID={IBEACON_UUID} Major=0x{IBEACON_MAJOR:04X} Minor=0x{IBEACON_MINOR:04X}")
        else:
            print(f"  UUID={args.labhub_uuid.upper()} Major={args.major} Minor={args.minor}")
        if len(registered) == len(ads):
            print("Ctrl+C で停止します")

    def on_error(ad, err):
        print(f"[{ad.name}] 広告の登録に失敗しました: {err}", file=sys.stderr)
        if ad.name == "LINE":
            loop.quit()
        elif ad.name == "iBeacon":
            print("  LINE 用 iBeacon なしで続行します（LINE のバックグラウンド検知は期待できません）", file=sys.stderr)
        else:
            print("  LabHub iBeacon なしで続行します（自作アプリでは検知できません）", file=sys.stderr)

    for ad in ads:
        adv_mgr.RegisterAdvertisement(
            dbus.ObjectPath(ad.path),
            dbus.Dictionary({}, signature="sv"),
            reply_handler=functools.partial(on_registered, ad),
            error_handler=functools.partial(on_error, ad),
        )

    def stop():
        for ad in registered:
            try:
                adv_mgr.UnregisterAdvertisement(dbus.ObjectPath(ad.path))
            except dbus.exceptions.DBusException:
                pass
        print("\n発信を停止しました")
        loop.quit()
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
    loop.run()


if __name__ == "__main__":
    main()
