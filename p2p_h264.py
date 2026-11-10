"""H264 video from the native P2P session (KCP conversation 1).

tuya-ipc-p2p-sdk can only reassemble JPEG; this doorbell sends RTP H264 inside
a continuous stream, split at random points across the decrypted records:

    +0   03 00 01 00    video channel
    +16  uint32 LE      extension length (0 or 8)
    +24  ext            (8 bytes on keyframes: 720x400 etc.)
    +24+ext uint32 LE   RTP packet length
    then RTP H264 (PT 96, single NAL / FU-A / STAP-A)

Worked out from a session dump: 720x400, ~24 fps, continuous (unlike the
cloud WebRTC stream, which always stops after 384 packets).
"""
from __future__ import annotations

import os

_START = b"\x03\x00\x01\x00"
_SC = b"\x00\x00\x00\x01"


class H264Extractor:
    def __init__(self) -> None:
        self._buf = bytearray()
        self._aligned = False
        self._started = False  # drop everything until the first SPS (ffmpeg cannot probe without it)

    def feed(self, record: bytes) -> bytes:
        """Add a decrypted record, return the H264 Annex-B completed so far."""
        self._buf += record
        out = bytearray()
        b = self._buf
        i = 0
        while True:
            if not self._aligned:
                j = b.find(_START, i)
                if j < 0:
                    i = max(i, len(b) - 3)
                    break
                i, self._aligned = j, True
            if len(b) - i < 28:
                break
            ext = int.from_bytes(b[i + 16:i + 20], "little")
            if b[i:i + 2] != b"\x03\x00" or ext > 64:
                self._aligned = False
                i += 1
                continue
            if len(b) - i < 28 + ext:
                break
            n = int.from_bytes(b[i + 24 + ext:i + 28 + ext], "little")
            if not 12 <= n <= 200_000:
                self._aligned = False
                i += 1
                continue
            a = i + 28 + ext
            if len(b) < a + n:
                break
            if b[i + 2:i + 4] == b"\x01\x00":
                out += _rtp_to_annexb(bytes(b[a:a + n]))
            i = a + n
        del b[:i]
        if not self._started:
            j = 0
            while (j := out.find(_SC, j)) >= 0 and j + 4 < len(out) and out[j + 4] & 0x1F != 7:
                j += 4
            if j < 0 or j + 4 >= len(out):
                return b""
            self._started, out = True, out[j:]
        return bytes(out)


def _rtp_to_annexb(p: bytes) -> bytes:
    off = 12 + 4 * (p[0] & 0x0F)
    pl = p[off:]
    if not pl:
        return b""
    t = pl[0] & 0x1F
    if t == 28:  # FU-A
        if len(pl) < 2:
            return b""
        if pl[1] & 0x80:
            return _SC + bytes([(pl[0] & 0xE0) | (pl[1] & 0x1F)]) + pl[2:]
        return pl[2:]
    if t == 24:  # STAP-A
        out, q = bytearray(), 1
        while q + 2 <= len(pl):
            n = int.from_bytes(pl[q:q + 2], "big")
            out += _SC + pl[q + 2:q + 2 + n]
            q += 2 + n
        return bytes(out)
    return _SC + pl


def install(fd: int) -> None:
    """Write to `fd` the H264 of every P2P session opened by this process."""
    from tuya_ipc_p2p_sdk import stream_session as ss
    from tuya_ipc_p2p_sdk.crypto import decrypt_record

    extractors: dict[int, H264Extractor] = {}
    state = {"closed": False}
    original = ss.StreamSession._on_media_record

    def _on_media_record(self, record: bytes) -> None:
        if not state["closed"]:
            try:
                plain = decrypt_record(self._answer_key, record)
            except Exception:
                plain = None
            if plain:
                data = extractors.setdefault(id(self), H264Extractor()).feed(plain)
                try:
                    if data:
                        os.write(fd, data)
                except OSError:  # ffmpeg gone: stop writing, keep the session
                    state["closed"] = True
        return original(self, record)

    ss.StreamSession._on_media_record = _on_media_record
