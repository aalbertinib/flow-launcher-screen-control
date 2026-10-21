"""Read and parse a monitor's EDID from the registry (physical size, serial...)."""

from __future__ import annotations

import logging
import math
import winreg
from dataclasses import dataclass

__all__ = (
    "EdidInfo",
    "device_instance_id",
    "parse_edid",
    "read_edid",
    "vendor_name",
)

log = logging.getLogger(__name__)

_EDID_HEADER = b"\x00\xff\xff\xff\xff\xff\xff\x00"

# A handful of common PNP vendor IDs. Unknown IDs are shown as-is.
_VENDORS = {
    "ACI": "ASUS",
    "ACR": "Acer",
    "AOC": "AOC",
    "APP": "Apple",
    "AUO": "AU Optronics",
    "AUS": "ASUS",
    "BNQ": "BenQ",
    "BOE": "BOE",
    "CMN": "Innolux",
    "DEL": "Dell",
    "ENC": "EIZO",
    "GBT": "Gigabyte",
    "GSM": "LG",
    "HPN": "HP",
    "HWP": "HP",
    "IVM": "iiyama",
    "LEN": "Lenovo",
    "LGD": "LG Display",
    "MEI": "Panasonic",
    "MSI": "MSI",
    "NEC": "NEC",
    "PHL": "Philips",
    "SAM": "Samsung",
    "SDC": "Samsung Display",
    "SHP": "Sharp",
    "SNY": "Sony",
    "VSC": "ViewSonic",
}


def vendor_name(pnp_id: str) -> str:
    return _VENDORS.get(pnp_id.upper(), pnp_id.upper())


@dataclass(frozen=True)
class EdidInfo:
    manufacturer_id: str
    product_code: int
    serial_number: str | None
    model_name: str | None
    width_cm: int | None
    height_cm: int | None
    year: int | None

    @property
    def diagonal_inches(self) -> float | None:
        if not self.width_cm or not self.height_cm:
            return None
        return math.hypot(self.width_cm, self.height_cm) / 2.54


def _descriptor_text(block: bytes) -> str | None:
    text = block[5:18].split(b"\x0a", 1)[0]
    text = text.decode("cp437", errors="replace").strip(" \x00")
    return text or None


def parse_edid(data: bytes) -> EdidInfo | None:
    if len(data) < 128 or data[:8] != _EDID_HEADER:
        return None

    raw_id = (data[8] << 8) | data[9]
    manufacturer = "".join(
        chr(((raw_id >> shift) & 0x1F) + ord("A") - 1) for shift in (10, 5, 0)
    )
    product_code = data[10] | (data[11] << 8)
    serial_int = int.from_bytes(data[12:16], "little")
    year = data[17] + 1990 if data[17] else None

    serial_text = model = None
    for offset in (54, 72, 90, 108):
        block = data[offset : offset + 18]
        if block[0:3] != b"\x00\x00\x00":
            continue  # detailed timing descriptor, not text
        if block[3] == 0xFF:
            serial_text = _descriptor_text(block)
        elif block[3] == 0xFC:
            model = _descriptor_text(block)

    serial: str | None = serial_text
    if serial is None and serial_int not in (0, 1, 0x01010101, 0xFFFFFFFF):
        serial = str(serial_int)

    return EdidInfo(
        manufacturer_id=manufacturer,
        product_code=product_code,
        serial_number=serial,
        model_name=model,
        width_cm=data[21] or None,
        height_cm=data[22] or None,
        year=year,
    )


def device_instance_id(device_path: str) -> str | None:
    r"""``\\?\DISPLAY#GSM5D24#4&1a2b3c4&0&UID1234#{guid}`` -> ``DISPLAY\GSM5D24\4&1a2b3c4&0&UID1234``"""

    path = device_path.removeprefix("\\\\?\\")
    parts = path.split("#")
    if len(parts) < 3 or not parts[0] or not parts[1] or not parts[2]:
        return None
    return "\\".join(parts[:3])


def read_edid(device_path: str) -> EdidInfo | None:
    instance = device_instance_id(device_path)
    if instance is None:
        return None
    key_path = f"SYSTEM\\CurrentControlSet\\Enum\\{instance}\\Device Parameters"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            data, kind = winreg.QueryValueEx(key, "EDID")
    except OSError:
        log.debug("No EDID in registry for %s", instance)
        return None
    if kind != winreg.REG_BINARY or not isinstance(data, bytes):
        return None
    return parse_edid(data)
