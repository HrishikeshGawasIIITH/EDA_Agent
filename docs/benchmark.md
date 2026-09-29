# Grounding benchmark — round-trips per task, verified by simulation

Run: 2026-09-29 · `deepseek-v4-pro` (temperature 0.1) · live Cadence Virtuoso IC23.1 + Spectre 23.1, TSMC 65nm · `MAX_RETRIES=3`

## What is measured

A **round-trip** is one `chat_session.send_message()` call — the unit that costs latency and tokens. A task that lands first try costs 2 (generate, then confirm).

**Success is proven by simulation, not by existence.** An earlier version of this benchmark counted a task successful when its cellviews existed. That was too weak: it passed an inverter testbench grounded with plain `VSS` net labels, which has no global ground, no SPICE node 0, and could never have simulated — yet it passes `schCheck` cleanly. The harness now builds the Maestro setup itself, runs a transient, and requires the measured node voltages to land within 5% of a rail.

The harness's own verification simulation is **not** counted in round-trips — it is scoring, not solving. Runs lost to a dead SSH tunnel or a busy Virtuoso are tagged `infra_error`, retried, and excluded rather than charged to the condition.

## Conditions

| | static API reference | RAG | design KB | error memory |
|---|:---:|:---:|:---:|:---:|
| `cold` | ✓ | ✗ | ✗ | ✗ |
| `grounded` | ✓ | ✓ | ✓ | ✓ |

Both conditions receive the shipped system prompt, which embeds a ~14 KB hand-written EDA reference (API surface, the analogLib parameter table, the `gnd!` rule, and the verified Maestro flow). `cold` is therefore **not** an ungrounded agent — it is an agent without *retrieval*. This distinction dominates the result.

## Results

| task | kind | cold | grounded |
|------|------|-----:|---------:|
| `inv` | simple | 2 | 2 |
| `nand2` | simple | 2 | 2 |
| `nor2` | simple | 2 | 2 |
| `tgate` | simple | 2 | 2 |
| `cs_amp` | simple | 2 | 2 |
| `cmirror` | simple | 2 | 2 |
| `cascode_mirror` | simple | 2 | 2 |
| `diffpair` | simple | 2 | 2 |
| `ota_5t` | simple | 2 | 3 |
| `ring_osc_3` | simple | 5 ✗ | 2 |
| `ring_osc_5` | simple | 4 | 3 ✗ |
| `inv_tb` | simple | 2 | 2 |
| `sinv` | sim | 4 | 1 ✗ |
| `snand` | sim | 4 | 4 |
| `snor` | sim | 5 | 4 |
| `sbuf` | sim | 4 | 4 |
| `stgate` | sim | 4 | 5 |
| `sinv_w` | sim | 4 | 4 |
| **mean — all 18** | | **3.00** (17/18 ok) | **2.67** (16/18 ok) |
| **mean — simple 12** | | **2.42** (11/12 ok) | **2.17** (11/12 ok) |
| **mean — simulation 6** | | **4.17** (6/6 ok) | **3.67** (5/6 ok) |

`✗` marks a task whose circuit did not verify.

## Headline — and why it is small

- Across all 18 tasks: **3.00 → 2.67** round-trips (**11% fewer**), completion 17/18 → 16/18.
- On the 15 tasks **both** conditions completed: **2.73 → 2.80** — a **2% increase**, i.e. no measurable benefit.

The paired figure is the honest one. The headline reduction is inflated by a `grounded` failure (`sinv`) that cost only 1 round-trip because the model returned an unparseable response and the loop bailed early — a failure that looks cheap.

**Why retrieval adds little here:** the decisive knowledge already sits in the static prompt that both conditions share. The nine simplest cells are at the 2-round-trip floor for both. Retrieval only helps where a task exceeds what the static prompt covers — `ring_osc_3` (cold: 5 round-trips and a failed check; grounded: 2) is the clearest case.

## The comparison that does show a large effect

An earlier run ablated grounding **as a whole** — a `bare` condition with the API reference, rules and retrieval all stripped, leaving only the JSON output contract:

| | bare | grounded |
|---|---:|---:|
| mean round-trips | 4.75 | 2.00 |
| tasks completed | 0/12 | 12/12 |

**58% fewer round-trips, and 0/12 → 12/12 completion.** `bare` built correct schematics but never discovered `v.create_symbol()`, burning all four attempts in 9 of 12 tasks. Caveats: that run used the earlier 12-task set and the earlier prompt, and `bare`'s mean is a ceiling imposed by `MAX_RETRIES` — read it next to the 0/12, not instead of it. See `benchmark_results.json`.

So the two numbers answer different questions: **grounding vs none ≈ 58%**; **retrieval on top of an already-grounded prompt ≈ 0–11%**.

## Independent verification

Three agent-built modules were re-simulated by hand at probe points the benchmark did not score on:

| module | check | measured |
|---|---|---|
| `sbuf` | output must FOLLOW input | IN 1.2 V → OUT 1.199986 V; IN 0 V → OUT 6.2 µV |
| `snand` | A=B=high → output low | 30.4 µV at 5 ns and 15 ns |
| `snor` | A=B=low → output high | 1.199946 V at 5 ns and 15 ns |

## Reproduce

```bash
python tools/benchmark_v2.py                      # cold + grounded, 18 tasks
python tools/benchmark_v2.py --only sim --tasks 3
python tools/benchmark_v2.py --resume             # retry infra failures only
```
