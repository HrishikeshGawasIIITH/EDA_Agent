"""
scan_libraries.py — Scan Virtuoso libraries and build a design knowledge base.

Connects to a live Virtuoso session, scans all custom libraries (excluding
system libs like tsmcN65, analogLib), and extracts:
  - Instance usage per cell
  - Transistor sizing (W, L, fingers, m)
  - Wp/Wn ratios
  - Source/stimulus configurations (testbench detection)
  - CDF master parameter catalog

Outputs:
  1. library_scan_dataset.json  — Full machine-readable dataset
  2. Updates the knowledge base markdown with a SCANNED DESIGN DATA section

Usage:
    python tools/scan_libraries.py
    python tools/scan_libraries.py --output data/  # custom output directory
"""

import datetime
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load env from project root
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from virtuoso_bridge import VirtuosoClient, decode_skill_output
from virtuoso_bridge.virtuoso.schematic.reader import read_schematic


# ── Constants ─────────────────────────────────────────────────────────────

SYSTEM_LIBS = {"tsmcN65", "analogLib", "sample", "basic", "ahdlLib"}

TB_SOURCE_CELLS = {
    ("analogLib", "vdc"), ("analogLib", "vpulse"), ("analogLib", "idc"),
    ("analogLib", "isource"), ("analogLib", "vsin"), ("analogLib", "vsource"),
    ("analogLib", "vexp"), ("analogLib", "ipulse"), ("analogLib", "vcvs"),
    ("analogLib", "cccs"), ("analogLib", "port"),
}

NMOS_TYPES = {"nch", "nch_mac", "nch_hvt", "nch_lvt", "nch_ulvt"}
PMOS_TYPES = {"pch", "pch_mac", "pch_hvt", "pch_lvt", "pch_ulvt"}

KB_SECTION_HEADER = "## SCANNED DESIGN DATA"


# ── SKILL helpers ─────────────────────────────────────────────────────────

def _skill(client, code, timeout=30):
    """Execute SKILL, raise on failure, return decoded output."""
    result = client.execute_skill(code, timeout=timeout)
    if result.errors:
        raise RuntimeError(f"SKILL error: {'; '.join(result.errors)}")
    if not result.ok:
        raise RuntimeError("SKILL failed (no output)")
    return decode_skill_output(result.output or "")


def list_libraries(client):
    """Return all library names from the current Virtuoso session."""
    raw = _skill(client, '''
let((out)
  out = ""
  foreach(lib ddGetLibList()
    out = strcat(out lib~>name "\\n"))
  out)
''', timeout=20)
    return [l for l in raw.splitlines() if l.strip()]


def list_cells_with_views(client, lib):
    """Return {cell_name: [view_names]} for every cell in a library."""
    try:
        raw = _skill(client, f'''
let((libObj out)
  libObj = ddGetObj("{lib}")
  out = ""
  when(libObj
    foreach(cell libObj~>cells
      let((vnames)
        vnames = ""
        foreach(view cell~>views
          vnames = strcat(vnames view~>name " "))
        out = strcat(out sprintf(nil "%s|%s\\n" cell~>name vnames)))))
  out)
''', timeout=20)
    except RuntimeError:
        return {}

    result = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 1)
        cell_name = parts[0].strip()
        views = parts[1].split() if len(parts) > 1 else []
        result[cell_name] = views
    return result


def get_master_params(client, lib, cell):
    """Query CDF parameter definitions (defaults) for a master cell."""
    try:
        raw = _skill(client, f'''
let((cdf result)
  cdf = cdfGetBaseCellCDF(ddGetObj("{lib}" "{cell}"))
  result = ""
  when(cdf
    foreach(p cdf~>parameters
      when(p~>name
        result = strcat(result sprintf(nil "%s||%L\\n" p~>name p~>defaultValue)))))
  result)
''', timeout=20)
    except RuntimeError:
        return {}

    params = {}
    for line in raw.splitlines():
        parts = line.split("||", 1)
        if not parts or not parts[0].strip():
            continue
        pname = parts[0].strip()
        default = parts[1].strip().strip('"') if len(parts) > 1 else ""
        params[pname] = {"default": default}
    return params


# ── Sizing helper ─────────────────────────────────────────────────────────

def _to_meters(s):
    """Parse a SKILL-style value string like '210n' → float in meters."""
    s = s.strip().strip('"')
    if not s:
        return None
    try:
        suffixes = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "p": 1e-12, "f": 1e-15, "k": 1e3}
        for suffix, multiplier in suffixes.items():
            if s.endswith(suffix):
                return float(s[:-1]) * multiplier
        return float(s)
    except (ValueError, TypeError):
        return None


# ── Cell scanner ──────────────────────────────────────────────────────────

def scan_cell(client, lib, cell):
    """Read a schematic and extract structured data.

    Returns dict with instances, transistors, pins, nets, testbench info.
    """
    try:
        data = read_schematic(client, lib, cell, param_filters=None)
    except Exception as exc:
        return {
            "error": str(exc),
            "instances": [], "pins": [], "nets": [],
            "transistors": [], "sources": [], "is_testbench": False,
            "wp_wn_ratio": None,
        }

    raw_instances = data.get("instances", [])
    raw_nets = data.get("nets", {})
    raw_pins = data.get("pins", {})

    instances = []
    transistors = []
    sources = []
    dut_instances = []

    for ri in raw_instances:
        iname, ilib, icell = ri["name"], ri["lib"], ri["cell"]
        params = ri.get("params", {})
        terms = ri.get("terms", {})

        instances.append({
            "name": iname, "lib": ilib, "cell": icell,
            "params": params, "terms": terms,
        })

        # Track transistors
        if icell in (NMOS_TYPES | PMOS_TYPES):
            transistors.append({
                "name": iname, "type": icell, "lib": ilib,
                "W": params.get("w", params.get("W", "")),
                "L": params.get("l", params.get("L", "")),
                "nf": params.get("fingers", params.get("nf", "1")),
                "m": params.get("m", "1"),
            })

        # Track testbench sources
        if (ilib, icell) in TB_SOURCE_CELLS:
            sources.append({"name": iname, "cell": icell, "params": params})

        # Track DUT instances (non-system-lib components)
        if ilib not in SYSTEM_LIBS:
            dut_instances.append(iname)

    is_testbench = bool(sources)

    # Compute Wp/Wn ratio (first PMOS / first NMOS)
    wp_wn = None
    nmos = [t for t in transistors if t["type"] in NMOS_TYPES and t["W"]]
    pmos = [t for t in transistors if t["type"] in PMOS_TYPES and t["W"]]
    if nmos and pmos:
        try:
            wn = _to_meters(nmos[0]["W"])
            wp = _to_meters(pmos[0]["W"])
            if wn and wp and wn > 0:
                wp_wn = round(wp / wn, 3)
        except Exception:
            pass

    pins_list = [
        {"name": name, "direction": info["direction"]}
        for name, info in raw_pins.items()
    ]

    return {
        "is_testbench": is_testbench,
        "pins": pins_list,
        "nets": list(raw_nets.keys()),
        "instances": instances,
        "transistors": transistors,
        "sources": sources,
        "dut_instances": dut_instances if is_testbench else [],
        "wp_wn_ratio": wp_wn,
    }


# ── Top-level scan ────────────────────────────────────────────────────────

def scan_all(client):
    """Scan all custom libraries and return the full dataset dict."""
    all_libs = list_libraries(client)
    custom_libs = [l for l in all_libs if l not in SYSTEM_LIBS]

    print(f"  Found {len(all_libs)} libraries, {len(custom_libs)} custom.")
    if not custom_libs:
        print("  No custom libraries found.")
    else:
        print(f"  Scanning: {custom_libs}")

    dataset = {
        "scan_timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "system_libs_excluded": sorted(SYSTEM_LIBS),
        "master_param_catalog": {},
        "libraries": {},
    }

    total_cells = total_instances = 0

    for lib in custom_libs:
        print(f"\n  [{lib}]")
        cells_views = list_cells_with_views(client, lib)
        lib_data = {"cells": {}}

        for cell, views in cells_views.items():
            if "schematic" not in views:
                continue
            print(f"    {cell} ...", end=" ", flush=True)
            cell_data = scan_cell(client, lib, cell)
            lib_data["cells"][cell] = cell_data

            n = len(cell_data.get("instances", []))
            tb = " [TB]" if cell_data.get("is_testbench") else ""
            err = " [ERR]" if "error" in cell_data else ""
            print(f"{n} instances{tb}{err}")

        dataset["libraries"][lib] = lib_data
        n_cells = len(lib_data["cells"])
        n_insts = sum(len(c["instances"]) for c in lib_data["cells"].values())
        total_cells += n_cells
        total_instances += n_insts
        print(f"    → {n_cells} cells, {n_insts} instances")

    # Build master parameter catalog
    print("\n  Building master parameter catalog...")
    seen_masters = set()
    for lib_data in dataset["libraries"].values():
        for cell_data in lib_data["cells"].values():
            for inst in cell_data.get("instances", []):
                seen_masters.add((inst["lib"], inst["cell"]))

    for master_lib, master_cell in sorted(seen_masters):
        key = f"{master_lib}/{master_cell}"
        print(f"    CDF query: {key}")
        params = get_master_params(client, master_lib, master_cell)
        dataset["master_param_catalog"][key] = params

    print(f"\n  Scan complete: {len(custom_libs)} libs, "
          f"{total_cells} cells, {total_instances} instances.")
    return dataset


# ── KB section generator ──────────────────────────────────────────────────

def generate_kb_section(dataset):
    """Generate the SCANNED DESIGN DATA markdown section from the dataset."""
    lines = [KB_SECTION_HEADER]
    lines.append(f"\n_Auto-generated by scan_libraries.py — {dataset['scan_timestamp']}_\n")

    # 1. Master parameter catalog
    lines.append("### 1. Master Component Parameter Catalog")
    lines.append("Parameters accepted by each component. Use these exact param names.\n")

    catalog = dataset.get("master_param_catalog", {})
    priority = [
        "tsmcN65/nch", "tsmcN65/pch", "tsmcN65/nch_mac", "tsmcN65/pch_mac",
        "tsmcN65/nch_hvt", "tsmcN65/pch_hvt", "tsmcN65/nch_lvt", "tsmcN65/pch_lvt",
        "analogLib/vdc", "analogLib/vpulse", "analogLib/vsin",
        "analogLib/idc", "analogLib/cap", "analogLib/res",
    ]
    ordered = [m for m in priority if m in catalog]
    ordered += sorted(m for m in catalog if m not in ordered)

    for master_key in ordered:
        params = catalog[master_key]
        if not params:
            lines.append(f"#### `{master_key}`\n_No CDF parameters found._\n")
            continue
        lines.append(f"#### `{master_key}`")
        lines.append("| Parameter | Default |")
        lines.append("|-----------|---------|")
        for pname, pinfo in sorted(params.items()):
            default = pinfo.get("default", "") if isinstance(pinfo, dict) else str(pinfo)
            lines.append(f"| `{pname}` | `{default}` |")
        lines.append("")

    # 2. Transistor sizing reference
    lines.append("### 2. Transistor Sizing Reference")
    lines.append(
        "**Balanced CMOS timing rule:** Wp/Wn ≈ **2.27** for equal rise/fall.\n\n"
        "| NMOS W | PMOS W (×2.27) | Notes |\n"
        "|--------|----------------|-------|\n"
        "| 210n   | 476n ≈ 480n    | Minimum logic gate |\n"
        "| 500n   | 1.135u ≈ 1.1u  | Medium drive |\n"
        "| 1u     | 2.27u ≈ 2.2u   | High drive |\n"
        "| 2u     | 4.54u ≈ 4.5u   | Strong driver |\n"
    )

    # Observed transistor sizings from scanned designs
    all_transistors = []
    for lib_name, lib_data in dataset["libraries"].items():
        for cell_name, cell_data in lib_data["cells"].items():
            for t in cell_data.get("transistors", []):
                all_transistors.append({**t, "in_lib": lib_name, "in_cell": cell_name})

    if all_transistors:
        lines.append("#### Observed Transistor Sizings")
        lines.append("| Library | Cell | Instance | Type | W | L | nf | m |")
        lines.append("|---------|------|----------|------|---|---|----|---|")
        for t in all_transistors:
            lines.append(
                f"| {t['in_lib']} | {t['in_cell']} | {t['name']} "
                f"| {t['type']} | {t['W']} | {t['L']} "
                f"| {t.get('nf', '')} | {t.get('m', '')} |"
            )
        lines.append("")

    # Wp/Wn ratios
    ratios = [
        (lib_name, cell_name, cell_data["wp_wn_ratio"])
        for lib_name, lib_data in dataset["libraries"].items()
        for cell_name, cell_data in lib_data["cells"].items()
        if cell_data.get("wp_wn_ratio")
    ]
    if ratios:
        lines.append("#### Wp/Wn Ratios by Cell")
        lines.append("| Library | Cell | Wp/Wn | Assessment |")
        lines.append("|---------|------|-------|------------|")
        for lib_name, cell_name, ratio in ratios:
            if 2.0 <= ratio <= 2.5:
                note = "balanced ✓"
            elif ratio > 2.5:
                note = "PMOS heavy"
            else:
                note = "NMOS heavy (fast fall, slow rise)"
            lines.append(f"| {lib_name} | {cell_name} | {ratio} | {note} |")
        lines.append("")

    # 3. Testbench patterns
    lines.append("### 3. Testbench Patterns (from Scanned Designs)")
    has_tb = False
    for lib_name, lib_data in dataset["libraries"].items():
        for cell_name, cell_data in lib_data["cells"].items():
            if not cell_data.get("is_testbench"):
                continue
            has_tb = True
            lines.append(f"#### `{lib_name}/{cell_name}` (testbench)")
            if cell_data.get("dut_instances"):
                lines.append(f"**DUT:** {', '.join(cell_data['dut_instances'])}")
            srcs = cell_data.get("sources", [])
            if srcs:
                lines.append("\n**Sources:**")
                for src in srcs:
                    p_str = ", ".join(
                        f"{k}={val}" for k, val in src["params"].items()
                        if val and val not in ("nil", '""')
                    )
                    lines.append(f"  - `{src['name']}` (`{src['cell']}`): {p_str or '(defaults)'}")
            pins = cell_data.get("pins", [])
            if pins:
                pin_str = ", ".join(f"{p['name']} ({p['direction']})" for p in pins)
                lines.append(f"\n**Pins:** {pin_str}")
            lines.append("")
    if not has_tb:
        lines.append("_No testbench cells detected._\n")

    # 4. Per-library inventory
    lines.append("### 4. Per-Library Design Inventory\n")
    for lib_name, lib_data in dataset["libraries"].items():
        if not lib_data["cells"]:
            continue
        lines.append(f"#### Library: `{lib_name}`")
        for cell_name, cell_data in lib_data["cells"].items():
            if "error" in cell_data:
                lines.append(f"**`{cell_name}`** — error: {cell_data['error']}")
                continue
            pins_str = ", ".join(p["name"] for p in cell_data.get("pins", []))
            tb_tag = " `[TESTBENCH]`" if cell_data.get("is_testbench") else ""
            lines.append(f"**`{cell_name}`**{tb_tag} — ports: `{pins_str or 'none'}`")
            for inst in cell_data.get("instances", []):
                p_items = [
                    f"{k}={val}" for k, val in inst["params"].items()
                    if val and val not in ("nil", '""', "0")
                ]
                p_str = (", ".join(p_items))[:120]
                lines.append(
                    f"  - `{inst['name']}` ← `{inst['lib']}/{inst['cell']}`"
                    + (f"  ({p_str})" if p_str else "")
                )
            lines.append("")

    # 5. Component quick reference
    lines.append("### 5. Component Quick Reference (tsmcN65)")
    lines.append(
        "| Component | lib | cell | view | Key Params |\n"
        "|-----------|-----|------|------|------------|\n"
        "| NMOS | tsmcN65 | nch | symbol | w, l, fingers, m |\n"
        "| PMOS | tsmcN65 | pch | symbol | w, l, fingers, m |\n"
        "| Capacitor | analogLib | cap | symbol | c |\n"
        "| Resistor | analogLib | res | symbol | r |\n"
        "| DC voltage | analogLib | vdc | symbol | vdc |\n"
        "| Pulse src | analogLib | vpulse | symbol | v1,v2,per,tr,tf,pw,td |\n"
        "| DC current | analogLib | idc | symbol | idc |\n"
    )

    return "\n".join(lines)


# ── File I/O ──────────────────────────────────────────────────────────────

def save_dataset(dataset, output_dir):
    """Save the dataset as JSON."""
    path = output_dir / "library_scan_dataset.json"
    path.write_text(json.dumps(dataset, indent=2), encoding="utf-8")
    print(f"  Dataset saved: {path}")


def update_kb(section_text, kb_path):
    """Insert or replace the SCANNED DESIGN DATA section in the KB markdown."""
    if not kb_path.exists():
        kb_path.write_text(section_text + "\n", encoding="utf-8")
        print(f"  Created new KB: {kb_path}")
        return

    existing = kb_path.read_text(encoding="utf-8")

    if KB_SECTION_HEADER in existing:
        start = existing.index(KB_SECTION_HEADER)
        after = existing[start + len(KB_SECTION_HEADER):]
        next_h2 = after.find("\n## ")
        if next_h2 != -1:
            new_content = existing[:start] + section_text + "\n\n" + after[next_h2 + 1:]
        else:
            new_content = existing[:start] + section_text + "\n"
    else:
        new_content = existing.rstrip() + "\n\n" + section_text + "\n"

    kb_path.write_text(new_content, encoding="utf-8")
    print(f"  KB updated: {kb_path}")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Scan Virtuoso libraries and build design KB")
    parser.add_argument("--output", "-o", type=str, default="data",
                        help="Output directory for dataset and KB (default: data/)")
    args = parser.parse_args()

    output_dir = Path(__file__).resolve().parent.parent / args.output
    output_dir.mkdir(parents=True, exist_ok=True)
    kb_path = output_dir / "virtuoso_bridge_knowledge_base.md"

    print("=" * 60)
    print("Virtuoso Library Scanner")
    print("=" * 60)

    print("\nConnecting to Virtuoso...")
    try:
        client = VirtuosoClient.from_env()
        t = client.execute_skill("getCurrentTime()")
        print(f"Connected  |  Server time: {decode_skill_output(t.output or '').strip()}")
    except Exception as exc:
        print(f"Connection failed: {exc}")
        print("Make sure Virtuoso is running with 'virtuoso-bridge start'.")
        sys.exit(1)

    print("\nScanning libraries...")
    dataset = scan_all(client)

    print("\nSaving outputs...")
    save_dataset(dataset, output_dir)
    section = generate_kb_section(dataset)
    update_kb(section, kb_path)

    # Print summary
    libs = dataset["libraries"]
    total_cells = sum(len(l["cells"]) for l in libs.values())
    total_insts = sum(
        len(c.get("instances", []))
        for l in libs.values() for c in l["cells"].values()
    )
    total_tb = sum(
        1 for l in libs.values()
        for c in l["cells"].values() if c.get("is_testbench")
    )

    print(f"\n{'=' * 60}")
    print(f"  Libraries  : {len(libs)}")
    print(f"  Cells      : {total_cells}")
    print(f"  Instances  : {total_insts}")
    print(f"  Testbenches: {total_tb}")
    print(f"  Masters    : {len(dataset['master_param_catalog'])}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
