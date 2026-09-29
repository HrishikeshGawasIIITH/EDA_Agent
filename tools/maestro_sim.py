"""
maestro_sim.py — Create, configure, run and read a Maestro (ADE Assembler) setup.

This encodes a flow verified end-to-end against a live Cadence Virtuoso IC23.1
session with no popups and no manual intervention. The obvious API calls are not
the ones that work; each rule below cost a frozen session to find.

  1. CREATE with `maeOpenSetup(lib, cell, "maestro")` — never `deOpenCellView`.
     `deOpenCellView(... "maestro" ... "a")` on a cell with no maestro view pops a
     modal "Not Found" dbox; mode "w" creates it but opens a GUI window that later
     collides as ERROR (ASSEMBLER-8127). `dbOpenCellViewByType(... "w")` silently
     returns nil — a maestro view is not an ordinary database cellview.
     `maeOpenSetup` creates it in a background session, no window, no dialog.

  2. CONFIGURE in that background session. The same writes silently no-op in a
     GUI session: they return without error and every read-back is nil. Here they
     apply first try, with no `maeMakeEditable`. Every write is still read back.
     The keyword is `?enable` (not `?enabled`); `?options` takes a BACKQUOTED alist.

  3. TEAR DOWN after a run by closing WINDOWS, not the session. `maeRunSimulation`
     spawns ADE Explorer + Visualization & Analysis + Waveform windows, which
     promotes the background session into a UI session — after which
     `maeCloseSession` always fails, even with ?forceClose:
       WARNING (EXPLORER-8051): ... maeCloseSession can be used to close only
       those sessions that were opened using maeOpenSetup in the SKILL code.
     Closing the ADE Explorer window drops maeGetSessions() back to nil.

Usage:
    python tools/maestro_sim.py --lib EDA_Agent_Bench --cell dbg_inv_tb \
        --stop 20n --net IN --net OUT \
        --measure 'vout_in_high=value(VT(\\"/OUT\\") 2.5n)' \
        --probe 'IN@2.5n' --probe 'OUT@2.5n' --probe 'OUT@7.5n'

    python tools/maestro_sim.py --lib ... --cell ... --status
    python tools/maestro_sim.py --lib ... --cell ... --teardown
"""

import argparse
import re
import sys
import time
from pathlib import Path

from virtuoso_bridge import VirtuosoClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MODEL_FILE = ("/home/PDK/Analog/TSMC_65/models/spectre/"
              "crn65gplus_2d5_lk_v1d0.scs")
MODEL_SECTION = "tt"

# Windows that a run spawns and that teardown must close.
RUN_WINDOW_HINTS = ("Visualization", "Waveform", "simulation/", "ADE Explorer",
                    "ADE Assembler")


class MaestroError(RuntimeError):
    pass


def q(client, expr: str, timeout: int = 120) -> str:
    r = client.execute_skill(expr, timeout=timeout)
    return (getattr(r, "output", "") or "").strip()


def blocking_form(client) -> bool:
    out = q(client, 'let((f) f=hiGetCurrentForm() '
                    'if(f && hiIsFormDisplayed(f) "BLOCKED" "free"))', 60)
    return "BLOCKED" in out


def open_setup(client, lib: str, cell: str) -> str:
    """Create (if needed) and open the maestro setup in a background session."""
    if blocking_form(client):
        raise MaestroError(
            "A modal Virtuoso dialog is open and blocking SKILL. Dismiss it in "
            "the GUI before retrying.")
    sess = q(client, f'maeOpenSetup("{lib}" "{cell}" "maestro")', 240).strip('"')
    if not sess or sess == "nil":
        raise MaestroError(f"maeOpenSetup failed for {lib}/{cell}")
    return sess


def configure(client, sess: str, lib: str, cell: str, stop: str, nets, measures,
              model_file: str = MODEL_FILE, section: str = MODEL_SECTION) -> str:
    """Create the test and set model/analysis/outputs, verifying each write."""
    test = f"{lib}_{cell}_1"
    S = f' ?session "{sess}"'

    setup = q(client, f'maeGetSetup(?session "{sess}")')
    if test not in setup:
        q(client, f'maeCreateTest("{test}" ?lib "{lib}" ?cell "{cell}" '
                  f'?view "schematic" ?simulator "spectre"{S})', 180)
        setup = q(client, f'maeGetSetup(?session "{sess}")')
        if test not in setup:
            raise MaestroError(f"maeCreateTest did not register {test}: {setup}")

    q(client, f'maeSetEnvOption("{test}" ?options '
              f'`(("modelFiles" (("{model_file}" "{section}")))){S})', 180)
    got = q(client, f'maeGetEnvOption("{test}" ?option "modelFiles")')
    if model_file not in got:
        raise MaestroError(f"modelFiles did not take (got {got!r})")

    q(client, f'maeSetAnalysis("{test}" "tran" ?enable t '
              f'?options `(("stop" "{stop}") ("errpreset" "moderate")){S})', 180)
    got = q(client, f'maeGetAnalysis("{test}" "tran")')
    if stop not in got:
        raise MaestroError(f"tran stop did not take (got {got!r})")
    if "tran" not in q(client, f'maeGetEnabledAnalysis("{test}")'):
        raise MaestroError("tran analysis is not enabled")

    for net in nets:
        q(client, f'maeAddOutput("{net}" "{test}" ?outputType "net" '
                  f'?signalName "/{net}"{S})', 120)
    for name, expr in measures:
        q(client, f'maeAddOutput("{name}" "{test}" ?outputType "point" '
                  f'?expr "{expr}"{S})', 120)

    q(client, f'maeSaveSetup(?lib "{lib}" ?cell "{cell}" ?view "maestro"{S})', 180)
    return test


def run(client, sess: str, timeout: int = 1800):
    from virtuoso_bridge.virtuoso.maestro import run_and_wait
    history, status = run_and_wait(client, session=sess, timeout=timeout)
    return (history or "").strip('"') or "ExplorerRun.0", status


def find_psf_dir(lib: str, cell: str, test: str = "") -> str:
    """Locate the newest PSF directory holding transient data for lib/cell.

    Results live under
      ~/simulation/<lib>/<cell>/maestro/results/maestro/<history>/<point>/<test>/psf
    and the <history> name is NOT stable — a run started by maeRunSimulation is
    "Interactive.N" while one started from the ADE GUI is "ExplorerRun.N", so it
    cannot be hardcoded. Find it on disk by looking for tran data instead.
    """
    import os
    import subprocess

    user = os.environ.get("VB_REMOTE_USER", "")
    host = os.environ.get("VB_REMOTE_HOST", "")
    if not (user and host):
        return ""
    root = f"~/simulation/{lib}/{cell}/maestro/results"
    # newest tran.tran.tran first; its parent dir is the psf directory
    cmd = (f"ls -1dt $(find {root} -name 'tran.tran.tran' -printf '%h\\n' "
           f"2>/dev/null | sort -u) 2>/dev/null | head -1")
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
             f"{user}@{host}", cmd],
            capture_output=True, text=True, timeout=90)
        return out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    except Exception:
        return ""


def read_results(client, test: str, history: str, measures, probes,
                 lib: str = "", cell: str = ""):
    """Read measured outputs and waveform probes after a run.

    Reads the PSF directory directly with OCEAN `openResults`. The Maestro route
    (`maeOpenResults` + `selectResult`) is unreliable: it returns t but leaves
    the result context unset, so every later `value(v(...))` comes back nil —
    which looks exactly like a failed simulation even when the data is on disk.
    """
    vals = {}
    psf = find_psf_dir(lib, cell, test) if lib and cell else ""
    if psf:
        vals["psf_dir"] = psf
        q(client, f'openResults("{psf}")', 180)
    else:
        q(client, f'maeOpenResults(?history "{history}")', 180)

    sel = q(client, "selectResult('tran)", 120)
    if "stdobj" not in sel:
        vals["error"] = f"no transient result loaded (selectResult -> {sel!r})"
        return vals

    vals["saved_nets"] = q(client, "outputs()")
    for name, _ in measures:
        vals[name] = q(client, f'maeGetOutputValue("{name}" "{test}")', 120)
    for probe in probes:
        m = re.match(r"\s*([\w/]+)\s*@\s*([\w.]+)\s*$", probe)
        if not m:
            vals[probe] = "unparsed (expected NET@TIME, e.g. OUT@2.5n)"
            continue
        net, t = m.groups()
        vals[probe] = q(client, f'value(v("/{net.lstrip("/")}") {t})', 120)
    return vals


def teardown(client, lib: str = "", cell: str = "") -> dict:
    """Close result viewers and the ADE window — the only way to end a run session.

    maeCloseSession cannot close a session that a run promoted to a UI session
    (EXPLORER-8051), so close the windows instead.
    """
    info = {}
    if lib and cell:
        sess = q(client, "car(maeGetSessions())").strip('"')
        if sess and sess != "nil":
            info["save"] = q(client, f'maeSaveSetup(?lib "{lib}" ?cell "{cell}" '
                                     f'?view "maestro" ?session "{sess}")', 180)
    try:
        info["closeResults"] = q(client, "maeCloseResults()", 120)
    except Exception as exc:
        info["closeResults"] = f"skipped ({type(exc).__name__})"

    for w in client.list_windows(timeout=90):
        name = w.get("name", "")
        if "Log:" in name:                       # never close the CIW
            continue
        if any(h in name for h in RUN_WINDOW_HINTS):
            try:
                info[f'close_{w["num"]}'] = q(
                    client, f'hiCloseWindow(window({w["num"]}))', 120)
            except Exception as exc:
                # a window can vanish when its parent closes — not an error
                info[f'close_{w["num"]}'] = f"gone ({type(exc).__name__})"
    time.sleep(2)

    # Closing the windows demotes the session back to a closable one, but its
    # registration lingers until maeCloseSession is called — PLAIN, not
    # ?forceClose (force returns nil here and leaves the session listed).
    for _ in range(3):
        sess = q(client, "car(maeGetSessions())").strip('"')
        if not sess or sess == "nil":
            break
        info[f"closeSession_{sess}"] = q(
            client, f'maeCloseSession(?session "{sess}")', 120)
        time.sleep(2)

    info["sessions"] = q(client, "maeGetSessions()")
    info["windows"] = len(client.list_windows(timeout=90))
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", required=True)
    ap.add_argument("--cell", required=True)
    ap.add_argument("--stop", default="20n")
    ap.add_argument("--net", action="append", default=[])
    ap.add_argument("--measure", action="append", default=[],
                    help='name=SKILL-expr, e.g. \'v=value(VT("/OUT") 2.5n)\'')
    ap.add_argument("--probe", action="append", default=[],
                    help="NET@TIME read straight off the waveform, e.g. OUT@2.5n")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--teardown", action="store_true")
    ap.add_argument("--no-run", action="store_true")
    ap.add_argument("--keep-open", action="store_true",
                    help="leave the ADE window open for manual inspection")
    args = ap.parse_args()

    measures = []
    for m in args.measure:
        name, _, expr = m.partition("=")
        measures.append((name.strip(), expr.strip()))

    client = VirtuosoClient.from_env()
    print(f"connected: {q(client, 'getCurrentTime()')}")

    if args.status:
        print("sessions        :", q(client, "maeGetSessions()"))
        print("maestro on disk :",
              q(client, f'ddGetObj("{args.lib}" "{args.cell}" "maestro") && t'))
        print("blocking dialog :", blocking_form(client))
        for w in client.list_windows(timeout=90):
            print("window          :", w["num"], w["name"][:66])
        return

    if args.teardown:
        for k, v in teardown(client, args.lib, args.cell).items():
            print(f"  {k}: {v}")
        return

    sess = open_setup(client, args.lib, args.cell)
    print(f"session : {sess}")
    test = configure(client, sess, args.lib, args.cell, args.stop,
                     args.net, measures)
    print(f"test    : {test}  (configured and saved)")

    if args.no_run:
        return

    history, status = run(client, sess)
    print(f"run     : status={status} history={history}")
    if status != "done":
        raise MaestroError(f"simulation did not complete: {status}")

    for k, v in read_results(client, test, history, measures, args.probe,
                             lib=args.lib, cell=args.cell).items():
        print(f"  {k} = {v}")

    if not args.keep_open:
        print("teardown:")
        for k, v in teardown(client, args.lib, args.cell).items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
