# Grounding benchmark — LLM round-trips per schematic task

Run: 2026-09-25 · model `deepseek-v4-pro` (Anthropic-compatible endpoint, temperature 0.1) · live Cadence Virtuoso IC23.1, TSMC 65nm · `MAX_RETRIES=3`

## What is being measured

A **round-trip** is one `chat_session.send_message()` call — the unit that costs latency and tokens. A task that succeeds on the first attempt costs 2 (generate, then confirm the execution result); every failed attempt adds one more, capped by `MAX_RETRIES`.

Each task is one natural-language prompt asking for a specific cell. **Success is verified against Virtuoso** — the cell and its required views must actually exist afterwards — not inferred from the absence of an exception. Cells are deleted before each run so every condition starts from the same state, and all three conditions start from an identical error-memory snapshot.

## Conditions

| Condition | Static API reference | RAG retrieval | Design KB | Error memory |
|-----------|:---:|:---:|:---:|:---:|
| `bare`     | ✗ | ✗ | ✗ | ✗ |
| `cold`     | ✓ | ✗ | ✗ | ✗ |
| `grounded` | ✓ | ✓ | ✓ | ✓ |

`bare` keeps only the JSON output contract the agent loop needs in order to parse anything at all; every piece of injected EDA domain knowledge is stripped. This distinction matters: the shipped system prompt already embeds a full API reference, so *"RAG off"* is not the same as *"ungrounded"*.

## Results

| task | bare | cold | grounded |
|------|-----:|-----:|---------:|
| `inv` | 4 ✗ | 2 | 2 |
| `nand2` | 5 ✗ | 2 | 2 |
| `nor2` | 4 ✗ | 2 | 2 |
| `tgate` | 5 ✗ | 2 | 2 |
| `cs_amp` | 5 ✗ | 2 | 2 |
| `cmirror` | 5 ✗ | 2 | 2 |
| `cascode_mirror` | 5 ✗ | 2 | 2 |
| `diffpair` | 5 ✗ | 2 | 2 |
| `ota_5t` | 5 ✗ | 2 | 2 |
| `ring_osc_3` | 5 ✗ | 5 | 2 |
| `ring_osc_5` | 5 ✗ | 1 ✗ | 2 |
| `inv_tb` | 4 ✗ | 4 | 2 |
| **completed** | **0/12** | **11/12** | **12/12** |
| **mean round-trips** | **4.75** | **2.33** | **2.00** |

`✗` marks a task that did not produce the requested cellviews.

## Headline

- **Grounding vs none — 58% fewer round-trips** (4.75 → 2.00 per task), and completion goes from **0/12** to **12/12**.
- **Retrieval on top of a detailed prompt — 14% fewer** (2.33 → 2.00), completion **11/12** → **12/12**.

The second number is small because `cold` already sits at the 2-round-trip floor on the nine simpler cells. The gap concentrates in the tasks that are actually hard:

| task | cold | grounded |
|------|-----:|---------:|
| `ring_osc_3` (hierarchical reuse) | 5 | 2 |
| `ring_osc_5` (hierarchical reuse) | failed | 2 |
| `inv_tb` (testbench + `schCheck`)  | 4 | 2 |

Across the two of those that both conditions completed, retrieval cuts round-trips by 56% (9 → 4).

## How `bare` fails

The failure is strikingly uniform. In 9 of 12 tasks `bare` built a correct schematic and then failed only on the symbol view — it never discovers `v.create_symbol()`, which lives in the static API reference. It burns all four attempts re-trying variations and still ends with `views=['schematic']`.

## Caveats

- `bare` round-trips are **capped** by `MAX_RETRIES=3` (≤5 round-trips). Its 4.75 mean is a ceiling imposed by the retry limit, not the cost of eventually succeeding — it never succeeded. Read the number alongside the 0/12 completion rate, not instead of it.
- `ring_osc_3`, `ring_osc_5` and `inv_tb` instantiate the `inv` cell built by an earlier task. Under `bare` that cell had no symbol view, so those three failures are partly **cascading** rather than independent.
- One run per cell per condition, a single model at temperature 0.1. These are not averaged over repeated trials, so treat per-task numbers as indicative.

## Reproduce

```bash
python tools/benchmark_roundtrips.py                       # all three conditions
python tools/benchmark_roundtrips.py --conditions bare grounded --tasks 4
```

Raw per-run metrics (all 36 runs): [`benchmark_results.json`](benchmark_results.json).
Full agent transcripts are kept locally in `data/benchmark_all.json` (gitignored — they
embed a listing of the host's private library names).
