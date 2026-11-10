#!/usr/bin/env python3
"""Wake the doorbell with Tuya's native P2P session (AES/KCP), like the app does.

tuya-ipc-p2p-sdk handles the Smart Life login, the offer over MQTT and the KCP
tunnel with channel-0 auth; p2p_udp.py adds the direct-UDP transport this
device requires. Opening the session is what the app does when you open the
camera view: the device powers its video SoC on (audible click) and from that
moment accepts the unlock DP.

Usage:
  python p2p_wake.py            list the cameras on the account (no session)
  python p2p_wake.py --wake     open a session and hold it (listen for the click)
  python p2p_wake.py --open     like --wake, then send the unlock DP (open_gate.py)
Options: --hold N (seconds to keep the session, default 20), --prewait N (default 4),
         --h264-fd N (write the H264 Annex-B video to that file descriptor)
Exit code 3: DP sent but the device never woke up (unlock not confirmed).
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import subprocess
import sys
import time

from tuya_ipc_p2p_sdk import TuyaIpcP2pClient

import config
import p2p_udp

HERE = pathlib.Path(__file__).resolve().parent
OPEN_GATE = HERE / "open_gate.py"


def ts() -> str:
    return time.strftime("%H:%M:%S")


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--wake", action="store_true")
    p.add_argument("--open", action="store_true")
    p.add_argument("--hold", type=float, default=20)
    p.add_argument("--prewait", type=float, default=4)
    p.add_argument("--attempts", type=int, default=3,
                   help="P2P sessions to try if the device sends no media")
    p.add_argument("--media-wait", type=float, default=3,
                   help="seconds within which video/audio must arrive")
    p.add_argument("--h264-fd", type=int, help="file descriptor for the H264 video")
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args()

    cfg = config.load()
    dev = cfg["TUYA_DEVICE_ID"]
    p2p_udp.install(verbose=a.verbose)
    if a.h264_fd is not None:
        import p2p_h264
        p2p_h264.install(a.h264_fd)

    async with TuyaIpcP2pClient(cfg["TUYA_EMAIL"], cfg["TUYA_PASSWORD"],
                                cfg["TUYA_COUNTRY_CODE"], region=cfg["TUYA_REGION"]) as client:
        # list_devices does not open sessions (discover does)
        cams = await client.async_list_devices()
        for cam in cams:
            print(f"[dev] {cam.name}  {cam.device_id}")
        cam = next((x for x in cams if x.device_id == dev), None)
        if cam is None:
            print(f"[!] {dev} not found among the cameras of this account")
            return 1
        if not (a.wake or a.open):
            return 0

        # sometimes the device acks the segments but never answers the channel-0
        # auth (no conversation 1/2): close and retry with a fresh session
        for n in range(1, a.attempts + 1):
            p2p_udp.reset()
            stream = client.create_camera_stream(cam.device_id, cam.local_key)
            print(f"[*] {ts()} starting P2P session (attempt {n}/{a.attempts}) ...")
            try:
                await stream.async_start()
            except Exception as e:
                print(f"[!] {ts()} start failed: {e!r}")
                await stream.async_stop()
                continue
            t0 = time.monotonic()
            while time.monotonic() - t0 < a.media_wait and not p2p_udp.media_count():
                await asyncio.sleep(0.2)
            if p2p_udp.media_count():
                break
            print(f"[!] {ts()} no video/audio within {a.media_wait:.0f}s")
            if n < a.attempts:
                await stream.async_stop()
                await asyncio.sleep(1)
        awake = p2p_udp.media_count() > 0
        print(f"[*] {ts()} device " + ("AWAKE (media received)" if awake else "NOT AWAKE"))
        rc = 0
        try:
            await stream.async_wait_for_frame(a.prewait)  # JPEG only: usually None, harmless
            if a.open:
                print(f"[*] {ts()} sending unlock DP while the P2P session is up ...")
                rc = await asyncio.to_thread(subprocess.call, [sys.executable, str(OPEN_GATE)])
            await asyncio.sleep(a.hold)
        finally:
            await stream.async_stop()
            print(f"[*] {ts()} session closed"
                  + (" (device needs a power cycle!)" if stream.needs_power_cycle else ""))
        if rc == 0 and a.open and not awake:
            rc = 3
        return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
