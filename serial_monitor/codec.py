"""Pure helpers for converting between user input and raw bytes.

Kept free of any GUI / serial dependency so it can be unit tested.
"""

from __future__ import annotations

import re

LINE_ENDINGS = {
    "None": b"",
    "CR (\\r)": b"\r",
    "LF (\\n)": b"\n",
    "CR+LF (\\r\\n)": b"\r\n",
}

_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "0": "\0",
    "\\": "\\",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "e": "\x1b",
}


class ParseError(ValueError):
    """Raised when user input cannot be converted to bytes."""


def parse_hex(text: str) -> bytes:
    """Parse a HEX string into bytes.

    Accepts bytes separated by spaces, commas, colons, dashes or nothing at
    all, with optional ``0x`` / ``h`` decorations, e.g.::

        "01 03 00 00 00 0A"
        "0x01,0x03,0x00"
        "0103000A"
        "AA:BB:cc"
    """
    tokens = [t for t in re.split(r"[\s,;:\-]+", text.strip()) if t]
    out = bytearray()
    for token in tokens:
        tok = token
        if tok[:2].lower() == "0x":
            tok = tok[2:]
        if tok[-1:].lower() == "h" and len(tok) > 1:
            tok = tok[:-1]
        if not tok or not re.fullmatch(r"[0-9a-fA-F]+", tok):
            raise ParseError(f"Invalid hex token: {token!r}")
        if len(tok) % 2:
            if len(tok) == 1:
                tok = "0" + tok
            else:
                raise ParseError(
                    f"Hex token {token!r} has an odd number of digits"
                )
        out += bytes.fromhex(tok)
    return bytes(out)


def parse_ascii(text: str, escapes: bool = True, encoding: str = "latin-1") -> bytes:
    r"""Convert ASCII text into bytes.

    When ``escapes`` is true, C-style escapes are interpreted:
    ``\r \n \t \0 \\ \xHH`` etc.
    """
    if not escapes:
        return _encode(text, encoding)

    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt in _ESCAPES:
            out.append(_ESCAPES[nxt])
            i += 2
        elif nxt in "xX":
            hex_digits = text[i + 2 : i + 4]
            if len(hex_digits) == 2 and re.fullmatch(r"[0-9a-fA-F]{2}", hex_digits):
                out.append(chr(int(hex_digits, 16)))
                i += 4
            else:
                raise ParseError(f"Invalid \\x escape at position {i}")
        else:
            # Unknown escape: keep it literally.
            out.append(ch)
            i += 1
    return _encode("".join(out), encoding)


def _encode(text: str, encoding: str) -> bytes:
    try:
        return text.encode(encoding)
    except UnicodeEncodeError as exc:
        raise ParseError(f"Cannot encode text as {encoding}: {exc}") from exc


def build_payload(text: str, mode: str, line_ending: str = "None",
                  escapes: bool = True) -> bytes:
    """Build the bytes to send for the given input ``mode`` ("ASCII"/"HEX")."""
    if mode.upper() == "HEX":
        data = parse_hex(text)
    else:
        data = parse_ascii(text, escapes=escapes)
    return data + LINE_ENDINGS.get(line_ending, b"")


def to_hex(data: bytes) -> str:
    """Format bytes as space separated uppercase hex: ``"01 0A FF"``."""
    return " ".join(f"{b:02X}" for b in data)


def to_ascii(data: bytes, show_control: bool = False) -> str:
    """Render bytes as text.

    Printable ASCII and CR/LF/TAB are kept as-is; other bytes are shown as
    ``.`` (or as ``<XX>`` when ``show_control`` is true).
    """
    parts = []
    for b in data:
        if 32 <= b < 127 or b in (9, 10, 13):
            if show_control and b in (9, 10, 13):
                parts.append({9: "<TAB>", 10: "<LF>\n", 13: "<CR>"}[b])
            else:
                parts.append(chr(b))
        else:
            parts.append(f"<{b:02X}>" if show_control else ".")
    return "".join(parts)


def hex_dump(data: bytes, width: int = 16) -> str:
    """Classic HEX + ASCII dump, one row per ``width`` bytes."""
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off : off + width]
        hex_part = to_hex(chunk).ljust(width * 3 - 1)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{hex_part}  |{asc}|")
    return "\n".join(lines)
