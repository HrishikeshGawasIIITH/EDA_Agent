"""
create_maestro.py — Minimal, step-by-step Maestro (ADE Assembler) setup creator.

Deliberately dumb and linear: every SKILL call is printed with its result and
its errors, so a failing step is obvious. No reuse heuristics, no retries.

    # print the SKILL without touching Virtuoso (paste-able into the CIW)
    python tools/create_maestro.py --lib EDA_Agent_Bench --cell inv_tb_ref --dry-run

    # actually execute, step by step
    python tools/create_maestro.py --lib EDA_Agent_Bench --cell inv_tb_ref

    # stop before creating, just report what's open
    python tools/create_maestro.py --lib EDA_Agent_Bench --cell inv_tb_ref --status

What bit us repeatedly, in case a step misbehaves:

  * `deOpenCellView(... "maestro" ... "a")` on a cell with NO maestro view pops a
    modal "Not Found" dbox. Mode "w" CREATES the view. A modal dialog blocks all
    SKILL, so every later call then times out.
  * Opening a second session while one is already editing the same cellview is
    ERROR (ASSEMBLER-8127) — also modal. Close the ADE window first, or reuse it.
  * The analysis keyword is `?enable`. `?enabled` raises "unrecognized keyword".
  * `?options` takes a BACKQUOTED alist: ?options `(("stop" "20n"))
  * Writes silently no-op when the setup is not editable — call maeMakeEditable()
    first and READ BACK every write.
"""

import argparse

from virtuoso_bridge import VirtuosoClient

MODEL = "/home/PDK/Analog/TSMC_65/models/spectre/crn65gplus_2d5_lk_v1d0.scs"
SECTION = "tt"


def steps(lib: str, cell: str, stop: str, nets, measures):
    """Return the ordered (label, skill) pairs that build the setup."""
    test = f"{lib}_{cell}_1"
    out = [
        ("0. what is already open (read-only)", "maeGetSessions()"),
        ("0b. does the maestro view exist?",
         f'ddGetObj("{lib}" "{cell}" "maestro") && t'),
        ("0c. is a modal dialog blocking SKILL?",
         'let((f) f=hiGetCurrentForm() '
         'if(f && hiIsFormDisplayed(f) "BLOCKED" "free"))'),

        # mode "w" creates the view; "a" would pop a modal "Not Found"
        ("1. create the maestro cellview",
         f'deOpenCellView("{lib}" "{cell}" "maestro" "maestro" nil "w")'),
        ("2. session that was just opened", "car(maeGetSessions())"),
        ("3. make the setup editable", "maeMakeEditable()"),

        ("4. create the test bound to the schematic",
         f'maeCreateTest("{test}" ?lib "{lib}" ?cell "{cell}" '
         f'?view "schematic" ?simulator "spectre")'),
        ("5. read back the test list", "maeGetSetup()"),

        ("6. point the test at the PDK model file",
         f'maeSetEnvOption("{test}" ?options `(("modelFiles" (("{MODEL}" "{SECTION}")))))'),
        ("7. read back modelFiles",
         f'maeGetEnvOption("{test}" ?option "modelFiles")'),

        # NOTE: ?enable, not ?enabled
        ("8. enable the transient analysis",
         f'maeSetAnalysis("{test}" "tran" ?enable t '
         f'?options `(("stop" "{stop}") ("errpreset" "moderate")))'),
        ("9. read back the analysis", f'maeGetAnalysis("{test}" "tran")'),
        ("9b. read back enabled analyses", f'maeGetEnabledAnalysis("{test}")'),
    ]

    for net in nets:
        out.append((f"10. save waveform for /{net}",
                    f'maeAddOutput("{net}" "{test}" ?outputType "net" '
                    f'?signalName "/{net}")'))
    for name, expr in measures:
        out.append((f"11. measured output {name}",
                    f'maeAddOutput("{name}" "{test}" ?outputType "point" '
                    f'?expr "{expr}")'))

    out += [
        ("12. save the setup to disk",
         f'maeSaveSetup(?lib "{lib}" ?cell "{cell}" ?view "maestro")'),
        ("13. run the simulation (async; returns a history name)",
         "maeRunSimulation()"),
        ("14. run mode / status", "maeGetCurrentRunMode()"),
    ]
    return test, out


def result_steps(test: str, history: str = "Interactive.0"):
    return [
        ("15. open the results", f'maeOpenResults(?history "{history}")'),
        ("16. select the transient", "selectResult('tran)"),
        ("17. which nets were saved", "outputs()"),
        ("18. read a measured output",
         f'maeGetOutputValue("vout_in_high" "{test}")'),
        ("19. or query the waveform directly", 'value(v("/OUT") 2.5n)'),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", required=True)
    ap.add_argument("--cell", required=True)
    ap.add_argument("--stop", default="20n")
    ap.add_argument("--net", action="append", default=["IN", "OUT"])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the SKILL only; do not contact Virtuoso")
    ap.add_argument("--status", action="store_true",
                    help="run only the read-only step 0 checks")
    ap.add_argument("--stop-after", type=int, default=None,
                    help="execute only the first N steps")
    args = ap.parse_args()

    measures = [
        ("vout_in_high", 'value(VT(\\"/OUT\\") 2.5n)'),
        ("vout_in_low",  'value(VT(\\"/OUT\\") 7.5n)'),
    ]
    test, plan = steps(args.lib, args.cell, args.stop, args.net, measures)

    if args.dry_run:
        print(f"# Maestro setup for {args.lib}/{args.cell}   (test: {test})")
        print("# Paste these into the CIW one at a time, in order.\n")
        for label, skill in plan:
            print(f"; {label}")
            print(f"{skill}\n")
        print("; ---- after the run finishes ----")
        for label, skill in result_steps(test):
            print(f"; {label}")
            print(f"{skill}\n")
        return

    client = VirtuosoClient.from_env()
    if args.status:
        plan = plan[:3]
    elif args.stop_after:
        plan = plan[:args.stop_after]

    for label, skill in plan:
        print(f"\n--- {label}")
        print(f"    SKILL: {skill[:150]}")
        try:
            r = client.execute_skill(skill, timeout=180)
            out = (getattr(r, "output", "") or "").strip()
            errs = getattr(r, "errors", []) or []
            print(f"    ->     {out[:200] if out else '(empty)'}")
            if errs:
                print(f"    ERRORS {errs[:2]}")
                print("    stopping — fix this step before continuing")
                return
        except Exception as exc:
            print(f"    RAISED {type(exc).__name__}: {str(exc)[:220]}")
            print("    stopping — fix this step before continuing")
            return


if __name__ == "__main__":
    main()
