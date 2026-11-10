#!/usr/bin/env python3
"""Send the gate-unlock data point through the Tuya mobile API.

What the Smart Life app does when you tap "open gate":

  1) MQTT wake-up       topic   m/w/<devId>
                        payload CRC32(localKey), 4 bytes big-endian
  2) wait ~2 s
  3) POST thing.m.device.dp.publish v1.0
          devId = <devId>, gwId = "", dps = {"231": false}

DP 231 is non-standard: it is not listed by the Tuya OpenAPI (GetProperties /
GetFunctions) and the device rejects it on the local protocol (port 6668 is
read-only on this firmware). Only the private mobile API accepts it.

On its own this is NOT enough: the device ignores the DP while it sleeps, and
the MQTT wake above does not wake it. Run it through `p2p_wake.py --open`,
which keeps a native P2P session open (the "click" you hear) and then calls
this script.

Usage:
  python open_gate.py             send the command
  python open_gate.py --dry-run   log in and print what would be sent
Environment: DP_UNLOCK (default 231), DP_UNLOCK_VALUE (default false)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import struct
import sys
import zlib

import aiohttp
import paho.mqtt.client as mqtt
from tuya_mobile import TuyaMobileApp, TuyaPasswordClient, get_mobile_app_profile
from tuya_mobile.mqtt_auth import mqtt_client_id, mqtt_credentials
from tuya_mobile.password_client import EMAIL_LOGIN_API, LOGIN_OPTIONS, _rsa_encrypt_password
from tuya_mobile.signer import PurePythonTuyaSigner

import config

DP_UNLOCK = os.environ.get("DP_UNLOCK", "231")
DP_UNLOCK_VALUE = os.environ.get("DP_UNLOCK_VALUE", "false").lower() == "true"
WAKE_DELAY_S = 2.0


def mqtt_wake(login: dict, device_id: str, local_key: str) -> None:
    """Publish the 4-byte wake-up message the app sends before talking to a doorbell."""
    profile = get_mobile_app_profile(TuyaMobileApp.SMART_LIFE)
    signer = PurePythonTuyaSigner(
        app_id=profile.app_id, app_secret=profile.app_secret,
        cert_sha256_hex=profile.cert_sha256_hex, app_key=profile.app_key,
        package=profile.package,
    )
    creds = mqtt_credentials(signer, uid=login["uid"], sid=login["sid"],
                             ecode=login["ecode"], partner_id=login["partnerIdentity"])
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=mqtt_client_id(profile.package))
    client.username_pw_set(creds["username"], creds["password"])
    client.tls_set_context(ssl.create_default_context())
    client.connect(login["domain"]["mobileMqttsUrl"], login["domain"]["mqttsPort"], keepalive=30)
    client.loop_start()
    payload = struct.pack(">I", zlib.crc32(local_key.encode()) & 0xFFFFFFFF)
    client.publish(f"m/w/{device_id}", payload, qos=1).wait_for_publish(timeout=5)
    client.loop_stop()
    client.disconnect()


async def run(dry_run: bool) -> int:
    cfg = config.load()
    dev = cfg["TUYA_DEVICE_ID"]
    dps = {DP_UNLOCK: DP_UNLOCK_VALUE}

    async with aiohttp.ClientSession() as session:
        client = TuyaPasswordClient.for_application(
            TuyaMobileApp.SMART_LIFE, session=session, username=cfg["TUYA_EMAIL"])
        # manual login: the raw answer carries partnerIdentity and the MQTT domain
        tok = await client._get_login_token(cfg["TUYA_COUNTRY_CODE"])
        login = await client._submit_login(
            *EMAIL_LOGIN_API,
            {
                "countryCode": cfg["TUYA_COUNTRY_CODE"],
                "email": cfg["TUYA_EMAIL"],
                "passwd": _rsa_encrypt_password(cfg["TUYA_PASSWORD"], tok),
                "options": LOGIN_OPTIONS,
                "token": str(tok["token"]),
                "ifencrypt": 1,
            },
            context="login",
        )
        client.sid, client.ecode, client.uid = (str(login[k]) for k in ("sid", "ecode", "uid"))
        client.mobile_url = login["domain"]["mobileApiUrl"] + "/api.json"

        r = await client._mobile_call("thing.m.device.get", "4.1", {"devId": dev})
        local_key = r["result"]["localKey"]

        if dry_run:
            print(f"[dry-run] MQTT wake on m/w/{dev}, then thing.m.device.dp.publish dps={json.dumps(dps)}")
            return 0

        print("[*] MQTT wake ...", flush=True)
        await asyncio.to_thread(mqtt_wake, login, dev, local_key)
        await asyncio.sleep(WAKE_DELAY_S)

        print("[*] thing.m.device.dp.publish ...", flush=True)
        res = await client._mobile_call(
            "thing.m.device.dp.publish", "1.0",
            {"devId": dev, "gwId": "", "dps": json.dumps(dps, separators=(",", ":"))},
        )
        if res.get("success"):
            # the API says success even when the device was asleep:
            # p2p_wake.py is what tells whether it was really awake
            print("GATE COMMAND SENT")
            return 0
        print(f"ERROR: {json.dumps(res, ensure_ascii=False)}", file=sys.stderr)
        return 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true")
    sys.exit(asyncio.run(run(p.parse_args().dry_run)))


if __name__ == "__main__":
    main()
