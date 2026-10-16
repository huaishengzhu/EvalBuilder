# data_train/ (you provide this — empty in the repo)

A copy of `data/results_agent/` (see its README for the file format) with
every **held-out** model's directory removed. Symlinked read-only into
each run's workspace as `reference_trajectories/`, so the agent can study
real task difficulty/style while designing its own brand-new tasks —
without ever being able to see a held-out model's data.

Only needed if you're using held-out models (`--held-out-models`, default
on). Skip this entirely if you pass `--held-out-models ""`.

## Build it

```bash
HELD_OUT_MODELS="claude-opus-4.7,gpt-5.5,kimi-k2.6"   # match --held-out-models
mkdir -p data_train
for d in data/results_agent/*/; do
  model=$(basename "$d")
  case ",$HELD_OUT_MODELS," in *",$model,"*) continue ;; esac
  cp -r "$d" data_train/
done
```

Rebuild this by hand any time you change `--held-out-models` — it is not
derived automatically at run time.

## Note for the agent

Real prompts/trajectories here are for understanding what "real task
difficulty" looks like — not for copying. The agent's own tasks must be
brand-new, not these tasks reused or lightly reworded.
