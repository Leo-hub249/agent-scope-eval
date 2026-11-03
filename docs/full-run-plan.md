# Full run: plan and cost estimate (budget approved 2026-11-03; calibration first)

**Design:** 15 tasks × 3 models (claude-haiku-5-5, claude-sonnet-5-5, claude-opus-5-5) × 5 repeats = **225 runs**.
- **Namespace and order:** all runs use namespace `experiment`. The (task, model, sample) triples run in one order shuffled with `task_order_seed` 1234, so time effects do not line up with models.
- **Settings:** every model runs at effort `high`, with the same prompt and the same limits (30 turns, 60 s per command, 400K tokens per run).
- **Provenance:** one locked image (D3). The run refuses a dirty tree.

**Prices** (USD per MTok, `config/pricing.yaml`, checked 2026-11-03, D46):

| model | input | cache write (5 min) | cache read | output |
|---|---|---|---|---|
| claude-haiku-5-5 | 0.10 | 0.125 | 0.01 | 0.50 |
| claude-sonnet-5-5 | 2.00 | 2.50 | 0.10 | 10.00 |
| claude-opus-5-5 | 4.00 | 5.00 | 0.20 | 20.00 |

**Basis:** the five pilot runs (Sonnet only) average 6.6 turns per run. Mean tokens per run: 15 input, 5,241 cache writes, 15,878 cache reads and 1,439 output.
- **Other models:** the same token profile, priced at each model's rates. Haiku is scaled by +30% for its tokenizer.
- **Longer runs:** scenario B doubles the turns. Cache reads then grow about 4x (each turn rereads the whole context); everything else grows about 2x.

| scenario | Haiku (75 runs) | Sonnet (75) | Opus (75) | total |
|---|---|---|---|---|
| A: pilot-like (6.6 turns) | $0.15 | $2.18 | $4.37 | **$6.70** |
| B: harder tasks (about 13 turns) | $0.33 | $4.60 | $9.21 | **$14.14** |
| C: every run near the 400K-token cap | $1.11 | $19.58 | $39.16 | **$59.86** (ceiling) |

**Other costs:**
- **Monitor** (Phase 7, Haiku, two conditions × 225 runs): about $0.81.
- **Calibration** before the full run (one Sonnet run of each new task, plus Opus and Haiku on two tasks to measure their token profiles): about $1.

**Uncertainty:** Opus and Haiku have not been measured yet, and the new tasks are designed to take longer. B is the planning figure; C is the ceiling.

**Approved budget (D49):** namespace `experiment`: 225 runs, hard cap $60. The runner checks the cap before every run, and the agent loop stops a run before its next request if the cap would be reached. Calibration runs first: namespace `calibration`, 14 runs, cap $1.50. Global cap: $62.

**Wall-clock:** pilot runs took about 1–1.5 minutes each. At 1.5 minutes, 225 sequential runs take about 6 hours, or about 2 hours with 3 sandboxes in parallel.
