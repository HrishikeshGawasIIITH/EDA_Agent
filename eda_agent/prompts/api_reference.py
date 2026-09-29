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

To actually simulate a testbench, use the Maestro flow below — it is verified
end-to-end. Do NOT improvise with deOpenCellView; that route raises modal
dialogs which FREEZE the whole Virtuoso session until a human clicks them.

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
Ground rules — BOTH matter, and getting either wrong fails silently:
1. Do NOT place an analogLib/gnd symbol. Its floating terminal makes schCheck
   throw, and the whole `client.schematic.edit()` block then fails.
2. DO label every ground terminal with the global net `gnd!` (with the `!`).
   The `!` suffix makes it a global net, so it netlists to node 0 without any
   gnd symbol. Labelling ground `VSS` instead leaves the testbench with NO
   ground reference: schCheck still passes, but Spectre has no node 0 and the
   simulation fails or returns garbage.

```python
GND = "gnd!"
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
    sch.add(label("V_DC",   "MINUS", GND))
    sch.add(label("V_IN",   "MINUS", GND))
    sch.add(label("C_LOAD", "MINUS", GND))
    sch.add(label("DUT",    "VSS",   GND))

# param_filters=None is REQUIRED for source timing params. The default
# allowlist silently DROPS td/tr/tf/pw/per (it only warns), so without it
# the source keeps its default timing and the waveform is wrong.
set_instance_params(client, "V_DC", lib=tb_lib, cell=tb_cell,
    param_filters=None, vdc="1.2")
set_instance_params(client, "V_IN", lib=tb_lib, cell=tb_cell,
    param_filters=None,
    v1="0", v2="1.2", per="10n", tr="10p", tf="10p", pw="5n", td="0")
set_instance_params(client, "C_LOAD", lib=tb_lib, cell=tb_cell,
    param_filters=None, c="10f")

# Always confirm connectivity extracted, or netlisting dies later (OSSHNL-108):
cv = f'dbOpenCellView("{tb_lib}" "{tb_cell}" "schematic" "" "r")'
assert "t" in client.execute_skill(
    f'integerp({cv}~>connectivityLastUpdated)').output
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

### Running a simulation (Maestro / ADE) — VERIFIED FLOW

Every rule here exists because the obvious alternative pops a modal dialog and
hangs the session. Follow it exactly.

```python
LIB, CELL = tb_lib, tb_cell
TEST  = f"{LIB}_{CELL}_1"
MODEL = "/home/PDK/Analog/TSMC_65/models/spectre/crn65gplus_2d5_lk_v1d0.scs"
q = lambda e, t=180: client.execute_skill(e, timeout=t).output.strip()

# 1. CREATE + OPEN. maeOpenSetup creates the maestro view if missing, in a
#    BACKGROUND session — no GUI window, no dialog. Never use deOpenCellView:
#    mode "a" on a missing view pops a modal "Not Found"; mode "w" opens a GUI
#    window that later collides as ERROR (ASSEMBLER-8127).
#    (dbOpenCellViewByType(... "maestro" ... "w") silently returns nil.)
sess = q(f'maeOpenSetup("{LIB}" "{CELL}" "maestro")').strip('"')
S = f' ?session "{sess}"'

# 2. CONFIGURE in that background session. The identical writes silently no-op
#    in a GUI session (they return fine and every read-back is nil), so ALWAYS
#    read back. Note ?enable (not ?enabled) and the BACKQUOTED ?options alist.
q(f'maeCreateTest("{TEST}" ?lib "{LIB}" ?cell "{CELL}" ?view "schematic" '
  f'?simulator "spectre"{S})')
q(f'maeSetEnvOption("{TEST}" ?options `(("modelFiles" (("{MODEL}" "tt")))){S})')
q(f'maeSetAnalysis("{TEST}" "tran" ?enable t '
  f'?options `(("stop" "20n") ("errpreset" "moderate")){S})')
q(f'maeAddOutput("OUT" "{TEST}" ?outputType "net" ?signalName "/OUT"{S})')
assert "20n" in q(f'maeGetAnalysis("{TEST}" "tran")')          # verify!
q(f'maeSaveSetup(?lib "{LIB}" ?cell "{CELL}" ?view "maestro"{S})')

# 3. RUN
from virtuoso_bridge.virtuoso.maestro import run_and_wait
history, status = run_and_wait(client, session=sess, timeout=1200)
assert status == "done"

# 4. READ results — open the PSF DIRECTORY, not the Maestro history.
#    maeOpenResults returns t but often leaves the result context unset, so
#    selectResult('tran) -> nil and every value() is nil. That looks identical
#    to a failed simulation even though the data is on disk. The history name
#    is also not stable: maeRunSimulation writes "Interactive.N", the ADE GUI
#    writes "ExplorerRun.N". Find the psf dir instead and use OCEAN directly:
#      ~/simulation/<lib>/<cell>/maestro/results/maestro/<history>/<point>/<test>/psf
psf = ("/home/<user>/simulation/<LIB>/<CELL>/maestro/results/maestro/"
       "Interactive.1/1/<TEST>/psf")          # locate the newest one on disk
q(f'openResults("{psf}")')
assert "stdobj" in q("selectResult('tran)")   # nil here means nothing loaded
q("outputs()")                                # which nets were actually saved
vout = float(q('value(v("/OUT") 2.5n)'))      # direct waveform probe

# 5. TEAR DOWN — close WINDOWS first, then the session.
#    A run spawns ADE Explorer + Visualization & Analysis + Waveform windows,
#    which promotes the session to a UI session. After that maeCloseSession
#    ALWAYS fails, even with ?forceClose:
#      WARNING (EXPLORER-8051): ... maeCloseSession can be used to close only
#      those sessions that were opened using maeOpenSetup in the SKILL code.
q("maeCloseResults()")
for w in client.list_windows():
    if "Log:" in w["name"]:                 # never close the CIW
        continue
    if any(h in w["name"] for h in
           ("Visualization", "Waveform", "simulation/", "ADE Explorer")):
        q(f'hiCloseWindow(window({w["num"]}))')
q(f'maeCloseSession(?session "{sess}")')    # PLAIN — ?forceClose returns nil here
```

Sanity check for a 1.2 V inverter TB (vpulse v1=0 v2=1.2 td=0 tr=tf=10p pw=5n
per=10n): `v("/OUT")` ≈ 7e-06 while IN is high and ≈ 1.199987 while IN is low —
full rail-to-rail swing. If OUT sits near 0.6 V or is flat, the testbench has no
real ground (see the `gnd!` rule above).

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
