"""
maestro_sim.py — Create, configure, run and read a Maestro (ADE Assembler) setup.

Driving Maestro from SKILL has a few sharp edges that produce *modal dialogs*
rather than errors — and a modal dialog blocks the whole Virtuoso session, so
every later call times out. This module encodes the working sequence:

  1. The maestro cellview must EXIST before it can be opened editable.
     `open_gui_session()` issues `deOpenCellView(... "maestro" ... "a")`, and
     mode "a" on a missing view pops a modal "Not Found" dbox. Create it with
     mode "w" first.

  2. Never open a second session while one is editing the same cellview —
     that is ERROR (ASSEMBLER-8127), also a modal dialog. Reuse the open
     editable session if there is one; otherwise purge before opening.

  3. Writes go through `maeMakeEditable()` first, and every write is READ BACK.
     `maeSetAnalysis` and friends return quietly when the setup is not
     editable, so an unverified write looks like success.

  4. The SKILL keyword is `?enable`, not `?enabled` (the latter raises
     "unrecognized keyword").

Usage:
    python tools/maestro_sim.py --lib EDA_Agent_Bench --cell inv_tb_ref \
        --stop 20n --net IN --net OUT \
        --measure "vout_at_2n5=value(VT(\\"/OUT\\") 2.5n)"

    python tools/maestro_sim.py --lib ... --cell ... --status
"""

import argparse
import sys
import time
from pathlib import Path

from virtuoso_bridge import VirtuosoClient
from virtuoso_bridge.virtuoso.maestro import (
    create_test, set_analysis, set_env_option, add_output, save_setup,
    run_and_wait, purge_maestro_cellviews,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MODEL_FILE = ("/home/PDK/Analog/TSMC_65/models/spectre/"
              "crn65gplus_2d5_lk_v1d0.scs")
MODEL_SECTION = "tt"


class MaestroError(RuntimeError):
    pass


def q(client, expr: str, timeout: int = 90) -> str:
    r = client.execute_skill(expr, timeout=timeout)
    return (getattr(r, "output", "") or "").strip()


def blocking_form(client) -> bool:
    """True if a modal form is displayed (it would stall every later call)."""
    out = q(client, "let((f) f=hiGetCurrentForm() "
                    "if(f && hiIsFormDisplayed(f) \"BLOCKED\" \"free\"))", 60)
    return "BLOCKED" in out


def assembler_windows(client, lib: str, cell: str) -> list:
    return [w for w in client.list_windows(timeout=None)
            if "Assembler" in w.get("name", "")
            and lib in w.get("name", "") and cell in w.get("name", "")]


def ensure_session(client, lib: str, cell: str, timeout: int = 180) -> str:
    """Return a session editing lib/cell/maestro, creating the view if needed."""
    if blocking_form(client):
        raise MaestroError(
            "A modal Virtuoso dialog is open and blocking SKILL. Dismiss it "
            "in the GUI before retrying.")

    sess = q(client, "car(maeGetSessions())").strip('"')
    wins = assembler_windows(client, lib, cell)

    # Reuse an already-editable session rather than opening a second one (8127).
    if sess and sess != "nil" and wins:
        q(client, "maeMakeEditable()")
        return sess

    # A window without a registered session is a stale lock holder.
    if wins and (not sess or sess == "nil"):
        raise MaestroError(
            f"An ADE Assembler window for {lib}/{cell} is open but no session "
            "is registered — it still holds the edit lock. Close that window "
            "in the GUI (answer No to any save prompt), then retry.")

    exists = q(client, f'ddGetObj("{lib}" "{cell}" "maestro") && t') == "t"
    if not exists:
        purge_maestro_cellviews(client)
        time.sleep(1)
        # mode "w" CREATES; mode "a" on a missing view pops a modal dbox.
        q(client, f'deOpenCellView("{lib}" "{cell}" "maestro" "maestro" nil "w")', 180)
    else:
        q(client, f'deOpenCellView("{lib}" "{cell}" "maestro" "maestro" nil "a")', timeout)
    time.sleep(4)

    if blocking_form(client):
        raise MaestroError("Opening the maestro view raised a modal dialog.")

    sess = q(client, "car(maeGetSessions())").strip('"')
    if not sess or sess == "nil":
        raise MaestroError(f"No maestro session after opening {lib}/{cell}")
    q(client, "maeMakeEditable()")
    return sess


def ensure_test(client, sess: str, lib: str, cell: str) -> str:
    test = f"{lib}_{cell}_1"
    setup = q(client, f'maeGetSetup(?session "{sess}")')
    if test not in setup:
        create_test(client, test, lib=lib, cell=cell, view="schematic",
                    simulator="spectre", session=sess)
        setup = q(client, f'maeGetSetup(?session "{sess}")')
        if test not in setup:
            raise MaestroError(f"create_test did not register {test}: {setup}")
    return test


def configure(client, sess: str, test: str, stop: str, nets, measures,
              model_file: str = MODEL_FILE, section: str = MODEL_SECTION):
    """Set model file, transient and outputs — verifying each write lands."""
    q(client, "maeMakeEditable()")

    set_env_option(client, test, f'(("modelFiles" (("{model_file}" "{section}"))))',
                   session=sess)
    got = q(client, f'maeGetEnvOption("{test}" ?option "modelFiles")')
    if model_file not in got:
        raise MaestroError(f"modelFiles did not take (got {got!r}). "
                           "Usually means the setup is not editable.")

    set_analysis(client, test, "tran",
                 options=f'(("stop" "{stop}") ("errpreset" "moderate"))',
                 session=sess)
    got = q(client, f'maeGetAnalysis("{test}" "tran")')
    if stop not in got:
        raise MaestroError(f"tran stop did not take (got {got!r})")

    for net in nets:
        add_output(client, net, test, output_type="net",
                   signal_name=f"/{net}", session=sess)
    for name, expr in measures:
        add_output(client, name, test, output_type="point", expr=expr,
                   session=sess)

    return True


def run(client, sess: str, timeout: int = 1200):
    history, status = run_and_wait(client, session=sess, timeout=timeout)
    return (history or "").strip('"'), status


def read_measures(client, test: str, history: str, measures):
    q(client, f'maeOpenResults(?history "{history}")', 120)
    q(client, "selectResult('tran)", 90)
    vals = {}
    for name, _ in measures:
        vals[name] = q(client, f'maeGetOutputValue("{name}" "{test}")', 90)
    return vals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", required=True)
    ap.add_argument("--cell", required=True)
    ap.add_argument("--stop", default="20n")
    ap.add_argument("--net", action="append", default=[])
    ap.add_argument("--measure", action="append", default=[],
                    help='name=SKILL-expr, e.g. \'v2=value(VT("/OUT") 2.5n)\'')
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--no-run", action="store_true")
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
        for w in assembler_windows(client, args.lib, args.cell):
            print("assembler window:", w["num"], w["name"][:70])
        return

    sess = ensure_session(client, args.lib, args.cell)
    print(f"session: {sess}")
    test = ensure_test(client, sess, args.lib, args.cell)
    print(f"test   : {test}")

    configure(client, sess, test, args.stop, args.net, measures)
    save_setup(client, args.lib, args.cell, session=sess)
    print("configured and saved")

    if args.no_run:
        return

    history, status = run(client, sess)
    print(f"run    : status={status} history={history}")
    if status != "done":
        raise MaestroError(f"simulation did not complete: {status}")

    vals = read_measures(client, test, history or "ExplorerRun.0", measures)
    for k, v in vals.items():
        print(f"  {k} = {v}")


if __name__ == "__main__":
    main()
