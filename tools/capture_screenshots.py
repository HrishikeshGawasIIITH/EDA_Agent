"""
capture_screenshots.py — Capture Virtuoso schematic screenshots for the docs.

Opens each requested cellview in the live Virtuoso session, sizes the window,
zooms to fit, grabs a screenshot through the bridge, and crops away the editor
side panels so the README shows the schematic rather than the chrome.

Note: the bridge's `fit_to_window()` helper is a documented no-op in this
environment. The schematic editor's own `schZoomFit(xMargin yMargin window)` does
work — argument order is two numbers first, then the window.

Usage:
    python tools/capture_screenshots.py --cells EDA_Agent_Bench:inv,EDA_Agent_Bench:nand2
    python tools/capture_screenshots.py --lib EDA_Agent_Bench --all
    python tools/capture_screenshots.py --steps        # build-sequence frames
"""

import argparse
import sys
import time
from pathlib import Path

from PIL import Image
from virtuoso_bridge import VirtuosoClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eda_agent.virtuoso_api import VirtuosoAPI, SafeClientProxy

OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "media"

WIN_W, WIN_H = 1500, 1000
# Canvas rect inside the schematic editor window, measured at the size above:
# left of x0 is the Navigator/Property panels, above y0 is the toolbar.
CROP = (200, 140, 10, 55)   # left, top, right-inset, bottom-inset


def size_window(client):
    client.execute_skill(
        f'hiResizeWindow(hiGetCurrentWindow() list(0:0 {WIN_W}:{WIN_H}))', timeout=20)
    time.sleep(1.5)


def zoom_fit(client):
    try:
        client.execute_skill('schZoomFit(0.9 0.9 hiGetCurrentWindow())', timeout=20)
    except Exception:
        # Symbol editor doesn't take schZoomFit; fall back to a fixed scale.
        client.execute_skill('hiZoomAbsoluteScale(hiGetCurrentWindow() 4.0)',
                             timeout=20)
    time.sleep(1.0)
    client.execute_skill('hiRedraw(hiGetCurrentWindow())', timeout=15)
    time.sleep(0.8)


def crop_canvas(path: Path) -> Path:
    """Trim the editor panels so only the schematic canvas remains."""
    im = Image.open(path).convert("RGB")
    left, top, right_inset, bottom_inset = CROP
    box = (min(left, im.width - 1), min(top, im.height - 1),
           max(left + 1, im.width - right_inset),
           max(top + 1, im.height - bottom_inset))
    im.crop(box).save(path)
    return path


def trim_to_content(path: Path, pad: int = 28, win: int = 11,
                    min_density: int = 6) -> Path:
    """Crop away empty canvas around the drawn circuit.

    The schematic grid is drawn as isolated bright dots on a near-black field,
    so a plain brightness threshold selects the entire canvas. Grid dots are
    single pixels on a coarse lattice while circuit strokes and text are dense,
    so content is found by local density instead: a pixel counts as content only
    if enough bright pixels fall inside a small window around it.
    """
    import numpy as np

    im = Image.open(path).convert("RGB")
    a = np.asarray(im).astype(np.int16)
    bright = (a.max(axis=2) > 90).astype(np.int32)

    # Integral image -> count of bright pixels in each win x win neighbourhood.
    integral = bright.cumsum(axis=0).cumsum(axis=1)
    integral = np.pad(integral, ((1, 0), (1, 0)))
    r = win // 2
    h, w = bright.shape
    ys, xs = np.mgrid[0:h, 0:w]
    y0 = np.clip(ys - r, 0, h)
    y1 = np.clip(ys + r + 1, 0, h)
    x0 = np.clip(xs - r, 0, w)
    x1 = np.clip(xs + r + 1, 0, w)
    counts = (integral[y1, x1] - integral[y0, x1]
              - integral[y1, x0] + integral[y0, x0])

    mask = counts >= min_density
    ys_c, xs_c = np.where(mask)
    if len(xs_c) == 0:
        return path

    box = (max(0, xs_c.min() - pad), max(0, ys_c.min() - pad),
           min(im.width, xs_c.max() + pad), min(im.height, ys_c.max() + pad))
    im.crop(box).save(path)
    return path


def _resolve_window(client, lib, cell, view):
    """Return the window number showing lib/cell/view, or raise.

    `hiGetCurrentWindow()` is the fast path, but it comes back `nil` for some
    editors (the symbol editor among them), so fall back to scanning the open
    window list by title.
    """
    for _ in range(3):
        win = client.execute_skill('hiGetCurrentWindow()', timeout=15)
        win_s = (getattr(win, "output", "") or "").strip()
        if ":" in win_s:
            num = win_s.split(":")[-1]
            title = client.execute_skill(
                'hiGetWindowName(hiGetCurrentWindow())', timeout=15)
            title_s = (getattr(title, "output", "") or "").strip().strip('"')
            if cell in title_s and lib in title_s:
                return int(num), title_s
        time.sleep(1.0)

    for w in client.list_windows(timeout=None):
        name = w.get("name", "")
        if lib in name and cell in name and view in name:
            return int(w["num"]), name

    raise RuntimeError(f"no open window showing {lib}/{cell} ({view})")


# Virtuoso's viewType for a symbol view is "schematicSymbol"; the bridge
# defaults it to "symbol", and geOpen then fails silently (no window, no error).
VIEW_TYPES = {"symbol": "schematicSymbol"}


def capture(client, v, lib, cell, out_name, view="schematic", crop=True,
            trim=True) -> Path:
    """Open a cellview, frame it, and screenshot *that* window.

    Screenshotting by view name (target="schematic") grabs whichever schematic
    window the bridge finds first, which silently captures the wrong cell once
    more than one editor window is open. Target the window number this open
    actually produced, and verify its title before trusting the image.
    """
    view_type = VIEW_TYPES.get(view)
    if view_type:
        client.open_window(lib, cell, view=view, view_type=view_type)
    else:
        v.open_window(lib, cell, view=view)
    time.sleep(2.0)

    num, _title = _resolve_window(client, lib, cell, view)

    size_window(client)
    zoom_fit(client)

    out = OUT_DIR / out_name
    out.parent.mkdir(parents=True, exist_ok=True)
    client.screenshot(output=str(out), target=num, timeout=None)
    time.sleep(0.5)

    # Close it so the next capture can't pick up a stale window.
    client.execute_skill(f'hiCloseWindow(hiGetCurrentWindow())', timeout=15)
    time.sleep(0.8)

    if crop and out.exists():
        crop_canvas(out)
        if trim:
            trim_to_content(out)
    return out


def build_steps(client, v, lib="EDA_Agent_Demo", cell="inv_steps"):
    """Build an inverter one step at a time, screenshotting after each.

    Produces the frames behind the "watch it build" animation. Each frame is a
    real screenshot of the schematic as it stood at that point.
    """
    from virtuoso_bridge.virtuoso.schematic import (
        schematic_create_inst_by_master_name as inst,
        schematic_create_pin as pin,
    )
    from virtuoso_bridge.virtuoso.schematic.params import set_instance_params

    if not v.library_exists(lib):
        v.create_library(lib, ref_lib="tsmcN65")
    client.execute_skill(
        f'when(ddGetObj("{lib}" "{cell}") ddDeleteObj(ddGetObj("{lib}" "{cell}")))',
        timeout=30)

    frames = []

    def shot(n, label):
        p = capture(client, v, lib, cell, f"step_{n}_{label}.png", trim=n > 1)
        frames.append(p)
        print(f"  frame {n}: {label} -> {p.name}")

    # 1. empty cellview
    with client.schematic.edit(lib, cell, mode="w") as sch:
        pass
    shot(1, "empty")

    # 2. transistors placed
    with client.schematic.edit(lib, cell, mode="a") as sch:
        sch.add(inst("tsmcN65", "nch", "symbol", "MN0", 0, 0, "R0"))
        sch.add(inst("tsmcN65", "pch", "symbol", "MP0", 0, 1.5, "MX"))
    shot(2, "devices")

    # 3. nets labelled
    with client.schematic.edit(lib, cell, mode="a") as sch:
        sch.add_net_label_to_transistor("MN0", drain_net="OUT", gate_net="IN",
                                        source_net="VSS", body_net="VSS")
        sch.add_net_label_to_transistor("MP0", drain_net="OUT", gate_net="IN",
                                        source_net="VDD", body_net="VDD")
    shot(3, "nets")

    # 4. pins added
    with client.schematic.edit(lib, cell, mode="a") as sch:
        sch.add(pin("IN", -1.5, 0.75, "R0", direction="input"))
        sch.add(pin("OUT", 1.5, 0.75, "R0", direction="output"))
        sch.add(pin("VDD", 0, 3.0, "R0", direction="inputOutput"))
        sch.add(pin("VSS", 0, -1.5, "R0", direction="inputOutput"))
    shot(4, "pins")

    # 5. sized
    set_instance_params(client, "MN0", lib=lib, cell=cell,
                        w="210n", l="60n", fingers="1")
    set_instance_params(client, "MP0", lib=lib, cell=cell,
                        w="480n", l="60n", fingers="1")
    shot(5, "sized")

    # 6. symbol generated
    v.create_symbol(lib, cell)
    p = capture(client, v, lib, cell, "step_6_symbol.png", view="symbol")
    frames.append(p)
    print(f"  frame 6: symbol -> {p.name}")

    return frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cells", help="comma list of lib:cell[:view]")
    ap.add_argument("--lib", help="library for --all")
    ap.add_argument("--all", action="store_true", help="capture every cell in --lib")
    ap.add_argument("--steps", action="store_true", help="build-sequence frames")
    ap.add_argument("--no-crop", action="store_true")
    args = ap.parse_args()

    raw = VirtuosoClient.from_env()
    v = VirtuosoAPI(raw)
    client = SafeClientProxy(raw)
    print(f"Connected: {raw.execute_skill('getCurrentTime()').output.strip()}")

    if args.steps:
        build_steps(client, v)
        return

    targets = []
    if args.all and args.lib:
        for cell in v.list_cells(args.lib):
            targets.append((args.lib, cell, "schematic"))
    if args.cells:
        for spec in args.cells.split(","):
            parts = spec.split(":")
            targets.append((parts[0], parts[1],
                            parts[2] if len(parts) > 2 else "schematic"))

    for lib, cell, view in targets:
        try:
            out = capture(client, v, lib, cell, f"{cell}_{view}.png", view=view,
                          crop=not args.no_crop)
            print(f"  {lib}/{cell} ({view}) -> {out.name}")
        except Exception as exc:
            print(f"  {lib}/{cell} ({view}) FAILED: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
