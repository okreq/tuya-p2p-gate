"""Shared configuration: Smart Life credentials and the target device.

Values are read from environment variables first, then from a KEY=VALUE file
(default ~/.config/tuya-p2p-gate/credentials, override with TUYA_GATE_CONFIG):

    TUYA_EMAIL=you@example.com
    TUYA_PASSWORD=...
    TUYA_COUNTRY_CODE=39        # phone country code of the account (39 = Italy)
    TUYA_REGION=eu              # eu / us / cn / in
    TUYA_DEVICE_ID=bf00000000000000000000

Keep the file private (chmod 600).
"""
from __future__ import annotations

import os
import pathlib
import sys

CONFIG_PATH = pathlib.Path(
    os.environ.get("TUYA_GATE_CONFIG", "~/.config/tuya-p2p-gate/credentials")
).expanduser()

REQUIRED = ("TUYA_EMAIL", "TUYA_PASSWORD", "TUYA_COUNTRY_CODE", "TUYA_DEVICE_ID")
DEFAULTS = {"TUYA_REGION": "eu"}


def load() -> dict[str, str]:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        for line in CONFIG_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip("'\"")
    for k in (*REQUIRED, *DEFAULTS):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        sys.exit(f"missing {', '.join(missing)} (environment or {CONFIG_PATH})")
    return cfg
