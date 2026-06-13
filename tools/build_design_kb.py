"""
build_design_kb.py — Process library_scan_dataset.json into a focused design KB.

Reads the scan dataset (produced by scan_libraries.py) and generates a
design_knowledge_base.md with:
  1. Testbench creation guide
  2. Real testbench examples from scanned libraries
  3. MOSFET sizing reference (Wp/Wn ≈ 2.27 rule)
  4. Master parameter catalog (tsmcN65 + analogLib)
  5. Component quick reference
  6. Observed transistor sizings and Wp/Wn ratios

Usage:
    python tools/build_design_kb.py                       # default paths
    python tools/build_design_kb.py input.json output.md  # custom paths
"""

import json
import sys
from pathlib import Path

# ── Constants ─────────────────────────────────────────────────────────────

NMOS_TYPES = {"nch", "nch_mac", "nch_hvt", "nch_lvt", "nch_ulvt"}
PMOS_TYPES = {"pch", "pch_mac", "pch_hvt", "pch_lvt", "pch_ulvt"}

# Priority order for parameter catalog
CATALOG_PRIORITY = [
    "tsmcN65/nch", "tsmcN65/pch", "tsmcN65/nch_mac", "tsmcN65/pch_mac",
    "tsmcN65/nch_hvt", "tsmcN65/pch_hvt", "tsmcN65/nch_lvt", "tsmcN65/pch_lvt",
    "analogLib/vdc", "analogLib/vpulse", "analogLib/vsin",
    "analogLib/idc", "analogLib/cap", "analogLib/res",
]


# ── KB builder ────────────────────────────────────────────────────────────

def build_kb(dataset):
    """Generate the design knowledge base markdown from the scan dataset."""
    lines = []
    lines.append("# Design Knowledge Base")
    lines.append(f"\n_Auto-generated from library_scan_dataset.json — "
                 f"{dataset.get('scan_timestamp', 'unknown')}_\n")

    # 1. Testbench creation guide
    lines.append("## 1. Testbench Creation Guide\n")
    lines.append("""\
**Step-by-step recipe:**

1. **Create library** (if needed): `v.create_library("myLib_tb", ref_lib="tsmcN65")`
2. **Create TB schematic**: Use `client.schematic.edit(tb_lib, tb_cell, mode="w")`
3. **Place DUT** (your custom cell as symbol)
4. **Place supply source** — `analogLib/vdc` for VDD (set `vdc="1.2"` for tsmcN65)
5. **Place input stimulus**:
   - Digital: `analogLib/vpulse` — v1="0", v2="1.2", period="2n", rise="10p", fall="10p", width="1n"
   - Analog AC: `analogLib/vsin` — vdc="0.6", ampl="100m", freq="1G"
6. **Place load cap** (optional): `analogLib/cap` — c="10f"
7. **Connect with net labels** — same label name = same net
   - DO NOT use analogLib/gnd symbol — causes schCheck failure
8. **Set params** with `set_instance_params(client, inst, lib=..., cell=...)`
9. **Save**: automatic on `client.schematic.edit()` exit

**CRITICAL:** All analogLib sources use PLUS and MINUS terminals.
vpulse CDF params: v1, v2, per, tr, tf, pw, td (NOT period, rise, fall, width, delay).
Body connections: NMOS body (B) → VSS, PMOS body (B) → VDD.
""")

    # 2. Real testbench examples
    lines.append("## 2. Testbench Examples from Scanned Libraries\n")
    tb_count = 0
    for lib_name, lib_data in dataset.get("libraries", {}).items():
        for cell_name, cell_data in lib_data.get("cells", {}).items():
            if not cell_data.get("is_testbench"):
                continue
            tb_count += 1
            if tb_count > 20:
                break

            lines.append(f"### `{lib_name}/{cell_name}` (testbench)")
            dut = cell_data.get("dut_instances", [])
            if dut:
                lines.append(f"**DUT:** {', '.join(dut)}")
            sources = cell_data.get("sources", [])
            if sources:
                lines.append("\n**Sources:**")
                for src in sources:
                    params = {k: v for k, v in src.get("params", {}).items()
                              if v and v not in ("nil", '""')}
                    p_str = ", ".join(f"{k}={v}" for k, v in params.items()) or "(defaults)"
                    lines.append(f"  - `{src['name']}` ({src['cell']}): {p_str}")
            pins = cell_data.get("pins", [])
            if pins:
                pin_str = ", ".join(f"{p['name']} ({p['direction']})" for p in pins)
                lines.append(f"\n**Pins:** {pin_str}")
            lines.append("")
        if tb_count > 20:
            lines.append("_... (additional testbenches omitted)_\n")
            break

    if tb_count == 0:
        lines.append("_No testbench cells found in scanned libraries._\n")

    # 3. MOSFET sizing reference
    lines.append("## 3. MOSFET Sizing Reference\n")
    lines.append("""\
**Balanced CMOS timing rule:** Wp/Wn ≈ **2.27** for equal rise/fall times.

| NMOS W | PMOS W (×2.27) | Use Case |
|--------|----------------|----------|
| 210n   | 476n ≈ 480n    | Minimum logic gate |
| 500n   | 1.135u ≈ 1.1u  | Medium drive |
| 1u     | 2.27u ≈ 2.2u   | High drive |
| 2u     | 4.54u ≈ 4.5u   | Strong driver |

**Key rules:**
- Minimum L = **60n** (60 nm). Never go below.
- CDF param is **`fingers`** not `nf` in tsmcN65 (nf is read-only)
- NMOS body (B) → VSS; PMOS body (B) → VDD
""")

    # Observed transistor sizings
    all_transistors = []
    for lib_name, lib_data in dataset.get("libraries", {}).items():
        for cell_name, cell_data in lib_data.get("cells", {}).items():
            for t in cell_data.get("transistors", []):
                if t.get("W") and t.get("L"):
                    all_transistors.append({**t, "lib": lib_name, "cell": cell_name})

    if all_transistors:
        # Deduplicate by type+W+L
        seen = set()
        unique = []
        for t in all_transistors:
            key = (t["type"], t.get("W", ""), t.get("L", ""))
            if key not in seen:
                seen.add(key)
                unique.append(t)

        lines.append("### Observed Transistor Sizings\n")
        lines.append("| Type | W | L | nf | m | Example Cell |")
        lines.append("|------|---|---|----|---|-------------|")
        for t in unique[:40]:
            lines.append(
                f"| {t['type']} | {t.get('W','')} | {t.get('L','')} "
                f"| {t.get('nf','')} | {t.get('m','')} | {t['lib']}/{t['cell']} |"
            )
        lines.append("")

    # Observed Wp/Wn ratios
    ratios = []
    for lib_name, lib_data in dataset.get("libraries", {}).items():
        for cell_name, cell_data in lib_data.get("cells", {}).items():
            r = cell_data.get("wp_wn_ratio")
            if r:
                ratios.append((lib_name, cell_name, r))

    if ratios:
        lines.append("### Observed Wp/Wn Ratios\n")
        lines.append("| Library | Cell | Wp/Wn | Assessment |")
        lines.append("|---------|------|-------|------------|")
        for lib_name, cell_name, ratio in ratios[:30]:
            if 2.0 <= ratio <= 2.5:
                note = "balanced ✓"
            elif ratio > 2.5:
                note = "PMOS heavy"
            else:
                note = "NMOS heavy (fast fall, slow rise)"
            lines.append(f"| {lib_name} | {cell_name} | {ratio} | {note} |")
        lines.append("")

    # 4. Master parameter catalog
    catalog = dataset.get("master_param_catalog", {})
    if catalog:
        lines.append("## 4. Master Component Parameter Catalog\n")
        ordered = [k for k in CATALOG_PRIORITY if k in catalog]
        ordered += sorted(k for k in catalog if k not in ordered)

        for key in ordered:
            params = catalog[key]
            if not params:
                continue
            lines.append(f"### `{key}`\n")
            lines.append("| Parameter | Default |")
            lines.append("|-----------|---------|")
            for pname, pinfo in sorted(params.items()):
                default = pinfo.get("default", "") if isinstance(pinfo, dict) else str(pinfo)
                lines.append(f"| `{pname}` | `{default}` |")
            lines.append("")

    # 5. Component quick reference
    lines.append("## 5. Component Quick Reference (tsmcN65 PDK)\n")
    lines.append("""\
| Component | lib | cell | view | Key Params | Example Values |
|-----------|-----|------|------|------------|----------------|
| NMOS | tsmcN65 | nch | symbol | w, l, fingers, m | w="210n", l="60n" |
| PMOS | tsmcN65 | pch | symbol | w, l, fingers, m | w="480n", l="60n" |
| Capacitor | analogLib | cap | symbol | c | c="100f" |
| Resistor | analogLib | res | symbol | r | r="10k" |
| DC voltage | analogLib | vdc | symbol | vdc | vdc="1.2" |
| Pulse src | analogLib | vpulse | symbol | v1,v2,per,tr,tf,pw,td | v2="1.2" |
| DC current | analogLib | idc | symbol | idc | idc="10u" |
| Sine src | analogLib | vsin | symbol | vdc,ampl,freq | freq="1G" |
""")

    # 6. Library summary
    libs = dataset.get("libraries", {})
    if libs:
        lines.append("## 6. Scanned Library Summary\n")
        lines.append("| Library | Cells | Testbenches | Transistors |")
        lines.append("|---------|-------|-------------|-------------|")
        for lib_name, lib_data in libs.items():
            cells = lib_data.get("cells", {})
            n_cells = len(cells)
            n_tb = sum(1 for c in cells.values() if c.get("is_testbench"))
            n_tr = sum(len(c.get("transistors", [])) for c in cells.values())
            lines.append(f"| {lib_name} | {n_cells} | {n_tb} | {n_tr} |")
        lines.append("")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    default_data = Path(__file__).resolve().parent.parent / "data"
    dataset_path = default_data / "library_scan_dataset.json"
    output_path = default_data / "design_knowledge_base.md"

    # Allow custom paths via CLI args
    if len(sys.argv) >= 2:
        dataset_path = Path(sys.argv[1])
    if len(sys.argv) >= 3:
        output_path = Path(sys.argv[2])

    if not dataset_path.exists():
        print(f"Dataset not found: {dataset_path}")
        print("Run tools/scan_libraries.py first to generate the dataset.")
        sys.exit(1)

    size_mb = dataset_path.stat().st_size / 1e6
    print(f"Loading dataset from {dataset_path} ({size_mb:.1f} MB)...")
    with open(dataset_path, "r", encoding="utf-8") as f:
        dataset = json.load(f)

    libs = dataset.get("libraries", {})
    total_cells = sum(len(l.get("cells", {})) for l in libs.values())
    total_tb = sum(
        1 for l in libs.values()
        for c in l.get("cells", {}).values() if c.get("is_testbench")
    )
    print(f"  {len(libs)} libraries, {total_cells} cells, {total_tb} testbenches")

    print("Generating design knowledge base...")
    kb_text = build_kb(dataset)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(kb_text, encoding="utf-8")
    print(f"Written to {output_path} ({len(kb_text) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
