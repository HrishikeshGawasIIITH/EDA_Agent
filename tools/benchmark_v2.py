"""
benchmark_v2.py — Round-trip benchmark with simulation-verified success.

Two differences from the first benchmark, both of which make the numbers mean
more:

  * **Success is proven by simulation, not by existence.** v1 counted a task as
    successful when its cellviews existed. That was too weak: it passed an
    inverter testbench that had no `gnd!` net and therefore no ground reference,
    which could never have simulated. Here the harness builds the Maestro setup
    itself, runs a transient, and checks the measured node voltages land in a
    physically sensible range.

  * **Multi-stage tasks.** The simple cells saturate at the 2-round-trip floor,
    so they cannot show a difference. The `sim` tasks ask for a cell AND a
    testbench AND a transient AND a reported measurement, which is where the
    round-trips actually accumulate.

Round-trips counted are the AGENT's `send_message` calls only. The harness's own
verification simulation is not part of that — it is scoring, not solving.

    python tools/benchmark_v2.py                         # cold + grounded
    python tools/benchmark_v2.py --only sim --tasks 3
    python tools/benchmark_v2.py --conditions grounded
"""

import argparse
import contextlib
import io
import json
import shutil
import sys
import time
from pathlib import Path

from virtuoso_bridge import VirtuosoClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eda_agent import agent as agent_mod
from eda_agent.agent import run_agent_cycle
from eda_agent.cli import _build_chat_session, _get_kb, _load_design_context
from eda_agent.config import DATA_DIR, ERROR_LOG_PATH
from eda_agent.prompts import build_system_prompt
from eda_agent.virtuoso_api import VirtuosoAPI, SafeClientProxy
from maestro_sim import (open_setup, configure, run as mae_run, read_results,
                         teardown, blocking_form, q as skill)

LIB = "EDA_Agent_Bench"

# A dead bridge or a busy Virtuoso is NOT the agent getting the circuit wrong.
# Runs that hit these are recorded as infra_error and excluded from success
# rates, rather than being counted against the condition under test.
INFRA_MARKERS = (
    "SKILL execution timeout",
    "maeOpenSetup failed",
    "Connection refused",
    "not reachable",
    "Socket timeout",
    "Broken pipe",
    "no route to host",
)


def is_infra(detail: str) -> bool:
    d = (detail or "").lower()
    return any(m.lower() in d for m in INFRA_MARKERS)


def bridge_alive(client) -> bool:
    try:
        return bool(skill(client, "getCurrentTime()", 45))
    except Exception:
        return False

VDD = 1.2
# A CMOS output must sit within 5% of a rail; anything mid-rail means the
# testbench has no real ground or the device is not switching.
LOW = (-0.05 * VDD, 0.05 * VDD)
HIGH = (0.95 * VDD, 1.05 * VDD)

SIMPLE = [
    ("inv", True, "Create a CMOS inverter cell named 'inv' in library EDA_Agent_Bench "
     "using tsmcN65 nch and pch. Wp=480n, Wn=210n, L=60n. Pins IN, OUT, VDD, VSS. "
     "Generate the symbol view."),
    ("nand2", True, "Create a 2-input CMOS NAND gate 'nand2' in EDA_Agent_Bench: two "
     "series NMOS, two parallel PMOS, L=60n, Wn=420n, Wp=480n. Pins A, B, OUT, VDD, "
     "VSS. Generate the symbol."),
    ("nor2", True, "Create a 2-input CMOS NOR gate 'nor2' in EDA_Agent_Bench: two "
     "parallel NMOS, two series PMOS, L=60n, Wn=210n, Wp=960n. Pins A, B, OUT, VDD, "
     "VSS. Generate the symbol."),
    ("tgate", True, "Create a CMOS transmission gate 'tgate' in EDA_Agent_Bench with an "
     "nch and pch in parallel, L=60n, W=240n each. Pins IN, OUT, EN, ENB. Symbol too."),
    ("cs_amp", True, "Create a common-source amplifier 'cs_amp' in EDA_Agent_Bench: one "
     "nch (W=2u, L=200n) with a 10k analogLib resistor as drain load. Pins IN, OUT, "
     "VDD, VSS. Generate the symbol."),
    ("cmirror", True, "Create an NMOS current mirror 'cmirror' in EDA_Agent_Bench with "
     "two matched nch devices (W=1u, L=500n) sharing a gate, diode-connected input "
     "branch. Pins IREF, IOUT, VSS. Generate the symbol."),
    ("cascode_mirror", True, "Create a cascode NMOS current mirror 'cascode_mirror' in "
     "EDA_Agent_Bench using four nch devices (W=1u, L=500n). Pins IREF, IOUT, VBIAS, "
     "VSS. Generate the symbol."),
    ("diffpair", True, "Create a differential pair 'diffpair' in EDA_Agent_Bench: two "
     "matched nch inputs (W=4u, L=180n) with a shared tail nch current source (W=8u, "
     "L=500n). Pins INP, INN, OUTP, OUTN, VBIAS, VDD, VSS. Generate the symbol."),
    ("ota_5t", True, "Create a 5-transistor OTA 'ota_5t' in EDA_Agent_Bench: nch "
     "differential pair (W=4u, L=180n), pch mirror load (W=8u, L=180n), nch tail "
     "(W=8u, L=500n). Pins INP, INN, OUT, VBIAS, VDD, VSS. Generate the symbol."),
    ("ring_osc_3", False, "Create a 3-stage ring oscillator 'ring_osc_3' in "
     "EDA_Agent_Bench by instantiating the existing EDA_Agent_Bench/inv symbol three "
     "times in a ring. Pins OUT, VDD, VSS."),
    ("ring_osc_5", False, "Create a 5-stage ring oscillator 'ring_osc_5' in "
     "EDA_Agent_Bench by instantiating the EDA_Agent_Bench/inv symbol five times in a "
     "ring. Pins OUT, VDD, VSS."),
    ("inv_tb", False, "Create a testbench schematic 'inv_tb' in EDA_Agent_Bench for the "
     "EDA_Agent_Bench/inv cell: a vpulse driving IN (v1=0, v2=1.2, td=0, tr=10p, "
     "tf=10p, pw=5n, per=10n), a 1.2V vdc supply on VDD, a 10f cap on OUT, and proper "
     "ground. It must pass schCheck with connectivityLastUpdated set to an integer."),
]

# (cell, tb, prompt, stop, probes) — probes: (NET@TIME, expected_range)
SIM = [
    ("sinv", "sinv_tb",
     "In library EDA_Agent_Bench: (1) build a CMOS inverter cell 'sinv' (tsmcN65 nch "
     "Wn=210n, pch Wp=480n, L=60n; pins IN, OUT, VDD, VSS) and generate its symbol; "
     "(2) build a testbench 'sinv_tb' instantiating that symbol, driven by a vpulse on "
     "IN (v1=0, v2=1.2, td=0, tr=10p, tf=10p, pw=5n, per=10n) with a 1.2V vdc on VDD "
     "and a 10f load cap on OUT, grounded so it can actually simulate; (3) run a 20ns "
     "transient in Maestro and report the voltage on OUT at 2.5ns and at 7.5ns.",
     "20n", [("OUT@2.5n", LOW), ("OUT@7.5n", HIGH), ("IN@2.5n", HIGH)]),

    ("snand", "snand_tb",
     "In library EDA_Agent_Bench: (1) build a 2-input CMOS NAND 'snand' (two series "
     "nch Wn=420n, two parallel pch Wp=480n, L=60n; pins A, B, OUT, VDD, VSS) with a "
     "symbol; (2) build testbench 'snand_tb' holding both A and B HIGH at 1.2V using "
     "vdc sources, a 1.2V supply on VDD, a 10f cap on OUT, properly grounded; (3) run "
     "a 20ns transient and report OUT at 10ns. With both inputs high a NAND output "
     "must be LOW.",
     "20n", [("OUT@10n", LOW)]),

    ("snor", "snor_tb",
     "In library EDA_Agent_Bench: (1) build a 2-input CMOS NOR 'snor' (two parallel "
     "nch Wn=210n, two series pch Wp=960n, L=60n; pins A, B, OUT, VDD, VSS) with a "
     "symbol; (2) build testbench 'snor_tb' holding both A and B LOW at 0V with vdc "
     "sources, a 1.2V supply on VDD, a 10f load cap, properly grounded; (3) run a 20ns "
     "transient and report OUT at 10ns. With both inputs low a NOR output must be HIGH.",
     "20n", [("OUT@10n", HIGH)]),

    ("sbuf", "sbuf_tb",
     "In library EDA_Agent_Bench: (1) build a non-inverting buffer 'sbuf' as two "
     "cascaded CMOS inverter stages in one cell (each stage nch Wn=210n / pch Wp=480n, "
     "L=60n; pins IN, OUT, VDD, VSS) with a symbol; (2) build testbench 'sbuf_tb' "
     "driving IN with a vpulse (v1=0, v2=1.2, td=0, tr=10p, tf=10p, pw=5n, per=10n), "
     "1.2V on VDD, 10f load, properly grounded; (3) run a 20ns transient and report "
     "OUT at 2.5ns and 7.5ns. A buffer output must FOLLOW its input, not invert it.",
     "20n", [("OUT@2.5n", HIGH), ("OUT@7.5n", LOW)]),

    ("stgate", "stgate_tb",
     "In library EDA_Agent_Bench: (1) build a CMOS transmission gate 'stgate' (nch and "
     "pch in parallel, W=240n, L=60n; pins IN, OUT, EN, ENB) with a symbol; (2) build "
     "testbench 'stgate_tb' with EN tied to 1.2V and ENB tied to 0V so the gate is ON, "
     "IN driven by a 1.2V vdc, a 10f cap on OUT, properly grounded; (3) run a 20ns "
     "transient and report OUT at 15ns. With the gate enabled OUT must reach IN.",
     "20n", [("OUT@15n", HIGH)]),

    ("sinv_w", "sinv_w_tb",
     "In library EDA_Agent_Bench: (1) build a wide CMOS inverter 'sinv_w' (nch "
     "Wn=1u, pch Wp=2u, L=60n; pins IN, OUT, VDD, VSS) with a symbol; (2) build "
     "testbench 'sinv_w_tb' with IN held LOW at 0V by a vdc source, 1.2V on VDD, a "
     "50f load cap, properly grounded; (3) run a 20ns transient and report OUT at "
     "15ns. With the input low the output must be pulled to the supply rail.",
     "20n", [("OUT@15n", HIGH)]),
]


class CountingSession:
    """Wraps a chat session and counts send_message calls (round-trips)."""

    def __init__(self, inner):
        self._inner = inner
        self.round_trips = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def send_message(self, text: str):
        self.round_trips += 1
        resp = self._inner.send_message(text)
        self.input_tokens += getattr(resp, "input_tokens", 0) or 0
        self.output_tokens += getattr(resp, "output_tokens", 0) or 0
        return resp


def views_of(client, cell):
    out = skill(client, f'mapcar(lambda((x) x~>name) ddGetObj("{LIB}" "{cell}")~>views)')
    return [w.strip('"') for w in out.strip("()").split() if w.strip('"')]


def conn_ok(client, cell):
    return skill(client, f'integerp(dbOpenCellView("{LIB}" "{cell}" "schematic" "" "r")'
                         f'~>connectivityLastUpdated)') == "t"


def nets_of(client, cell):
    out = skill(client, f'mapcar(lambda((n) n~>name) '
                        f'dbOpenCellView("{LIB}" "{cell}" "schematic" "" "r")~>nets)')
    return [w.strip('"') for w in out.strip("()").split() if w.strip('"')]


def drop_cells(client, cells):
    for c in cells:
        skill(client, f'when(ddGetObj("{LIB}" "{c}") ddDeleteObj(ddGetObj("{LIB}" "{c}")))')


def verify_simple(client, cell, needs_symbol):
    if not views_of(client, cell):
        return False, "cell not created"
    views = views_of(client, cell)
    if "schematic" not in views:
        return False, f"no schematic ({views})"
    if needs_symbol and "symbol" not in views:
        return False, f"no symbol ({views})"
    if not conn_ok(client, cell):
        return False, "schCheck unclean (connectivityLastUpdated not an integer)"
    return True, f"views={views}"


def verify_sim(client, cell, tb, stop, probes):
    """Prove the design works by simulating it — the harness's own run."""
    if "schematic" not in views_of(client, cell):
        return False, "cell has no schematic", {}
    if "schematic" not in views_of(client, tb):
        return False, "testbench not created", {}
    if not conn_ok(client, tb):
        return False, "testbench schCheck unclean", {}

    nets = nets_of(client, tb)
    if not any(n.endswith("!") for n in nets):
        return False, f"testbench has no global ground net (nets={nets})", {}

    nets_wanted = sorted({p.split("@")[0] for p, _ in probes})
    try:
        teardown(client, LIB, tb)
        sess = open_setup(client, LIB, tb)
        test = configure(client, sess, LIB, tb, stop, nets_wanted, [])
        history, status = mae_run(client, sess, timeout=1800)
        if status != "done":
            return False, f"simulation status={status}", {}
        vals = read_results(client, test, history, [], [p for p, _ in probes],
                            lib=LIB, cell=tb)
    except Exception as exc:
        return False, f"sim error: {type(exc).__name__}: {str(exc)[:130]}", {}
    finally:
        with contextlib.suppress(Exception):
            teardown(client, LIB, tb)

    measured, bad = {}, []
    for probe, (lo, hi) in probes:
        raw = vals.get(probe, "")
        try:
            val = float(raw)
        except (TypeError, ValueError):
            bad.append(f"{probe}=unreadable({raw!r})")
            continue
        measured[probe] = val
        if not (lo <= val <= hi):
            bad.append(f"{probe}={val:.4g} outside [{lo:.3g},{hi:.3g}]")
    if bad:
        return False, "; ".join(bad), measured
    return True, "; ".join(f"{k}={v:.4g}" for k, v in measured.items()), measured


def run_one(task, kind, condition, v, client, kb, design_kb, prompts, provider):
    if kind == "simple":
        cell, needs_symbol, prompt = task
        cells = [cell]
    else:
        cell, tb, prompt, stop, probes = task
        cells = [cell, tb]

    drop_cells(client, cells)

    inner, model_display = _build_chat_session(provider, prompts[condition])
    session = CountingSession(inner)
    use_kb = kb if condition == "grounded" else None
    use_design = design_kb if condition == "grounded" else None

    original = agent_mod.get_error_kb
    if condition != "grounded":
        agent_mod.get_error_kb = lambda: None

    buf, t0, failed = io.StringIO(), time.time(), None
    try:
        with contextlib.redirect_stdout(buf):
            run_agent_cycle(session, v, client, prompt, model_display,
                            kb=use_kb, design_kb=use_design)
    except Exception as exc:
        failed = f"{type(exc).__name__}: {exc}"
    finally:
        agent_mod.get_error_kb = original
    elapsed = time.time() - t0

    measured = {}
    if kind == "simple":
        ok, detail = verify_simple(client, cell, needs_symbol)
    else:
        ok, detail, measured = verify_sim(client, cell, tb, stop, probes)

    transcript = buf.getvalue()
    infra = is_infra(failed or detail)
    return {
        "cell": cell, "kind": kind, "condition": condition,
        "infra_error": infra,
        "round_trips": session.round_trips,
        "attempts": transcript.count("Executing (attempt"),
        "success": ok, "detail": failed or detail, "measured": measured,
        "input_tokens": session.input_tokens,
        "output_tokens": session.output_tokens,
        "seconds": round(elapsed, 1), "transcript": transcript,
    }


def write_results(results, path=None):
    path = path or (DATA_DIR / "benchmark_v2.json")
    path.write_text(json.dumps(results, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conditions", nargs="+", default=["cold", "grounded"],
                    choices=["cold", "grounded"])
    ap.add_argument("--only", choices=["simple", "sim"], default=None)
    ap.add_argument("--tasks", type=int, default=None,
                    help="limit each family to the first N tasks")
    ap.add_argument("--provider", default="anthropic")
    ap.add_argument("--resume", action="store_true",
                    help="keep good records from a previous run, retry the rest")
    args = ap.parse_args()

    simple = SIMPLE if args.only in (None, "simple") else []
    sim = SIM if args.only in (None, "sim") else []
    if args.tasks:
        simple, sim = simple[:args.tasks], sim[:args.tasks]

    raw = VirtuosoClient.from_env()
    v, client = VirtuosoAPI(raw), SafeClientProxy(raw)
    print(f"connected: {skill(client, 'getCurrentTime()')}")
    if blocking_form(client):
        raise SystemExit("A modal Virtuoso dialog is blocking SKILL — dismiss it first.")
    if not v.library_exists(LIB):
        v.create_library(LIB, ref_lib="tsmcN65")

    prompts = {"cold": build_system_prompt(""),
               "grounded": build_system_prompt(_load_design_context())}
    kb, design_kb = _get_kb()

    snapshot = DATA_DIR / "agent_errors.v2snapshot"
    if ERROR_LOG_PATH.exists():
        shutil.copy(ERROR_LOG_PATH, snapshot)

    results = []
    done = set()
    resume = DATA_DIR / "benchmark_v2.json"
    if args.resume and resume.exists():
        results = json.loads(resume.read_text())
        done = {(r["condition"], r["kind"], r["cell"]) for r in results
                if not r.get("infra_error")}
        print(f"resuming — {len(done)} good records kept, "
              f"{len(results) - len(done)} infra failures will be retried")
        results = [r for r in results if not r.get("infra_error")]

    for condition in args.conditions:
        if snapshot.exists():
            shutil.copy(snapshot, ERROR_LOG_PATH)
        agent_mod.get_error_kb.__globals__["_error_kb"] = None
        print(f"\n{'=' * 66}\nCONDITION: {condition}\n{'=' * 66}")
        for kind, tasks in (("simple", simple), ("sim", sim)):
            for i, task in enumerate(tasks, 1):
                if (condition, kind, task[0]) in done:
                    continue
                print(f"[{condition}/{kind} {i}/{len(tasks)}] {task[0]} ... ",
                      end="", flush=True)
                if not bridge_alive(client):
                    print("BRIDGE DOWN — stopping so the remaining tasks are not "
                          "scored as agent failures. Restore the tunnel and "
                          "re-run; completed records are already saved.")
                    write_results(results)
                    return
                try:
                    rec = run_one(task, kind, condition, v, client, kb, design_kb,
                                  prompts, args.provider)
                except Exception as exc:                 # harness-side blowup
                    rec = {"cell": task[0], "kind": kind, "condition": condition,
                           "infra_error": True, "round_trips": 0, "attempts": 0,
                           "success": False, "measured": {},
                           "detail": f"harness error: {type(exc).__name__}: {exc}",
                           "input_tokens": 0, "output_tokens": 0,
                           "seconds": 0.0, "transcript": ""}
                results.append(rec)
                tag = "INFRA" if rec.get("infra_error") else (
                    "ok " if rec["success"] else "FAIL")
                print(f"{tag} rt={rec['round_trips']} att={rec['attempts']} "
                      f"{rec['seconds']}s | {rec['detail'][:70]}")
                write_results(results)

    if snapshot.exists():
        shutil.copy(snapshot, ERROR_LOG_PATH)
        snapshot.unlink()
    write_results(results)
    print(f"\nwrote {DATA_DIR / 'benchmark_v2.json'}")


if __name__ == "__main__":
    main()
