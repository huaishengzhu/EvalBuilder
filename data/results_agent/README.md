# data/results_agent/ (you provide this — empty in the repo)

Real evaluation trajectories, used once at startup to compute each
candidate model's ground-truth pass rate (`truth.json`). Not committed —
see the repo root `.gitignore`.

## Layout

```
data/results_agent/
  <model_name_1>/*.jsonl
  <model_name_2>/*.jsonl
  ...
```

One subdirectory per model. The directory name IS the model name used
everywhere else in this project (`truth.json` keys, `candidate_models.json`
entries, `--held-out-models`). Every `*.jsonl` inside a model's directory
is read; each line is one JSON record for one task:

```json
{"task_id": "some-unique-id", "correct": true}
```

Only `correct` is required (boolean). If you don't have a flat `correct`
field, a nested `code_test.is_resolved` is also accepted — see
`build_truth_json()` in `run.py` for the exact fallback logic. Any other
fields (prompt, trajectory, model name, etc.) are ignored by
`build_truth_json`, but kept as-is if you build `data_train/` from these
same files (see `data_train/README.md`) — extra fields there are useful
context for the agent, not just noise.

## What happens to it

`run.py` averages `correct` (1/0) per model directory into
`truth.json` = `{model_name: pass_rate}`. That's the only ground truth the
agent ever gets — an aggregate, with no per-task key, so it can't be
reverse-engineered for the agent's own new tasks.

Every model you want scored — including held-out ones — needs a directory
here. `--held-out-models` only controls what the agent gets to *see*
(`data_train/`, `truth.json`); the harness still needs every held-out
model's real pass rate from here to compute the final held-out report.
