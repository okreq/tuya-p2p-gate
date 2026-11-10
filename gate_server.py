#!/usr/bin/env python3
"""Small HTTP server: gate unlock + live video for Home Assistant.

Every unlock runs `p2p_wake.py --open` (native P2P session that wakes the
device, then the unlock DP); overlapping requests never run twice.

API (Bearer token where noted):
  POST /open          [token] -> 200 {"ok": true}  unlocked (or already in progress)
                                 502              device never woke up / DP refused
  POST /wake-video    [token] -> starts (or renews) a P2P session for VIDEO_HOLD
                                 seconds; its H264 goes through ffmpeg and
                                 becomes the MJPEG of /video.mjpeg
  GET  /photo         [token] -> a real JPEG from the doorbell (~10 s), 503 if none
  GET  /wake-status   [token] -> state of the video session
  GET  /video.mjpeg            -> MJPEG; a placeholder while the doorbell sleeps
  GET  /snapshot.jpg           -> latest recent frame, or the placeholder
  GET  /health

The video endpoints have no auth (video only, like most LAN cameras in HA):
bind to a trusted network. Token: ~/.config/tuya-p2p-gate/token (>= 24
chars, chmod 600). Needs ffmpeg in PATH; /photo also needs PyAV (`av`).
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import time

from aiohttp import web

HERE = pathlib.Path(__file__).resolve().parent
CONFIG_DIR = pathlib.Path("~/.config/tuya-p2p-gate").expanduser()
TOKEN_PATH = pathlib.Path(os.environ.get("GATE_TOKEN_FILE", CONFIG_DIR / "token"))
LISTEN_HOST = os.environ.get("GATE_LISTEN", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("GATE_PORT", "8787"))
P2P_WAKE = HERE / "p2p_wake.py"
PLACEHOLDER = (HERE / "placeholder.jpg").read_bytes()


def _floats(name: str, default: str) -> list[float]:
    return [float(x) for x in os.environ.get(name, default).split(",") if x]


# A successful unlock takes ~18 s (wake ~2 s, prewait 4 s, DP ~5 s, hold 8 s),
# so there are three separate timeouts instead of one. If "AWAKE" does not show
# up within OPEN_WAKE_TIMEOUT the device is stuck: close everything, pause,
# retry. Pauses grow (5 s, then 15 s), like the manual cure in the app.
OPEN_WAKE_TIMEOUT = float(os.environ.get("GATE_WAKE_TIMEOUT", "10"))
OPEN_DP_TIMEOUT = float(os.environ.get("GATE_DP_TIMEOUT", "20"))
OPEN_HOLD_TIMEOUT = 15
OPEN_COOLDOWN = _floats("GATE_COOLDOWN", "5,15")
VIDEO_HOLD = int(os.environ.get("GATE_VIDEO_HOLD", "90"))
VIDEO_COOLDOWN = _floats("GATE_VIDEO_COOLDOWN", "5,15")
VIDEO_WAKE_TIMEOUT = float(os.environ.get("GATE_VIDEO_WAKE_TIMEOUT", "10"))
PHOTO_LIMIT_S = 25.0
PHOTO_SETTLE_S = 1.0     # exposure settles after the first real frame
FRAME_FRESH_S = 10       # older frames are replaced by the placeholder
FFMPEG_ARGS = ["ffmpeg", "-loglevel", "fatal", "-probesize", "65536",
               "-analyzeduration", "500000", "-f", "h264", "-i", "pipe:0",
               "-vf", "fps=8", "-q:v", "6", "-f", "mjpeg", "pipe:1"]

_open_lock = asyncio.Lock()
_photo_lock = asyncio.Lock()
_video_proc: asyncio.subprocess.Process | None = None
_video_task: asyncio.Task | None = None
_video_state: dict = {"state": "idle", "ts": 0.0, "attempt": 0, "detail": ""}
_ffmpeg: asyncio.subprocess.Process | None = None
_readers: set = set()
_frame: dict = {"jpg": None, "ts": 0.0}
_new_frame = asyncio.Event()


def _authorized(request: web.Request) -> bool:
    return request.headers.get("Authorization", "") == f"Bearer {request.app['token']}"


async def _kill(proc: asyncio.subprocess.Process | None) -> None:
    if proc and proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()


# ---------------------------------------------------------------- video ----

async def _start_ffmpeg() -> int:
    """ffmpeg H264 -> JPEG; returns the write fd to hand to p2p_wake.py."""
    global _ffmpeg
    await _stop_ffmpeg()
    r, w = os.pipe()
    try:
        _ffmpeg = await asyncio.create_subprocess_exec(
            *FFMPEG_ARGS, stdin=r, stdout=asyncio.subprocess.PIPE)
    finally:
        os.close(r)
    task = asyncio.create_task(_read_jpegs(_ffmpeg))
    _readers.add(task)  # keep a strong reference to the task
    task.add_done_callback(_readers.discard)
    return w


async def _stop_ffmpeg() -> None:
    global _ffmpeg
    if _ffmpeg and _ffmpeg.returncode is None:
        _ffmpeg.kill()
        await _ffmpeg.wait()
    _ffmpeg = None


async def _read_jpegs(proc: asyncio.subprocess.Process) -> None:
    """Split ffmpeg's MJPEG output into JPEGs and publish them in _frame."""
    buf = bytearray()
    while data := await proc.stdout.read(65536):
        buf += data
        while True:
            a = buf.find(b"\xff\xd8")
            z = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
            if z < 0:
                break
            _frame.update(jpg=bytes(buf[a:z + 2]), ts=time.time())
            del buf[:z + 2]
            _new_frame.set()
            _new_frame.clear()


def _current_frame() -> bytes:
    if _frame["jpg"] and time.time() - _frame["ts"] < FRAME_FRESH_S:
        return _frame["jpg"]
    return PLACEHOLDER


def _set_state(state: str, attempt: int = 0, detail: str = "") -> None:
    _video_state.update(state=state, attempt=attempt, detail=detail, ts=time.time())
    print(f"[video] {state} (attempt {attempt}) {detail}", flush=True)


async def _stop_video() -> None:
    global _video_proc, _video_task
    if _video_task and not _video_task.done():
        _video_task.cancel()
        try:
            await _video_task
        except (asyncio.CancelledError, Exception):
            pass
    _video_task = None
    await _kill(_video_proc)
    _video_proc = None
    await _stop_ffmpeg()


async def _video_loop(hold: int) -> None:
    """Wake after wake, with cooldowns, until the device answers."""
    global _video_proc
    attempts = len(VIDEO_COOLDOWN) + 1
    for n in range(1, attempts + 1):
        _set_state("starting", n)
        fd = await _start_ffmpeg()
        try:
            _video_proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", str(P2P_WAKE), "--wake", "--prewait", "1",
                "--hold", str(hold), "--h264-fd", str(fd),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                pass_fds=(fd,))
        finally:
            os.close(fd)  # stays open only in p2p_wake: ffmpeg sees EOF when it exits
        result = None

        async def _watch():
            nonlocal result
            while raw := await _video_proc.stdout.readline():
                line = raw.decode(errors="replace").rstrip()
                print(f"[video#{n}] {line}", flush=True)
                if "AWAKE (media received)" in line:
                    result = True
                    return
                if "NOT AWAKE" in line:
                    result = False
                    return
        try:
            await asyncio.wait_for(_watch(), VIDEO_WAKE_TIMEOUT)
        except asyncio.TimeoutError:
            _set_state("wake_timeout", n)
        if result:
            _set_state("awake", n)
            # keep draining stdout: with a full pipe p2p_wake (and the video) would block
            while await _video_proc.stdout.readline():
                pass
            return
        await _kill(_video_proc)
        _video_proc = None
        if n < attempts:
            pause = VIDEO_COOLDOWN[n - 1]
            _set_state("cooldown", n, f"waiting {pause:.0f}s")
            await asyncio.sleep(pause)
    _set_state("failed", attempts, "device stuck")


# ----------------------------------------------------------------- gate ----

async def _open_attempt(n: int) -> dict:
    """One `p2p_wake.py --open`, read line by line: give up quickly if the
    device does not wake, but leave it time to send the DP if it does."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", str(P2P_WAKE), "--open", "--prewait", "4", "--hold", "8",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    lines: list[str] = []

    async def _until(*signals: str) -> str | None:
        while raw := await proc.stdout.readline():
            line = raw.decode(errors="replace").rstrip()
            lines.append(line)
            print(f"[open#{n}] {line}", flush=True)
            for s in signals:
                if s in line:
                    return s
        return None

    awake = sent = False
    try:
        seen = await asyncio.wait_for(
            _until("AWAKE (media received)", "NOT AWAKE"), OPEN_WAKE_TIMEOUT)
        awake = seen == "AWAKE (media received)"
        if awake:
            sent = await asyncio.wait_for(_until("GATE COMMAND SENT"), OPEN_DP_TIMEOUT) is not None
        if sent:
            await asyncio.wait_for(_until(), OPEN_HOLD_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"[open#{n}] timeout (awake={awake} sent={sent})", flush=True)
    finally:
        await _kill(proc)
    return {"success": awake and sent, "attempt": n, "not_confirmed": not awake,
            "log": "\n".join(lines)[-600:]}


async def open_gate() -> dict:
    if _open_lock.locked():
        return {"success": True, "already_running": True}
    async with _open_lock:
        # the gate wins: a running video/photo session would keep the device busy
        await _stop_video()
        last: dict = {}
        for n in range(1, len(OPEN_COOLDOWN) + 2):
            last = await _open_attempt(n)
            if last["success"] or n > len(OPEN_COOLDOWN):
                return last
            pause = OPEN_COOLDOWN[n - 1]
            print(f"[open#{n}] not confirmed, retrying in {pause:.0f}s", flush=True)
            await asyncio.sleep(pause)
        return last


# ------------------------------------------------------------- handlers ----

async def handle_open(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    r = await open_gate()
    if r.get("success"):
        return web.json_response({"ok": True, "already_running": r.get("already_running", False)})
    return web.json_response({"error": "not_opened", "detail": r}, status=502)


async def handle_wake_video(request: web.Request) -> web.Response:
    global _video_task
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    if _open_lock.locked():
        return web.json_response({"error": "gate_opening"}, status=503)
    await _stop_video()  # already running: restart so the hold starts over
    _video_task = asyncio.create_task(_video_loop(VIDEO_HOLD))
    return web.json_response({"ok": True, "hold": VIDEO_HOLD})


async def handle_wake_status(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    out = dict(_video_state)
    out["age"] = round(time.time() - out["ts"], 1) if out["ts"] else None
    out["running"] = bool(_video_proc and _video_proc.returncode is None)
    return web.json_response(out)


def _real_photo(jpg: bytes) -> bool:
    """With the sensor off the frame is flat grey apart from the timestamp on top."""
    import io
    import av
    with av.open(io.BytesIO(jpg), format="mjpeg") as c:
        f = next(c.decode(video=0))
    y = f.planes[0]
    data = bytes(y)
    samples = [data[r * y.line_size + x] for r in range(f.height // 3, f.height, 12)
               for x in range(0, f.width, 12)]
    mean = sum(samples) / len(samples)
    return sum((v - mean) ** 2 for v in samples) / len(samples) > 40


async def handle_photo(request: web.Request) -> web.Response:
    global _video_task
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    if _open_lock.locked():
        return web.json_response({"error": "gate_opening"}, status=503)
    async with _photo_lock:
        t0 = time.time()
        await _stop_video()
        _frame.update(jpg=None, ts=0.0)
        _video_task = asyncio.create_task(_video_loop(int(PHOTO_LIMIT_S) + 5))
        photo, first = None, None
        try:
            while time.time() - t0 < PHOTO_LIMIT_S and not _open_lock.locked():
                try:
                    await asyncio.wait_for(_new_frame.wait(), 1)
                except asyncio.TimeoutError:
                    continue
                jpg = _frame["jpg"]
                if not jpg or not _real_photo(jpg):
                    continue
                photo, first = jpg, first or time.time()
                if time.time() - first >= PHOTO_SETTLE_S:
                    break
        finally:
            # free the device right away, so "Answer" in the app can get in
            if not _open_lock.locked():
                await _stop_video()
        if not photo:
            return web.json_response({"error": "no_photo"}, status=503)
        return web.Response(body=photo, content_type="image/jpeg")


async def handle_video(request: web.Request) -> web.StreamResponse:
    """MJPEG: P2P frames when there are any, otherwise the placeholder every
    2 s, so the HA card never shows a broken image."""
    resp = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control": "no-store"})
    await resp.prepare(request)
    try:
        while True:
            try:
                await asyncio.wait_for(_new_frame.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
            jpg = _current_frame()
            await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                             + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


async def handle_snapshot(request: web.Request) -> web.Response:
    """Never opens a session: HA polls this all the time."""
    return web.Response(body=_current_frame(), content_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def main() -> None:
    token = TOKEN_PATH.read_text().strip() if TOKEN_PATH.exists() else ""
    if len(token) < 24:
        sys.exit(f"put a token of at least 24 characters in {TOKEN_PATH}")
    app = web.Application()
    app["token"] = token
    app.router.add_post("/open", handle_open)
    app.router.add_post("/wake-video", handle_wake_video)
    app.router.add_get("/wake-status", handle_wake_status)
    app.router.add_get("/photo", handle_photo)
    app.router.add_get("/video.mjpeg", handle_video)
    app.router.add_get("/snapshot.jpg", handle_snapshot)
    app.router.add_get("/health", handle_health)
    print(f"[*] listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=lambda _: None)


if __name__ == "__main__":
    main()
