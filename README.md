# tuya-p2p-gate

Open the gate from a Tuya-based video doorbell **without the phone app**, and get
its live video into Home Assistant.

Tested on the **IP BOX PROXE 620217** (Italian video intercom, hardware by
Shengheng, Tuya product key `jtc6fpl3`, category `sp`). Other Tuya doorbells
whose "open gate" button does nothing through the usual integrations may
behave the same way. Reports are welcome.

> Uses Tuya's **private mobile API** (the one the Smart Life app uses) with
> your own Smart Life account. It is not an official API and may break at any
> time. Provided as is, no support. Use it only on devices you own.

## The problem

- In **local mode** (tinytuya, LocalTuya, tuya-local, port 6668) the device is
  read-only: `set_value(231, False)` returns error 904.
- In the **official cloud integration** the "unlock" switch exists but does
  nothing.
- The app's unlock button sends the data point **DP 231 = false** through the
  mobile API `thing.m.device.dp.publish`. That works, but **only while the
  doorbell is awake**. Asleep, the API still answers "success" and nothing
  happens.
- The app's MQTT wake-up (`m/w/<devId>`, payload CRC32 of the local key)
  does **not** wake this device.

## What wakes it: the native P2P session

What really wakes it is what the app does when you open the camera view. It
opens a **native Tuya P2P session** (AES/KCP): you hear a click, the video SoC
powers on and from then on DP 231 opens the gate.

[tuya-ipc-p2p-sdk](https://github.com/roquerodrigo/tuya-ipc-p2p-sdk) implements
that session, but only over Tuya's TCP relay. This doorbell never joins the
relay: it answers the offer with `AES/KCP 3` and wants **direct UDP on the ICE
pair it nominates** (STUN `USE-CANDIDATE`). The datagram format, worked out
from a packet capture of the app, is:

```
datagram = KCP segment(s), concatenated
         + HMAC-SHA1(aes key from the offer, segments)     # 20 bytes
```

`p2p_udp.py` patches this transport into the SDK at runtime. With it the
device accepts the channel-0 auth and opens conversation 1 (H264 video) and 2
(audio). That is the "awake" signal, and DP 231 then opens the gate.

The P2P session is UDP, so the machine running this must reach the doorbell
directly (same LAN, or a site-to-site VPN).

## Files

| file | what it does |
|---|---|
| `p2p_wake.py` | Smart Life login, P2P session with retries, `--open` sends the DP while awake |
| `p2p_udp.py` | direct-UDP transport for tuya-ipc-p2p-sdk |
| `open_gate.py` | DP 231 through `thing.m.device.dp.publish` (`--dry-run` available) |
| `p2p_h264.py` | pulls the RTP H264 out of the session (the SDK only does JPEG) |
| `gate_server.py` | HTTP API for Home Assistant: `/open`, live MJPEG, photo |
| `examples/` | Home Assistant config, systemd user unit |

## Setup

Python 3.11+, ffmpeg for the video.

```sh
git clone https://github.com/okreq/tuya-p2p-gate && cd tuya-p2p-gate
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

mkdir -p ~/.config/tuya-p2p-gate && chmod 700 ~/.config/tuya-p2p-gate
cat > ~/.config/tuya-p2p-gate/credentials <<'EOF'
TUYA_EMAIL=you@example.com
TUYA_PASSWORD=your-smart-life-password
TUYA_COUNTRY_CODE=39
TUYA_REGION=eu
TUYA_DEVICE_ID=
EOF
chmod 600 ~/.config/tuya-p2p-gate/credentials

.venv/bin/python p2p_wake.py          # lists the cameras: copy the id into TUYA_DEVICE_ID
.venv/bin/python p2p_wake.py --wake   # you should hear the click and see "AWAKE"
.venv/bin/python p2p_wake.py --open   # opens the gate
```

Notes:
- Each run logs in to the Smart Life account. The app on your phone stays
  logged in.
- If the app is showing the camera at the same moment, the device may stay
  busy. The scripts retry (3 sessions, then cooldowns in the server).
- DP 231 and the value `false` are what this model uses. Override them with
  `DP_UNLOCK` / `DP_UNLOCK_VALUE` if yours differs. To find yours, watch the
  local DPs (tinytuya) while you tap the button in the app.

## Home Assistant

Run `gate_server.py` as a service (`examples/tuya-p2p-gate.service`). Put a
random token in `~/.config/tuya-p2p-gate/token`
(`openssl rand -hex 24 > ~/.config/tuya-p2p-gate/token`). Then use
`examples/home-assistant.yaml`:

- `rest_command.gate_open` + a script/button to open the gate;
- an MJPEG camera on `/video.mjpeg` (`/snapshot.jpg` as still image), woken by
  `rest_command.gate_wake_video` when you open the card. While the doorbell
  sleeps you get a placeholder image instead of a broken stream.

For bell-press events and the rest of the device, the local integration
[ha_tuya_doorbell](https://github.com/jurgenmahn/ha_tuya_doorbell) works fine
alongside this.

## Credits

- [roquerodrigo/tuya-ipc-p2p-sdk](https://github.com/roquerodrigo/tuya-ipc-p2p-sdk) (MIT): P2P session, KCP, crypto
- [AboveColin/tuya-mobile](https://github.com/AboveColin/tuya-mobile) (MIT): Smart Life mobile API login and signing

---

**In italiano:** apre il cancello di un videocitofono Tuya (provato su IP BOX
PROXE 620217) senza l'app. Il comando è il DP 231 via API mobile, ma funziona
solo a citofono sveglio. Per svegliarlo serve la sessione P2P nativa su UDP
diretto, che qui viene aggiunta a tuya-ipc-p2p-sdk. Le istruzioni sopra valgono
uguali.
