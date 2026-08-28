"""Extract chara_card_v2 JSON from PNG tEXt / zTXt 'chara' chunks."""

from __future__ import annotations

import base64
import json
import struct
import zlib
from typing import Any

from server.core.persona.chara_v2 import CharaCardError


def _read_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise CharaCardError("not a PNG file")
    pos = 8
    chunks: list[tuple[bytes, bytes]] = []
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        start = pos + 8
        end = start + length
        if end + 4 > len(data):
            break
        chunk_data = data[start:end]
        chunks.append((ctype, chunk_data))
        pos = end + 4  # skip CRC
        if ctype == b"IEND":
            break
    return chunks


def extract_chara_json_from_png(data: bytes) -> dict[str, Any]:
    """Return decoded card JSON from PNG 'chara' text chunk."""
    for ctype, chunk in _read_chunks(data):
        if ctype not in (b"tEXt", b"zTXt"):
            continue
        if b"\x00" not in chunk:
            continue
        key, _, rest = chunk.partition(b"\x00")
        if key.decode("latin-1", errors="ignore") != "chara":
            continue
        raw = rest
        if ctype == b"zTXt":
            # compression method byte then zlib stream
            if not raw:
                continue
            if raw[0] != 0:
                raise CharaCardError("unsupported zTXt compression")
            try:
                raw = zlib.decompress(raw[1:])
            except zlib.error as exc:
                raise CharaCardError("invalid zTXt chara chunk") from exc
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        try:
            decoded = base64.b64decode(text)
            payload = json.loads(decoded.decode("utf-8"))
        except Exception:
            try:
                payload = json.loads(text)
            except Exception as exc:
                raise CharaCardError("chara chunk is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise CharaCardError("chara chunk JSON must be an object")
        return payload
    raise CharaCardError("PNG has no chara text chunk")
