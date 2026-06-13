"""
virtuoso_api.py — Python wrapper over a live Cadence Virtuoso session.

All SKILL code is hidden behind clean Python methods. The LLM interacts
with Python only — it never writes raw SKILL directly.

The wrapper provides methods for:
  - Library/cell management (create, delete, list, open)
  - Schematic building (place instances, wires, pins, labels)
  - Schematic inspection (list instances, nets, pins, params)
  - Simulation control (run, read waveforms, DC operating points)
  - Layout queries (bounding box, area)
  - Utilities (pin coordinates, symbol generation, raw SKILL escape hatch)
"""

from virtuoso_bridge import VirtuosoClient

from eda_agent.config import DEFAULT_REF_LIB


class VirtuosoAPI:
    """Clean Python interface to Cadence Virtuoso via the SKILL bridge.

    Args:
        client: A connected VirtuosoClient instance.

    Attributes:
        client: The raw VirtuosoClient for native API access
                (client.schematic.edit(), client.layout.edit(), etc.)
    """

    def __init__(self, client: VirtuosoClient):
        self._client = client
        self.client = client  # exposed for native API access

        # Register a helper procedure to get the active cellview
        self._client.execute_skill(
            "procedure(_get_api_cv() "
            "if(geGetEditCellView() then geGetEditCellView() "
            "else if(boundp('apiCV) then apiCV else nil)))"
        )

    # ── Internal helpers ──────────────────────────────────────────────────

    def _skill(self, code: str) -> str:
        """Execute SKILL code, return output string. Raises on any error."""
        result = self._client.execute_skill(code)
        out = (result.output or "").strip()

        if result.errors:
            raise RuntimeError(f"SKILL error (CIW): {'; '.join(result.errors)}")
        if not result.ok:
            raise RuntimeError(f"SKILL execution failed: {out}")
        if out.startswith("*") or out.lower().startswith("error"):
            raise RuntimeError(f"SKILL error: {out}")

        return out

    def _parse_list(self, raw: str) -> list:
        """Parse a SKILL list string like ("a" "b" "c") into a Python list."""
        if not raw:
            return []
        cleaned = raw.strip().strip("()")
        return [item.strip().strip('"') for item in cleaned.split() if item.strip()]

    # ── Library & cell navigation ─────────────────────────────────────────

    def list_libraries(self) -> list:
        """Return names of all libraries currently open in Virtuoso."""
        raw = self._skill("mapcar(lambda((l) l~>name) ddGetLibList())")
        return self._parse_list(raw)

    def create_library(self, lib_name: str, ref_lib: str = DEFAULT_REF_LIB,
                       path: str = "") -> str:
        """Create a new library and attach it to a reference tech library.

        Args:
            lib_name: Name for the new library.
            ref_lib: Technology reference library (default: tsmcN65).
            path: Disk path. Defaults to ~/lib_name.

        Returns:
            The library name on success.
        """
        if path:
            path_expr = f'"{path}"'
        else:
            path_expr = f'strcat(getShellEnvVar("HOME") "/{lib_name}")'

        self._skill(
            f'let((lib) lib = ddCreateLib("{lib_name}" {path_expr}) '
            f'techBindTechFile(lib "{ref_lib}") lib~>name)'
        )
        return lib_name

    def delete_library(self, lib_name: str) -> None:
        """Delete a library and all its cells/views from disk."""
        self._skill(
            f'let((lib) lib = ddGetObj("{lib_name}") when(lib ddDeleteLib(lib)))'
        )

    def library_exists(self, lib_name: str) -> bool:
        """Check if a library is open in Virtuoso."""
        raw = self._skill(f'if(ddGetObj("{lib_name}") then "t" else "nil")')
        return raw.strip() == "t"

    def cell_exists(self, lib: str, cell: str) -> bool:
        """Check if a cell exists in the library."""
        raw = self._skill(f'if(ddGetObj("{lib}" "{cell}") then "t" else "nil")')
        return raw.strip() == "t"

    def list_cells(self, lib: str) -> list:
        """Return all cell names inside a library."""
        raw = self._skill(
            f'let((libObj) libObj = ddGetObj("{lib}") '
            f'unless(libObj error("Library {lib} not found")) '
            f'mapcar(lambda((c) c~>name) libObj~>cells))'
        )
        return self._parse_list(raw)

    def list_views(self, lib: str, cell: str) -> list:
        """Return all view names for a given cell."""
        raw = self._skill(
            f'let((cellObj) cellObj = ddGetObj("{lib}" "{cell}") '
            f'unless(cellObj error("Cell {lib}/{cell} not found")) '
            f'mapcar(lambda((v) v~>name) cellObj~>views))'
        )
        return self._parse_list(raw)

    def open_cellview(self, lib: str, cell: str, view: str = "schematic") -> str:
        """Open a cellview for reading. Returns the cell name."""
        raw = self._skill(
            f'let((cv) cv = dbOpenCellViewByType("{lib}" "{cell}" "{view}" "" "r") '
            f'unless(cv error("Cannot open {lib}/{cell}/{view}")) '
            f'apiCV = cv cv~>cellName)'
        )
        return raw.strip().strip('"')

    def create_schematic(self, lib: str, cell: str) -> str:
        """Create a new empty schematic cellview. Returns cell name."""
        raw = self._skill(
            f'let((cv) cv = dbOpenCellViewByType("{lib}" "{cell}" "schematic" "schematic" "w") '
            f'unless(cv error("Failed to create schematic for {lib}/{cell}")) '
            f'apiCV = cv dbSave(cv) cv~>cellName)'
        )
        return raw.strip().strip('"')

    def delete_cell(self, lib: str, cell: str) -> None:
        """Delete a cell and all its views."""
        self._skill(
            f'let((cellObj) cellObj = ddGetObj("{lib}" "{cell}") '
            f'when(cellObj ddDeleteObj(cellObj)))'
        )

    def delete_cellview(self, lib: str, cell: str, view: str) -> None:
        """Delete a specific cellview (e.g. 'schematic', 'symbol', 'layout')."""
        self._skill(
            f'let((cvObj) cvObj = ddGetObj("{lib}" "{cell}" "{view}") '
            f'when(cvObj ddDeleteObj(cvObj)))'
        )

    def open_window(self, lib: str, cell: str, view: str = "schematic") -> None:
        """Open a cellview in the Virtuoso GUI."""
        self._client.open_window(lib, cell, view=view)

    def current_cell(self) -> str:
        """Return the cell name of the active cellview."""
        return self._skill("_get_api_cv()~>cellName")

    def current_library(self) -> str:
        """Return the library name of the active cellview."""
        return self._skill("_get_api_cv()~>libName")

    def current_view(self) -> str:
        """Return the view name of the active cellview."""
        return self._skill("_get_api_cv()~>viewName")

    def save(self) -> None:
        """Save the current cellview."""
        self._skill("dbSave(_get_api_cv())")

    def close(self) -> None:
        """Close the current cellview without saving."""
        self._skill("dbClose(_get_api_cv())")

    def fit_to_window(self) -> None:
        """No-op: zoom-to-fit SKILL functions are unavailable in this environment."""
        pass

    # ── Schematic instance placement ──────────────────────────────────────

    def place_instance(self, inst_lib: str, inst_cell: str, inst_view: str,
                       x: float, y: float, name: str = "",
                       orient: str = "R0") -> str:
        """Place a component instance at (x, y).

        Args:
            orient: R0, R90, R180, R270, MX, MY, MXR90, MYR90

        Returns:
            The assigned instance name.
        """
        name_skill = f'"{name}"' if name else 'nil'
        raw = self._skill(
            f'let((cv inst) cv = _get_api_cv() '
            f'inst = schCreateInst(cv dbOpenCellViewByType("{inst_lib}" "{inst_cell}" "{inst_view}") '
            f'{name_skill} list({x} {y}) "{orient}") inst~>name)'
        )
        return raw.strip().strip('"')

    def place_instances_grid(self, components: list, cols: int = 3,
                             col_spacing: float = 2.0, row_spacing: float = 2.0,
                             origin_x: float = 0.0, origin_y: float = 0.0) -> list:
        """Place multiple instances in a tidy grid layout.

        Args:
            components: List of dicts with keys: lib, cell, view, name, orient.
            cols: Number of columns in the grid.

        Returns:
            List of placed instance names in order.
        """
        placed = []
        for i, comp in enumerate(components):
            x = origin_x + (i % cols) * col_spacing
            y = origin_y - (i // cols) * row_spacing
            name = self.place_instance(
                inst_lib=comp["lib"],
                inst_cell=comp["cell"],
                inst_view=comp.get("view", "symbol"),
                x=x, y=y,
                name=comp.get("name", ""),
                orient=comp.get("orient", "R0"),
            )
            placed.append(name)
        return placed

    def add_wire(self, x1: float, y1: float, x2: float, y2: float) -> None:
        """Draw a wire between two points in the current schematic."""
        self._skill(
            f'schCreateWire(_get_api_cv() "full" "draw" '
            f'list(list({x1} {y1}) list({x2} {y2})) 0.0625 0.0625 0)'
        )

    def add_pin(self, name: str, direction: str, x: float, y: float) -> None:
        """Add a pin (port). direction: 'input', 'output', 'inputOutput'."""
        self._skill(
            f'schCreatePin(_get_api_cv() "{name}" "{direction}" list({x} {y}))'
        )

    def add_wire_label(self, net_name: str, x: float, y: float) -> None:
        """Add a net label at (x, y) to name a wire/net."""
        self._skill(
            f'schCreateWireLabel(_get_api_cv() nil list({x} {y}) '
            f'"{net_name}" "lowerCenter" "R0" "stick" 0.0625 nil)'
        )

    def set_instance_param(self, inst_name: str, param: str, value: str) -> None:
        """Set a CDF parameter on a named instance.

        Uses cdfGetInstCDF + cdfUpdateInstParam so CDF callbacks fire correctly.
        """
        self._skill(
            f'let((cv inst iCDF p) cv = _get_api_cv() '
            f'inst = car(setof(x cv~>instances x~>name == "{inst_name}")) '
            f'unless(inst error("instance not found: {inst_name}")) '
            f'iCDF = cdfGetInstCDF(inst) '
            f'p = cdfFindParamByName(iCDF "{param}") '
            f'unless(p error("param not found: {param}")) '
            f'p~>value = {value} '
            f'cdfUpdateInstParam(inst) t)'
        )

    # ── Schematic inspection ──────────────────────────────────────────────

    def list_instances(self) -> list:
        """Return all instances: [{name, lib, cell, xy}, ...]."""
        raw = self._skill(
            'let((cv result) cv = _get_api_cv() result = "" '
            'foreach(inst cv~>instances result = strcat(result '
            'inst~>name "||" inst~>libName "||" inst~>cellName "||" '
            'anyToString(car(inst~>xy)) "||" anyToString(cadr(inst~>xy)) "\\n")) result)'
        )
        instances = []
        for line in raw.splitlines():
            parts = line.split("||")
            if len(parts) == 5:
                instances.append({
                    "name": parts[0].strip(),
                    "lib": parts[1].strip(),
                    "cell": parts[2].strip(),
                    "xy": [parts[3].strip(), parts[4].strip()],
                })
        return instances

    def list_nets(self) -> list:
        """Return all net names in the open schematic."""
        raw = self._skill(
            'let((cv) cv = _get_api_cv() mapcar(lambda((n) n~>name) cv~>nets))'
        )
        return self._parse_list(raw)

    def list_pins(self) -> list:
        """Return all pins: [{name, direction}, ...]."""
        raw = self._skill(
            'let((cv result) cv = _get_api_cv() result = "" '
            'foreach(pin cv~>terminals result = strcat(result '
            'pin~>name "||" pin~>direction "\\n")) result)'
        )
        pins = []
        for line in raw.splitlines():
            parts = line.split("||")
            if len(parts) == 2:
                pins.append({
                    "name": parts[0].strip(),
                    "direction": parts[1].strip(),
                })
        return pins

    def get_instance_params(self, inst_name: str) -> dict:
        """Return all CDF parameters of a named instance as a dict."""
        raw = self._skill(
            f'let((cv inst iCDF result) cv = _get_api_cv() '
            f'inst = car(setof(x cv~>instances x~>name == "{inst_name}")) '
            f'unless(inst error("instance not found: {inst_name}")) '
            f'iCDF = cdfGetInstCDF(inst) '
            f'result = "" '
            f'when(iCDF foreach(p iCDF~>parameters '
            f'result = strcat(result p~>name "=" anyToString(p~>value) "\\n"))) result)'
        )
        params = {}
        for line in raw.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                params[k.strip()] = v.strip()
        return params

    # ── Design variables ──────────────────────────────────────────────────

    def set_var(self, name: str, value) -> None:
        """Set a design variable."""
        self._skill(f'desVar("{name}" {value})')

    def get_var(self, name: str) -> str:
        """Get the current value of a design variable."""
        return self._skill(f'desVar("{name}")')

    # ── Simulation ────────────────────────────────────────────────────────

    def run_simulation(self) -> bool:
        """Run the currently configured Spectre simulation."""
        result = self._skill("asiRunSimulation()")
        return "t" in result.lower()

    def get_waveform_at(self, signal: str, time: float,
                        analysis: str = "tran") -> float:
        """Read a waveform value at a specific time point."""
        raw = self._skill(
            f'drGetWaveformYAtX(awvGetWaveform("{signal}" ?result "{analysis}") {time})'
        )
        return float(raw.strip())

    def get_dc_op(self, instance: str, param: str) -> float:
        """Read a DC operating point parameter from an instance."""
        raw = self._skill(f'getData("{instance}:{param}" ?result "dc")')
        return float(raw.strip())

    # ── Layout ────────────────────────────────────────────────────────────

    def get_bbox(self) -> dict:
        """Return bounding box of current layout: {x1, y1, x2, y2}."""
        raw = self._skill("_get_api_cv()~>bBox")
        nums = [float(x) for x in raw.replace('(', '').replace(')', '').split()]
        return {"x1": nums[0], "y1": nums[1], "x2": nums[2], "y2": nums[3]}

    def get_area(self) -> float:
        """Return layout area in µm²."""
        b = self.get_bbox()
        return (b["x2"] - b["x1"]) * (b["y2"] - b["y1"])

    # ── Utilities ─────────────────────────────────────────────────────────

    def get_instance_pin_xy(self, inst_name: str, pin_name: str) -> list:
        """Return [x, y] schematic coordinates of a pin on a placed instance."""
        skill_code = (
            "let((cv inst instTerm bb x1 y1 x2 y2 xc yc pt) "
            "cv = _get_api_cv() "
            f'inst = car(setof(x cv~>instances x~>name == "{inst_name}")) '
            f'instTerm = car(exists(x inst~>instTerms x~>name == "{pin_name}")) '
            "if(instTerm && instTerm~>term~>pins then "
            "bb = car(instTerm~>term~>pins~>fig~>bBox) "
            "if(bb && type(bb) == 'list && type(car(bb)) == 'list then "
            "x1=caar(bb) y1=cadar(bb) x2=caadr(bb) y2=cadadr(bb) "
            "xc=(x1+x2)/2.0 yc=(y1+y2)/2.0 "
            "pt = dbTransformPoint(list(xc yc) inst~>transform) "
            'sprintf(nil "%f %f" car(pt) cadr(pt)) '
            'else "nil nil") '
            'else "nil nil"))'
        )
        raw = self._skill(skill_code)
        parts = raw.strip().strip('"').split()
        if len(parts) != 2 or parts[0] == "nil":
            raise RuntimeError(
                f"Could not get pin coords for {inst_name}.{pin_name}. "
                f"Check names are correct. Raw: {raw!r}"
            )
        return [float(parts[0]), float(parts[1])]

    def create_symbol(self, lib: str, cell: str) -> str:
        """Auto-generate a symbol view using TSG.

        Uses schSchemToPinList → schPinListToSymbol internally.
        NEVER use schGenerateSymbol or raw SKILL for symbols.
        """
        self._client.execute_skill('schSetEnv("ssgSortPins" "geometric")')
        r = self._client.execute_skill(
            f'let((pl) pl = schSchemToPinList("{lib}" "{cell}" "schematic") '
            f'schPinListToSymbol("{lib}" "{cell}" "symbol" pl))'
        )
        if r.errors:
            raise RuntimeError(f"create_symbol: TSG failed: {'; '.join(r.errors)}")
        if not r.ok:
            raise RuntimeError(f"create_symbol: TSG failed: {(r.output or '').strip()}")

        views = self.list_views(lib, cell)
        if "symbol" not in views:
            raise RuntimeError(
                f"Symbol view not created for {lib}/{cell}. Check CIW."
            )
        return cell

    def ping(self) -> str:
        """Connection check — returns Virtuoso server time."""
        return self._skill("getCurrentTime()")

    def raw_skill(self, code: str) -> str:
        """Escape hatch — run raw SKILL code. Use sparingly."""
        return self._skill(code)


class SafeClientProxy:
    """Wraps VirtuosoClient so execute_skill() auto-raises on CIW errors.

    Used in the agent's exec() sandbox so LLM-generated code that calls
    client.execute_skill() directly gets the same error-checking as VirtuosoAPI.
    """

    def __init__(self, client: VirtuosoClient):
        self._client = client

    def __getattr__(self, name: str):
        return getattr(self._client, name)

    def execute_skill(self, code: str, **kwargs):
        result = self._client.execute_skill(code, **kwargs)
        if result.errors:
            raise RuntimeError(f"SKILL error (CIW): {'; '.join(result.errors)}")
        if not result.ok:
            raise RuntimeError(f"SKILL failed: {(result.output or '').strip()}")
        return result
