"""Read-only hardware diagnostics: prints what the plugin sees for each screen.

    python tools/probe.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screen_control.win32 import ddc, displayconfig, edid, gdi  # noqa: E402


def main() -> None:
    gdi.enable_dpi_awareness()
    print(f"Python {sys.version.split()[0]} ({sys.executable})\n")

    t = time.perf_counter()
    paths = displayconfig.query_active_paths()
    print(f"CCD: {len(paths)} active path(s) in {(time.perf_counter() - t) * 1000:.1f} ms")
    monitors = {m.device_name: m for m in gdi.enum_monitors()}

    for p in paths:
        print(f"\n== {p.friendly_name or '(no name)'}  [{p.gdi_device_name}]")
        print(f"   device path : {p.device_path}")
        print(f"   output      : {displayconfig.OUTPUT_TECHNOLOGY_NAMES.get(p.output_technology, p.output_technology)}")
        print(f"   mode        : {p.width}x{p.height} @ {p.refresh_hz and round(p.refresh_hz, 2)} Hz")
        g = monitors.get(p.gdi_device_name)
        if g:
            print(f"   desktop rect: ({g.left},{g.top})-({g.right},{g.bottom}) primary={g.primary} scale={g.scale_percent}%")
        info = edid.read_edid(p.device_path)
        if info:
            diag = info.diagonal_inches
            print(
                f"   EDID        : {edid.vendor_name(info.manufacturer_id)} ({info.manufacturer_id}) "
                f"model={info.model_name!r} serial={info.serial_number!r} "
                f"size={info.width_cm}x{info.height_cm}cm ({diag and round(diag, 1)}\") year={info.year}"
            )
        t = time.perf_counter()
        hdr = displayconfig.get_hdr_state(p.adapter_id, p.target_id)
        print(f"   HDR         : {hdr}  ({(time.perf_counter() - t) * 1000:.1f} ms)")
        if g:
            print(f"   interfaces  : {gdi.enum_attached_monitor_interfaces(g.device_name)}")
            with ddc.open_physical_monitors(g.hmonitor) as phys:
                for pm in phys:
                    for name, code in (("brightness", ddc.VCP_BRIGHTNESS), ("contrast", ddc.VCP_CONTRAST)):
                        t = time.perf_counter()
                        try:
                            value = ddc.get_vcp(pm.handle, code)
                        except ddc.DdcError as e:
                            value = f"ERROR {e}"
                        print(f"   DDC {name:<10}: {value}  ({(time.perf_counter() - t) * 1000:.0f} ms) [{pm.description}]")


if __name__ == "__main__":
    main()
