"""
api_reference.py — API documentation injected into the system prompt.

This is the complete reference that the LLM sees when generating code.
It documents all available `v.*` methods and `client.*` patterns with
usage examples and critical warnings.
"""

API_REFERENCE = """
## API USAGE HIERARCHY — FOLLOW THIS ORDER
1. **ALWAYS try `v` methods first.** They raise clear Python exceptions.
2. For creating/editing schematics or layouts → `client.schematic.edit()` / `client.layout.edit()`.
3. `client.execute_skill()` is LAST RESORT ONLY.
4. NEVER call any API and discard the result.

---

## VirtuosoAPI — available methods (object is `v`)

### Libraries & cells
- v.list_libraries() → list[str]
- v.library_exists(lib_name) → bool
- v.cell_exists(lib, cell) → bool
- v.create_library(lib_name, ref_lib="tsmcN65", path="") → str
- v.delete_library(lib_name) / v.delete_cell(lib, cell) / v.delete_cellview(lib, cell, view)
- v.list_cells(lib) / v.list_views(lib, cell) → list[str]
- v.open_cellview(lib, cell, view="schematic") → str
- v.create_schematic(lib, cell) → str
- v.current_cell() / v.current_library() / v.current_view() → str
- v.save() / v.close() / v.open_window(lib, cell, view="schematic")

### Schematic building
⚠️ These require an active cellview. PREFER client.schematic.edit() for new schematics.
- v.place_instance(inst_lib, inst_cell, inst_view, x, y, name="", orient="R0") → str
- v.place_instances_grid(components, cols=3, col_spacing=2.0, row_spacing=2.0) → list[str]
- v.add_wire(x1, y1, x2, y2)
- v.add_pin(name, direction, x, y)
- v.add_wire_label(net_name, x, y)
- v.set_instance_param(inst_name, param, value)

### Schematic inspection
- v.list_instances() → list[dict]   # keys: name, lib, cell, xy
- v.list_nets() → list[str]
- v.list_pins() → list[dict]        # keys: name, direction
- v.get_instance_params(inst_name) → dict

### Simulation
- v.set_var(name, value) / v.get_var(name) → str
- v.run_simulation() → bool
- v.get_waveform_at(signal, time, analysis="tran") → float
- v.get_dc_op(instance, param) → float

### Layout
- v.get_bbox() → dict   # keys: x1, y1, x2, y2
- v.get_area() → float  # µm²

### Utilities
- v.get_instance_pin_xy(inst_name, pin_name) → [x, y]
- v.create_symbol(lib, cell) → str  # TSG: schSchemToPinList → schPinListToSymbol
- v.ping() → str
- v.raw_skill(code) → str   # last resort only

## Native virtuoso-bridge-lite API (object is `client`)

### Schematic editing (preferred for new schematics)
⚠️ client.schematic.edit() SAVES AND CLOSES the cv on exit.
    Set CDF params AFTER the block with set_instance_params(client, inst, lib=..., cell=...).

```python
from virtuoso_bridge.virtuoso.schematic import (
    schematic_create_inst_by_master_name as inst,
    schematic_create_pin as pin,
    schematic_label_instance_term as label,
)
from virtuoso_bridge.virtuoso.schematic.params import set_instance_params

with client.schematic.edit(lib, cell, mode="w") as sch:
    sch.add(inst("tsmcN65", "nch", "symbol", "MN0", 0, 0, "R0"))
    sch.add(inst("tsmcN65", "pch", "symbol", "MP0", 0, 1.5, "MX"))
    sch.add_net_label_to_transistor("MN0",
        drain_net="OUT", gate_net="IN", source_net="VSS", body_net="VSS")
    sch.add_net_label_to_transistor("MP0",
        drain_net="OUT", gate_net="IN", source_net="VDD", body_net="VDD")
    sch.add(pin("IN", -1.5, 0.75, "R0", direction="input"))
    sch.add(pin("OUT", 1.5, 0.75, "R0", direction="output"))

# Set params AFTER the with-block (cv is closed)
set_instance_params(client, "MN0", lib=lib, cell=cell, w="210n", l="60n", nf="1")
set_instance_params(client, "MP0", lib=lib, cell=cell, w="420n", l="60n", nf="1")
v.open_window(lib, cell, view="schematic")
```

### Testbench example
⚠️ Do NOT add analogLib/gnd symbol — its terminal causes schCheck to fail.
    VSS net labels on source MINUS terminals are sufficient.

```python
with client.schematic.edit(tb_lib, tb_cell, mode="w") as sch:
    sch.add(inst(tb_lib, dut_cell, "symbol", "DUT", 0, 0, "R0"))
    sch.add(inst("analogLib", "vdc",    "symbol", "V_DC",   -4, 1, "R0"))
    sch.add(inst("analogLib", "vpulse", "symbol", "V_IN",   -4, -1, "R0"))
    sch.add(inst("analogLib", "cap",    "symbol", "C_LOAD",  4, 0, "R0"))
    sch.add(label("V_DC",   "PLUS",  "VDD"))
    sch.add(label("DUT",    "VDD",   "VDD"))
    sch.add(label("V_IN",   "PLUS",  "IN"))
    sch.add(label("DUT",    "IN",    "IN"))
    sch.add(label("DUT",    "OUT",   "OUT"))
    sch.add(label("C_LOAD", "PLUS",  "OUT"))
    sch.add(label("V_DC",   "MINUS", "VSS"))
    sch.add(label("V_IN",   "MINUS", "VSS"))
    sch.add(label("C_LOAD", "MINUS", "VSS"))
    sch.add(label("DUT",    "VSS",   "VSS"))

set_instance_params(client, "V_DC", lib=tb_lib, cell=tb_cell, vdc="1.2")
set_instance_params(client, "V_IN", lib=tb_lib, cell=tb_cell,
    v1="0", v2="1.2", per="1n", tr="10p", tf="10p", pw="0.5n", td="0")
set_instance_params(client, "C_LOAD", lib=tb_lib, cell=tb_cell, c="10f")
```

#### analogLib Component Parameters (VERIFIED)
⚠️ Use EXACT parameter names below — case-sensitive!

| Component | Terminals | Key Parameters | Example |
|-----------|-----------|----------------|---------|
| **vdc** | PLUS, MINUS | vdc | `vdc="1.2"` |
| **vpulse** | PLUS, MINUS | v1, v2, per, tr, tf, pw, td | `v1="0", v2="1.2", per="1n", tr="10p", tf="10p", pw="500p", td="0"` |
| **vsin** | PLUS, MINUS | vdc, ampl, freq | `vdc="0.6", ampl="100m", freq="1G"` |
| **idc** | PLUS, MINUS | idc | `idc="10u"` |
| **cap** | PLUS, MINUS | c | `c="10f"` |
| **res** | PLUS, MINUS | r | `r="1k"` |
| **ind** | PLUS, MINUS | l | `l="1n"` |

**CRITICAL vpulse params:** per (period), tr (rise time), tf (fall time), pw (pulse width), td (delay)
**NOT:** period, rise, fall, width, delay — those will fail with "param not found"

#### Unknown Parameters? Query them!
If you encounter "param not found" for any component, query its CDF parameters:
```python
# Query all parameters for an instance
params = client.execute_skill('''
let((cv inst params)
  cv = geGetEditCellView()
  inst = dbFindAnyInstByName(cv "INSTANCE_NAME")
  params = inst~>?? ; get all parameter names
  params)
''')
```

#### Terminal names & warnings
- All analogLib sources: PLUS, MINUS terminals
- gnd: DO NOT USE in testbenches (causes schCheck failure)
- Use net labels for VSS instead

### Layout editing
```python
from virtuoso_bridge.virtuoso.layout import (
    layout_create_rect as rect, layout_create_via_by_name as via,
)
with client.layout.edit(lib, cell, mode="w") as lay:
    lay.add(rect("M1", "drawing", 0, 0, 1, 0.5))
    lay.add(via("M1_M2", 0.5, 0.25))
```

### Schematic reading
```python
from virtuoso_bridge.virtuoso.schematic.reader import read_schematic
data = read_schematic(client, lib, cell)
# data["instances"], data["nets"], data["pins"]
```

### CDF parameter setting
⚠️ ALWAYS pass lib= and cell= explicitly.
```python
from virtuoso_bridge.virtuoso.schematic.params import set_instance_params
set_instance_params(client, "MP0", lib="myLib", cell="inv", w="500n", l="60n", nf="4")
```

### File transfer & screenshots
- client.upload_file(local_path, remote_path)
- client.download_file(remote_path, local_path)
- client.screenshot(output="output/", target="current")
- client.list_windows()
"""
