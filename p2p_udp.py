"""Direct-UDP transport for tuya-ipc-p2p-sdk (patched in at runtime).

tuya-ipc-p2p-sdk only speaks the TCP relay. This doorbell answers the offer
with `AES/KCP 3` and never joins the relay: it wants plain P2P over UDP on the
ICE pair it nominates itself (STUN USE-CANDIDATE). This module swaps the SDK's
RelayConnection for one that sends over that nominated ICE socket; everything
above it (KCP conversations, channel-0 auth, encryption) stays the SDK's.

On-the-wire format, taken from a packet capture of the official app:

    datagram = one or more KCP segments, concatenated
             + HMAC-SHA1(aes key from the offer, segments)   (20 bytes)

No TLV header, no relay framing. Once the device accepts the channel-0 auth it
opens conversation 1 (H264 video) and 2 (audio): that is the moment the video
SoC powers on, and the moment it starts accepting the unlock DP.
"""
from __future__ import annotations

import asyncio
import time

import tuya_ipc_p2p_sdk.stream_session as _ss
import tuya_ipc_p2p_sdk.transport.ice_responder as _ir
import tuya_ipc_p2p_sdk.transport.relay_session as _rs
from tuya_ipc_p2p_sdk.transport.relay_framing import media_tag

_STATE = {"ice": None, "peer": None, "nominated": None, "conn": None, "media": 0}
VERBOSE = False


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _split(data: bytes) -> list[bytes]:
    """Split a datagram into KCP segments (the last 20 bytes are the tag)."""
    body, segs, i = data[:-20], [], 0
    while i + 24 <= len(body):
        n = int.from_bytes(body[i + 20:i + 24], "little")
        if i + 24 + n > len(body):
            return []
        segs.append(body[i:i + 24 + n])
        i += 24 + n
    return segs if i == len(body) else []


class UdpConnection:
    """Same interface as the SDK's RelayConnection, on top of the ICE socket."""

    def __init__(self, media_key: bytes) -> None:
        self._key = media_key
        self._seg = None
        self._close = None
        self.sent = 0
        self.recv = 0

    async def async_connect(self) -> None:
        _STATE["conn"] = self
        async with asyncio.timeout(10):
            await asyncio.shield(_STATE["nominated"])
        print(f"[udp] {_ts()} pair nominated, peer {_STATE['peer']}")

    def set_segment_handler(self, h) -> None:
        self._seg = h

    def set_close_handler(self, h) -> None:
        self._close = h

    def write_segment(self, seg: bytes) -> None:
        ice = _STATE["ice"]
        if ice is None or ice._transport is None or _STATE["peer"] is None:
            return
        self.sent += 1
        ice._transport.sendto(seg + media_tag(self._key, seg), _STATE["peer"])

    def feed(self, data: bytes) -> None:
        self.recv += 1
        segs = _split(data)
        if VERBOSE and self.recv <= 12:
            print(f"[udp<] {_ts()} {len(data)}B -> " + " ".join(
                f"conv={int.from_bytes(s[:4], 'little'):#x}/cmd={s[4]:#x}" for s in segs))
        for s in segs:
            if int.from_bytes(s[:4], "little") != 0:
                _STATE["media"] += 1  # conversation 1 (video) / 2 (audio) = device awake
            if self._seg:
                self._seg(s)

    async def async_close(self) -> None:
        _STATE["conn"] = None
        h, self._close = self._close, None
        if h:
            h(None)


def reset() -> None:
    """Call before every new session: new ICE pair, media counter back to zero."""
    _STATE.update(peer=None, conn=None, media=0,
                  nominated=asyncio.get_event_loop().create_future())


def media_count() -> int:
    """KCP segments received on media conversations: > 0 means the device is awake."""
    return _STATE["media"]


def install(verbose: bool = False) -> None:
    global VERBOSE
    VERBOSE = verbose
    reset()

    orig_gather = _ir.IceResponder.async_gather
    orig_rx = _ir.IceResponder.datagram_received

    async def gather(self):
        _STATE["ice"] = self
        await orig_gather(self)

    def rx(self, data, addr):
        if len(data) >= 20 and data[4:8] == b"\x21\x12\xa4\x42":  # STUN
            i = 20
            while i + 4 <= len(data):
                at = int.from_bytes(data[i:i + 2], "big")
                al = int.from_bytes(data[i + 2:i + 4], "big")
                if at == 0x0025 and not _STATE["nominated"].done():  # USE-CANDIDATE
                    _STATE["peer"] = addr
                    _STATE["nominated"].set_result(addr)
                i += 4 + al + (-al % 4)
            return orig_rx(self, data, addr)
        if _STATE["conn"] is not None:
            _STATE["conn"].feed(data)

    _ir.IceResponder.async_gather = gather
    _ir.IceResponder.datagram_received = rx

    class UdpRelaySession(_rs.RelaySession):
        def __init__(self, token, signer, device_id, uid, media_key):
            super().__init__(token, signer, device_id, uid, media_key)
            self._connection = UdpConnection(media_key)
            # conversation 0 was bound to the original RelayConnection
            self._conversations.clear()
            self.control = self._conversation(_rs.CONTROL_CONVERSATION)

    _ss.RelaySession = UdpRelaySession
    # the offer tunnel only exists to bind the relay to the session: not needed
    _ss.StreamSession._tunnel_signaling = lambda self, *a, **k: None
