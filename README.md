# auto-eval

An LLM agent designs its own **lightweight** substitute for a heavy,
sandboxed benchmark (e.g. SWE-Bench Verified): it writes brand-new tasks,
collects real responses to them from a set of **candidate models**, writes
a `verify.py` to score those responses, and iterates until its cheap
benchmark's model ranking matches the real, expensive benchmark's ranking
(`evaluate.py` checks this against `truth.json`, a model-level pass-rate
aggregate with no per-task answer key). A subset of candidate models can be
**held out** — completely invisible to the agent during the whole session
— and scored only afterwards, as a generalization check.

This repo is just the harness (`run.py`, `evaluate.py`, the initial
`verify.py` stub). It ships with **no API keys, no endpoints, and no
evaluation data** — you provide all three, locally, as described below.

## What you need to provide

| # | What | Where |
|---|------|-------|
| 1 | A model to **drive the research agent** (designs tasks + verify.py) | `~/.claude/settings.json` |
| 2 | One endpoint/key per **candidate model** being evaluated (≥ 2 required; any held-out models count towards this) | `~/.autoeval/candidate_models.json` |
| 3 | Real trajectories for every candidate model, to compute ground-truth pass rates | `data/results_agent/<model_name>/*.jsonl` |

Models are identified purely by the name you choose — there's no fixed
model list. Whatever directory names you use under `data/results_agent/`
become the model names used everywhere else (`truth.json`,
`candidate_models.json` keys, `--held-out-models`).

## Requirements

- Python 3.10+ (standard library only, no pip installs needed)
- [OpenCode](https://opencode.ai) CLI on `PATH` (or set `OPENCODE_BIN`)
- Every model in steps 1–2 above must be reachable through an
  Anthropic-Messages-API-compatible endpoint (`POST /v1/messages`)

## 1. Configure the research agent's model

Copy `settings.json.example` → `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://your-llm-gateway.example.com/",
    "ANTHROPIC_AUTH_TOKEN": "YOUR_AUTH_TOKEN",
    "ANTHROPIC_MODEL": "your-agent-model-id"
  }
}
```

- `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` — your gateway and its token.
- `ANTHROPIC_MODEL` — the model id that will design the benchmark. Can be
  overridden per-run with `--model <id>` without editing this file.

Or point `--settings` at a file anywhere else.

## 2. Configure candidate models

Copy `candidate_models.json.example` → `~/.autoeval/candidate_models.json`:

```json
{
  "default": {
    "endpoint": "https://your-llm-gateway.example.com/v1/messages",
    "api_key": "YOUR_DEFAULT_API_KEY"
  },
  "overrides": {
    "model-name-used-in-data-results-agent": {
      "endpoint": "https://a-different-gateway.example.com/v1/messages",
      "api_key": "A_DIFFERENT_API_KEY",
      "model_name": "actual-id-this-gateway-expects"
    }
  }
}
```

- `default` — endpoint + key used for any candidate model with no entry
  under `overrides`.
- `overrides.<name>` — `<name>` must match a directory name under
  `data/results_agent/` exactly. All three fields are optional and each
  falls back to `default` if omitted:
  - `endpoint` — use a different gateway for just this model.
  - `api_key` — use a different key for just this model.
  - `model_name` — the id actually sent as `"model"` in the API request,
    if it differs from `<name>` (e.g. `<name>` is your own short alias but
    the upstream gateway expects a longer/versioned id).
- If this file doesn't exist at all, every candidate model is called
  directly by its own name with the request unmodified — fine if your
  gateway needs no per-model routing.

The agent itself never sees this file or any credential in it — during a
run it only talks to a local HTTP proxy (`CANDIDATE_MODEL_ENDPOINT`) that
`run.py` starts, which does this routing/auth on its behalf and hard-blocks
(403) any request naming a held-out model.

Or point `--candidate-models-config` at a file anywhere else.

## 3. Provide ground-truth evaluation data

See `data/results_agent/README.md` for the exact file format. Then, if
you're using held-out models, build `data_train/` (see
`data_train/README.md`) — a copy of `data/results_agent/` with the
held-out models' directories removed, since it's exposed read-only to the
agent as reference material.

## Running

```bash
python run.py --run-name my_experiment --time-budget-minutes 480 --num-tasks 50
```

Useful flags (`python run.py --help` for the full list):

| Flag | Meaning |
|---|---|
| `--time-budget-minutes` | total wall-clock budget for the whole session (default 360) |
| `--num-tasks` | how many new tasks the agent should design (default 50) |
| `--model` | override the research-agent model id for this run |
| `--held-out-models` | comma-separated model names to hide from the agent entirely; `""` disables held-out evaluation (default: `claude-opus-4.7,gpt-5.5,kimi-k2.6` — change this to match your own model names, or pass your own list) |
| `--stagnation-patience` | auto-stop iterating after N non-improving rounds in a row; `0` disables |
| `--prepare-only` | just set up the workspace, don't launch the agent |

## Output

Each run writes to `runs/<run-name>/`:

```
runs/<run-name>/
  report.json                  final metrics, VISIBLE models only
  report_full.json             final metrics: overall / visible-only / held-out-only
  truth_full.json              ground truth for every model (visible + held-out)
  workspace/
    verify.py, responses.json  the agent's final submission
    ITERATIONS_LOG.txt         one line per iteration (timing, validity, metrics)
    iterations/iter_N/         full snapshot + summary.json per iteration
```

## Metrics (`evaluate.py`)

- **pairwise_accuracy** — fraction of model pairs ranked in the correct order.
- **spearman** / **kendall_tau_b** — rank correlation, predicted vs. real.
- **pearson** — linear correlation on raw scores.
- **mae / bias / mae_centered / mae_zscore** — raw error, systematic offset,
  and shape-only error (see comments in `evaluate.py`).
- **spread_ratio** — `std(predicted) / std(actual)`. Catches "score
  collapse": if every model gets a similar score, pairwise_accuracy/spearman
  can still look fine by luck, but `spread_ratio << 1` reveals the verifier
  isn't actually discriminating between models.
