"""
benchmark_roundtrips.py — Measure how much grounding reduces LLM round-trips.

Runs the same schematic task set under three levels of grounding against a live
Virtuoso session:

  bare      Output contract only. No API reference, no rules, no retrieval —
            the model gets the task and the JSON protocol, nothing else.
  cold      The shipped static system prompt (full API reference + hand-written
            rules), but no retrieval: no RAG, no design KB, no error recall.
  grounded  Full pipeline: static prompt + scanned design context, plus RAG over
            the bridge API docs (FAISS + sentence-transformers) and semantic
            recall of previously resolved errors injected into each retry.

bare→grounded isolates the value of grounding as a whole; cold→grounded isolates
the marginal value of retrieval on top of an already-detailed prompt.

A "round-trip" is one `chat_session.send_message()` call — the unit that costs
latency and tokens. A task that succeeds first try costs 2 (generate + confirm);
every failed attempt adds one more.

All conditions start from an identical error-memory snapshot, and every run is
verified against Virtuoso (the cell and its views must actually exist) rather
than trusting the absence of an exception.

Usage:
    python tools/benchmark_roundtrips.py                 # full run
    python tools/benchmark_roundtrips.py --tasks 4       # first 4 tasks only
    python tools/benchmark_roundtrips.py --conditions grounded

Results are written to data/benchmark_roundtrips.json and .md.
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

BENCH_LIB = "EDA_Agent_Bench"

# The shipped system prompt always embeds the full API_REFERENCE plus the
# hand-written gotchas (vpulse `per` not `period`, `fingers` not `nf`,
# Wp/Wn ≈ 2.27, create_symbol...). That is itself grounding, so "no RAG" is not
# the same as "ungrounded". This baseline keeps only the output contract — the
# JSON protocol the loop needs to parse anything at all — and strips every piece
# of injected EDA domain knowledge.
MINIMAL_SYSTEM_PROMPT = """You are an assistant in a live Cadence Virtuoso IC \
design session (TSMC 65nm, reference technology library 'tsmcN65').

You control the session by writing Python that uses two objects:
  1. `v` — VirtuosoAPI
  2. `client` — VirtuosoClient

STEP 1 — Return ONLY a raw JSON object:
- "plan": brief explanation of what you will do.
- "code": valid Python using `v` and/or `client`. Store the outcome in `result`.
No markdown fences around the JSON. Do NOT simulate execution.

STEP 2 — After [SYSTEM: Execution Result]:
- SUCCESS: explain in plain text what happened.
- ERROR: retry with a new JSON object (include "retry": true). Max 3 retries.
"""

# (cell, needs_symbol, prompt)
TASKS = [
    ("inv", True,
     "Create a CMOS inverter cell named 'inv' in library EDA_Agent_Bench using "
     "tsmcN65 nch and pch. Wp=480n, Wn=210n, L=60n. Add IN, OUT, VDD, VSS pins "
     "and generate the symbol view."),
    ("nand2", True,
     "Create a 2-input CMOS NAND gate cell named 'nand2' in library "
     "EDA_Agent_Bench: two series NMOS, two parallel PMOS, all L=60n, "
     "Wn=420n, Wp=480n. Pins A, B, OUT, VDD, VSS. Generate the symbol."),
    ("nor2", True,
     "Create a 2-input CMOS NOR gate cell named 'nor2' in library "
     "EDA_Agent_Bench: two parallel NMOS, two series PMOS, L=60n, Wn=210n, "
     "Wp=960n. Pins A, B, OUT, VDD, VSS. Generate the symbol."),
    ("tgate", True,
     "Create a CMOS transmission gate cell named 'tgate' in library "
     "EDA_Agent_Bench with an nch and pch in parallel, L=60n, W=240n each. "
     "Pins IN, OUT, EN, ENB. Generate the symbol."),
    ("cs_amp", True,
     "Create a common-source amplifier cell named 'cs_amp' in library "
     "EDA_Agent_Bench: one nch (W=2u, L=200n) with a 10k analogLib resistor as "
     "drain load. Pins IN, OUT, VDD, VSS. Generate the symbol."),
    ("cmirror", True,
     "Create an NMOS current mirror cell named 'cmirror' in library "
     "EDA_Agent_Bench with two matched nch devices (W=1u, L=500n) sharing a "
     "gate, diode-connected input branch. Pins IREF, IOUT, VSS. "
     "Generate the symbol."),
    ("cascode_mirror", True,
     "Create a cascode NMOS current mirror cell named 'cascode_mirror' in "
     "library EDA_Agent_Bench using four nch devices (W=1u, L=500n). "
     "Pins IREF, IOUT, VBIAS, VSS. Generate the symbol."),
    ("diffpair", True,
     "Create a differential pair cell named 'diffpair' in library "
     "EDA_Agent_Bench: two matched nch input devices (W=4u, L=180n) with a "
     "shared tail nch current source (W=8u, L=500n). Pins INP, INN, OUTP, "
     "OUTN, VBIAS, VDD, VSS. Generate the symbol."),
    ("ota_5t", True,
     "Create a 5-transistor OTA cell named 'ota_5t' in library "
     "EDA_Agent_Bench: nch differential pair (W=4u, L=180n), pch current "
     "mirror load (W=8u, L=180n), nch tail source (W=8u, L=500n). "
     "Pins INP, INN, OUT, VBIAS, VDD, VSS. Generate the symbol."),
    ("ring_osc_3", False,
     "Create a 3-stage ring oscillator cell named 'ring_osc_3' in library "
     "EDA_Agent_Bench by instantiating the existing EDA_Agent_Bench/inv symbol "
     "three times in a loop. Pins OUT, VDD, VSS."),
    ("ring_osc_5", False,
     "Create a 5-stage ring oscillator cell named 'ring_osc_5' in library "
     "EDA_Agent_Bench by instantiating the EDA_Agent_Bench/inv symbol five "
     "times in a ring. Pins OUT, VDD, VSS."),
    ("inv_tb", False,
     "Create a testbench schematic named 'inv_tb' in library EDA_Agent_Bench "
     "for the EDA_Agent_Bench/inv cell: a vpulse source driving IN (v1=0, "
     "v2=1.2, td=0, tr=10p, tf=10p, pw=5n, per=10n), a 1.2V vdc supply on VDD, "
     "a 10f cap on OUT, and proper ground. The schematic must pass schCheck "
     "with connectivityLastUpdated set to an integer."),
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


def cell_exists(client, lib: str, cell: str) -> bool:
    r = client.execute_skill(f'ddGetObj("{lib}" "{cell}") && t', timeout=20)
    return "t" in (getattr(r, "output", "") or "").strip()


def views_of(client, lib: str, cell: str) -> list:
    r = client.execute_skill(
        f'mapcar(lambda((x) x~>name) ddGetObj("{lib}" "{cell}")~>views)', timeout=20)
    out = (getattr(r, "output", "") or "").strip()
    return [w.strip('"') for w in out.strip("()").split() if w.strip('"')]


def delete_cell(client, lib: str, cell: str) -> None:
    """Remove a cell so each run starts from the same state."""
    client.execute_skill(
        f'when(ddGetObj("{lib}" "{cell}") ddDeleteObj(ddGetObj("{lib}" "{cell}")))',
        timeout=30)


def verify(client, lib, cell, needs_symbol) -> tuple[bool, str]:
    """Confirm in Virtuoso that the task actually produced what was asked."""
    if not cell_exists(client, lib, cell):
        return False, "cell not created"
    views = views_of(client, lib, cell)
    if "schematic" not in views:
        return False, f"no schematic view (views={views})"
    if needs_symbol and "symbol" not in views:
        return False, f"no symbol view (views={views})"
    return True, f"views={views}"


def run_one(task, condition, v, client, kb, design_kb, prompts, provider):
    """Run a single task under one condition; return a result record."""
    cell, needs_symbol, prompt = task

    delete_cell(client, BENCH_LIB, cell)

    inner, model_display = _build_chat_session(provider, prompts[condition])
    session = CountingSession(inner)

    use_kb = kb if condition == "grounded" else None
    use_design = design_kb if condition == "grounded" else None

    # Only the grounded condition may recall previously resolved errors;
    # patch the lookup the retry path uses for the others.
    original_get_error_kb = agent_mod.get_error_kb
    if condition != "grounded":
        agent_mod.get_error_kb = lambda: None

    buf = io.StringIO()
    t0 = time.time()
    failed = None
    try:
        with contextlib.redirect_stdout(buf):
            run_agent_cycle(session, v, client, prompt, model_display,
                            kb=use_kb, design_kb=use_design)
    except Exception as exc:  # a crash is a failed task, not a crashed benchmark
        failed = f"{type(exc).__name__}: {exc}"
    finally:
        agent_mod.get_error_kb = original_get_error_kb
    elapsed = time.time() - t0

    ok, detail = verify(client, BENCH_LIB, cell, needs_symbol)
    transcript = buf.getvalue()
    attempts = transcript.count("Executing (attempt")

    return {
        "cell": cell,
        "condition": condition,
        "round_trips": session.round_trips,
        "attempts": attempts,
        "success": ok,
        "detail": failed or detail,
        "input_tokens": session.input_tokens,
        "output_tokens": session.output_tokens,
        "seconds": round(elapsed, 1),
        "transcript": transcript,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=len(TASKS),
                    help="run only the first N tasks")
    ap.add_argument("--conditions", nargs="+",
                    default=["bare", "cold", "grounded"],
                    choices=["bare", "cold", "grounded"])
    ap.add_argument("--provider", default="anthropic")
    args = ap.parse_args()

    tasks = TASKS[:args.tasks]

    raw_client = VirtuosoClient.from_env()
    v = VirtuosoAPI(raw_client)
    client = SafeClientProxy(raw_client)
    print(f"Connected: {raw_client.execute_skill('getCurrentTime()').output.strip()}")

    if not v.library_exists(BENCH_LIB):
        v.create_library(BENCH_LIB, ref_lib="tsmcN65")
        print(f"Created library {BENCH_LIB}")

    design_context = _load_design_context()
    prompts = {
        # no injected EDA knowledge at all — output contract only
        "bare": MINIMAL_SYSTEM_PROMPT,
        # shipped static prompt (API reference + rules), but no retrieval
        "cold": build_system_prompt(""),
        # static prompt + scanned design context, plus RAG and error recall
        "grounded": build_system_prompt(design_context),
    }
    kb, design_kb = _get_kb()

    # Snapshot error memory so both conditions start from the same state.
    snapshot = DATA_DIR / "agent_errors.snapshot"
    if ERROR_LOG_PATH.exists():
        shutil.copy(ERROR_LOG_PATH, snapshot)

    results = []
    for condition in args.conditions:
        if snapshot.exists():
            shutil.copy(snapshot, ERROR_LOG_PATH)
        agent_mod.get_error_kb.__globals__["_error_kb"] = None

        print(f"\n{'=' * 64}\nCONDITION: {condition}\n{'=' * 64}")
        for i, task in enumerate(tasks, 1):
            print(f"[{condition} {i}/{len(tasks)}] {task[0]} ... ", end="", flush=True)
            rec = run_one(task, condition, v, client, kb, design_kb,
                          prompts, args.provider)
            results.append(rec)
            status = "ok" if rec["success"] else "FAIL"
            print(f"{status}  round_trips={rec['round_trips']} "
                  f"attempts={rec['attempts']} {rec['seconds']}s")
            write_results(results)

    # Restore the user's original error log.
    if snapshot.exists():
        shutil.copy(snapshot, ERROR_LOG_PATH)
        snapshot.unlink()

    write_results(results)
    print(f"\nWrote {DATA_DIR / 'benchmark_roundtrips.json'}")


def write_results(results):
    out_json = DATA_DIR / "benchmark_roundtrips.json"
    out_json.write_text(json.dumps(results, indent=2))

    by_cell = {}
    for r in results:
        by_cell.setdefault(r["cell"], {})[r["condition"]] = r

    lines = ["| task | cold | grounded | reduction |",
             "|------|------|----------|-----------|"]

    # A task that bailed out early costs fewer round-trips while producing
    # nothing, so the headline number is computed only over tasks that BOTH
    # conditions actually completed. Failures are reported separately.
    cold_tot = grounded_tot = 0
    paired = 0
    cold_fail = grounded_fail = 0

    for cell, conds in by_cell.items():
        c = conds.get("cold")
        g = conds.get("grounded")
        if not (c and g):
            continue
        cs = "" if c["success"] else " ✗"
        gs = "" if g["success"] else " ✗"
        cold_fail += 0 if c["success"] else 1
        grounded_fail += 0 if g["success"] else 1

        if c["success"] and g["success"]:
            paired += 1
            cold_tot += c["round_trips"]
            grounded_tot += g["round_trips"]
            delta = ((1 - g["round_trips"] / c["round_trips"]) * 100
                     if c["round_trips"] else 0)
            delta_s = f"{delta:.0f}%"
        else:
            delta_s = "—"
        lines.append(f"| {cell} | {c['round_trips']}{cs} | {g['round_trips']}{gs} "
                     f"| {delta_s} |")

    if cold_tot:
        total_delta = (1 - grounded_tot / cold_tot) * 100
        lines.append(f"| **mean (both succeeded, n={paired})** "
                     f"| **{cold_tot / paired:.2f}** | **{grounded_tot / paired:.2f}** "
                     f"| **{total_delta:.0f}%** |")

    total_tasks = len(by_cell)
    lines += [
        "",
        f"Tasks: {total_tasks}. Completed — cold "
        f"{total_tasks - cold_fail}/{total_tasks}, grounded "
        f"{total_tasks - grounded_fail}/{total_tasks}.",
        f"Round-trip means are over the {paired} tasks both conditions completed; "
        "✗ marks a task that did not produce the requested cellviews.",
    ]
    (DATA_DIR / "benchmark_roundtrips.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
