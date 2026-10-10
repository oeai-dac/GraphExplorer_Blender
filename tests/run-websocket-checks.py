"""Prüft die Rahmenverarbeitung in websocket.py -- ohne Netzwerk, ohne Blender.

    python tests/run-websocket-checks.py

Das Gegenstück dazu ist ``run-roundtrip-checks.mjs``, das denselben Server
gegen einen echten WebSocket-Client fährt. Beides hat seinen Zweck: Der
Rundlauf beweist, dass das Ganze funktioniert; die Prüfungen hier zeigen bei
einem Fehler, *welches* Stück nicht stimmt.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import struct  # noqa: E402

from websocket import (  # noqa: E402
    OP_CLOSE,
    OP_TEXT,
    WS_GUID,
    accept_key,
    encode_frame,
    handshake_response,
    unmask,
)

failures = []


def check(name, got, want):
    if got != want:
        failures.append("%s\n    erwartet: %r\n    bekommen: %r" % (name, want, got))


# -- Der Handschlag ----------------------------------------------------------
#
# Der Testvektor steht in RFC 6455 §1.3. Er ist hier die eigentliche Autorität:
# Die GUID stimmte im ersten Anlauf nicht (eine Hexziffer fehlte), der Browser
# wies die Verbindung wortlos ab, und ohne diesen Vektor wäre die Suche danach
# ein Ratespiel geblieben. Er ist der Grund, warum diese Prüfung existiert.

check("Accept-Wert nach RFC 6455 §1.3",
      accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
check("GUID hat UUID-Form",
      [len(part) for part in WS_GUID.split("-")], [8, 4, 4, 4, 12])

response = handshake_response("dGhlIHNhbXBsZSBub25jZQ==")
check("Statuszeile", response.startswith(b"HTTP/1.1 101 "), True)
check("Upgrade-Kopfzeile", b"Upgrade: websocket\r\n" in response, True)
check("Accept-Kopfzeile", b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n" in response, True)
check("Kopf endet mit Leerzeile", response.endswith(b"\r\n\r\n"), True)


# -- Rahmen bauen ------------------------------------------------------------
#
# Die drei Längendarstellungen und ihre Schwellen. Genau hier sitzen
# Abschneidefehler, und ein Rahmen mit falscher Länge bringt die Gegenstelle
# aus dem Tritt, ohne dass es beim Senden auffällt.

def parts(frame):
    """Erstes Byte, Längenfeld und der Rest -- zum Prüfen des Kopfes."""
    return frame[0], frame[1], frame[2:]


first, length_byte, rest = parts(encode_frame(b"hi", OP_TEXT))
check("FIN gesetzt, Opcode Text", first, 0x81)
check("kurze Länge direkt im Byte", length_byte, 2)
check("Server maskiert nicht", length_byte & 0x80, 0)
check("Nutzlast unverändert", rest, b"hi")

check("125 Bytes bleiben einbytig", encode_frame(b"a" * 125)[1], 125)

first, length_byte, rest = parts(encode_frame(b"a" * 126))
check("ab 126 folgt ein 16-Bit-Feld", length_byte, 126)
check("und trägt die Länge", struct.unpack("!H", rest[:2])[0], 126)

first, length_byte, rest = parts(encode_frame(b"a" * 65535))
check("65535 passt noch in 16 Bit", length_byte, 126)
check("Länge stimmt", struct.unpack("!H", rest[:2])[0], 65535)

first, length_byte, rest = parts(encode_frame(b"a" * 65536))
check("ab 65536 folgt ein 64-Bit-Feld", length_byte, 127)
check("und trägt die Länge", struct.unpack("!Q", rest[:8])[0], 65536)

check("Schließen-Rahmen ohne Nutzlast", encode_frame(b"", OP_CLOSE), b"\x88\x00")
check("Gesamtlänge stimmt", len(encode_frame(b"a" * 70000)), 2 + 8 + 70000)


# -- Maske herausrechnen -----------------------------------------------------

check("Maske aufgehoben", unmask(bytes(b ^ 0xAA for b in b"SE 2001"), b"\xaa\xaa\xaa\xaa"), b"SE 2001")
check("leere Nutzlast", unmask(b"", b"\x01\x02\x03\x04"), b"")

# Eine Maske, deren vier Bytes verschieden sind, deckt den Umlauf über die
# Bytegrenze hinweg ab -- eine gleichförmige Maske würde einen Indexfehler
# nicht auffallen lassen.
mask = b"\x37\xfa\x21\x3d"
plain = "Grabungsfläche Süd — SE 2001".encode("utf-8")
masked = bytes(b ^ mask[i % 4] for i, b in enumerate(plain))
check("wechselnde Maske, Länge kein Vielfaches von 4", unmask(masked, mask), plain)

# Der schnelle Weg über eine große Ganzzahl darf führende Nullbytes nicht
# verschlucken -- to_bytes(n) mit fester Länge fängt das ab, aber geprüft
# gehört es trotzdem.
plain = b"\x00\x00\x00\x00abc"
masked = bytes(b ^ mask[i % 4] for i, b in enumerate(plain))
check("führende Nullbytes bleiben erhalten", unmask(masked, mask), plain)

big = os.urandom(1024 * 1024)
masked = bytes(b ^ mask[i % 4] for i, b in enumerate(big))
check("1 MB fehlerfrei", unmask(masked, mask), big)


if failures:
    print("\n%d Prüfung(en) fehlgeschlagen:\n" % len(failures))
    for f in failures:
        print("  - %s\n" % f)
    sys.exit(1)

print("websocket.py: alle Prüfungen bestanden.")
