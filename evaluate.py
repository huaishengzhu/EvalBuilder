#!/usr/bin/env python3
"""Fixed evaluation script — the agent may NOT edit this file.

TASK: the agent designs its OWN 100 new evaluation tasks (not reused from
SWE-Bench), collects each candidate model's REAL response to every task
(by calling the model APIs), writes a scoring function in verify.py, and
this script checks whether that scoring pipeline reproduces the same model
ranking as the models' real SWE-Bench Verified pass rates.

INPUTS:
  --responses responses.json   Agent-authored file: a list of task records,
      each of the form:
        {
          "task_id": "...",              # unique id for the agent's own task
          "prompt": "...",                # the task the agent wrote
          "reference_answer": "...",      # optional, agent's own reference/rubric
          "model_responses": {
              "<model_name>": "<raw text response from that model>",
              ...                          # one entry per candidate model
          }
        }
      There must be at least 2 distinct tasks and every model in --truth
      should appear in model_responses for (ideally) every task.

  --verify verify.py           Agent-authored scoring function. Loaded and
      called once per (task, model) pair:
          verify(response: str, answer: str | None, task: dict) -> float
      task dict contains: task_id, prompt, reference_answer, model.

  --truth truth.json           Ground-truth ranking signal: a flat mapping
      of {model_name: real_swebench_verified_pass_rate}. This is a
      MODEL-LEVEL aggregate only (no per-task labels), so the agent cannot
      reverse-engineer answers to its own 100 tasks from it — it can only
      be used post-hoc to judge whether the agent's from-scratch benchmark
      produces the same model ordering as the real SWE-Bench numbers.

OUTPUT:
  --output report.json         Ranking-agreement report: for every model,
      the predicted average score (mean verify() output across the agent's
      100 tasks) vs. the real SWE-Bench Verified pass rate, plus ranking
      metrics: pairwise_accuracy, spearman, kendall_tau_b, pearson, mae,
      top1_match, plus MAE-derived diagnostics that expose score collapse
      (predicted scores clustering too tightly to discriminate models):
      bias, mae_centered, mae_zscore, pred_std, actual_std, spread_ratio.
      See compute_metrics() docstring/comments for how to read these.

Usage:
    python evaluate.py --responses responses.json --verify verify.py \\
        --truth truth.json --output report.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_verify(verify_path: Path):
    """Load verify function from verify.py."""
    spec = importlib.util.spec_from_file_location("autoeval_verifier", verify_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "verify"):
        raise AttributeError("verify.py must define verify(response, answer, task) -> float")
    return mod.verify


def compute_metrics(
    predicted: dict[str, float],
    actual: dict[str, float],
) -> dict[str, Any]:
    """Compute ranking metrics between predicted and actual model scores."""
    models = sorted(set(predicted) & set(actual))
    if len(models) < 2:
        return {
            "num_models": len(models),
            "pairwise_accuracy": None,
            "spearman": None,
            "kendall_tau_b": None,
            "pearson": None,
            "mae": None,
            "bias": None,
            "mae_centered": None,
            "mae_zscore": None,
            "pred_std": None,
            "actual_std": None,
            "spread_ratio": None,
            "top1_match": None,
        }

    pred_vals = [predicted[m] for m in models]
    actual_vals = [actual[m] for m in models]

    # Pairwise accuracy
    correct = total = 0
    for i in range(len(models)):
        for j in range(i + 1, len(models)):
            dp = pred_vals[i] - pred_vals[j]
            da = actual_vals[i] - actual_vals[j]
            if da == 0:
                continue
            total += 1
            if dp == 0:
                continue
            correct += int((da > 0) == (dp > 0))
    pairwise_accuracy = correct / total if total else None

    # Spearman (rank correlation)
    def rank(vals):
        indexed = sorted(enumerate(vals), key=lambda x: x[1], reverse=True)
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(indexed):
            j = i + 1
            while j < len(indexed) and indexed[j][1] == indexed[i][1]:
                j += 1
            avg_rank = (i + j) / 2
            for k in range(i, j):
                ranks[indexed[k][0]] = avg_rank
            i = j
        return ranks

    pr = rank(pred_vals)
    ar = rank(actual_vals)

    def pearson(xs, ys):
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        dx = sum((x - mx) ** 2 for x in xs) ** 0.5
        dy = sum((y - my) ** 2 for y in ys) ** 0.5
        if dx == 0 or dy == 0:
            return None
        return num / (dx * dy)

    spearman = pearson(pr, ar)

    # Kendall tau-b
    concordant = discordant = 0
    ties_pred = ties_actual = 0
    for i in range(len(models)):
        for j in range(i + 1, len(models)):
            dp = pred_vals[i] - pred_vals[j]
            da = actual_vals[i] - actual_vals[j]
            if dp == 0 and da == 0:
                ties_pred += 1
                ties_actual += 1
            elif dp == 0:
                ties_pred += 1
            elif da == 0:
                ties_actual += 1
            elif (dp > 0) == (da > 0):
                concordant += 1
            else:
                discordant += 1

    denom = ((concordant + discordant + ties_pred) * (concordant + discordant + ties_actual)) ** 0.5
    kendall_tau_b = (concordant - discordant) / denom if denom else None

    # MAE (raw). Note: this is an *absolute* error on the raw score scale.
    # When predicted_score barely varies across models (i.e. the agent's
    # verify() fails to discriminate between models), MAE is dominated by
    # the constant systematic offset between the predicted and actual score
    # scales and stays deceptively small/stable — it does NOT reveal that
    # the predictions collapsed onto a narrow band. The three metrics below
    # are derived from MAE but correct for this blind spot.
    diffs = [predicted[m] - actual[m] for m in models]
    mae = sum(abs(d) for d in diffs) / len(models)

    def _mean_std(vals):
        n = len(vals)
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / n
        return mean, var ** 0.5

    pred_mean, pred_std = _mean_std(pred_vals)
    actual_mean, actual_std = _mean_std(actual_vals)

    # 1) spread_ratio: ratio of how "spread out" the predicted scores are
    #    vs. the actual scores (std-dev based). This directly exposes score
    #    collapse: spread_ratio << 1 means the agent's verify() squashes
    #    all models toward a similar score even though the real models
    #    differ a lot, i.e. it "can't tell the models apart".
    spread_ratio = (pred_std / actual_std) if actual_std > 0 else None

    # 2) bias: the average signed offset (predicted - actual). A large
    #    |bias| with small mae_centered below means the error is almost
    #    entirely a constant shift (e.g. verify() is just globally too
    #    lenient/strict), not a discrimination problem.
    bias = pred_mean - actual_mean

    # 3) mae_centered ("bias-corrected MAE"): recompute MAE after removing
    #    each series' own mean, i.e. compare (predicted - pred_mean) vs.
    #    (actual - actual_mean). This cancels out any constant offset and
    #    isolates the *shape/spread* mismatch — it will stay large whenever
    #    predicted scores don't move together with actual scores across
    #    models, even if the raw MAE looks small.
    pred_centered = [p - pred_mean for p in pred_vals]
    actual_centered = [a - actual_mean for a in actual_vals]
    mae_centered = sum(abs(p - a) for p, a in zip(pred_centered, actual_centered)) / len(models)

    # 4) mae_zscore ("fully normalized MAE"): z-score both series (center
    #    AND rescale by their own std-dev) before taking MAE. This puts
    #    predicted/actual on a common, unit-variance scale, so the result
    #    is a direct, scale-free measure of ranking/shape disagreement.
    #    It stays informative (and tends to blow up) precisely in the
    #    "scores don't vary enough to distinguish models" scenario, since
    #    dividing by a tiny pred_std amplifies any relative misordering.
    if pred_std > 0 and actual_std > 0:
        pred_z = [(p - pred_mean) / pred_std for p in pred_vals]
        actual_z = [(a - actual_mean) / actual_std for a in actual_vals]
        mae_zscore = sum(abs(p - a) for p, a in zip(pred_z, actual_z)) / len(models)
    else:
        mae_zscore = None

    # Top-1 match
    pred_top = max(models, key=lambda m: predicted[m])
    actual_top = max(models, key=lambda m: actual[m])
    top1_match = pred_top == actual_top

    return {
        "num_models": len(models),
        "pairwise_accuracy": pairwise_accuracy,
        "spearman": spearman,
        "kendall_tau_b": kendall_tau_b,
        "pearson": pearson(pred_vals, actual_vals),
        "mae": mae,
        "bias": bias,
        "mae_centered": mae_centered,
        "mae_zscore": mae_zscore,
        "pred_std": pred_std,
        "actual_std": actual_std,
        "spread_ratio": spread_ratio,
        "top1_match": top1_match,
    }


def pairwise_accuracy_breakdown(
    predicted: dict[str, float],
    actual: dict[str, float],
    held_out_models: list[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Split pairwise accuracy by pair type, aligned with the held-out
    generalization analysis the harness reports at the very end.

    Every unordered model pair (m1, m2) is bucketed into exactly one of:

      - "visible-visible": BOTH m1 and m2 are in the visible set (not in
        held_out_models). This is the "in-distribution" view — the same
        pairs the agent could observe during its session.
      - "held-out-involved": AT LEAST ONE of m1/m2 is a held-out model.
        This MERGES the two previously-separate sub-categories
        (heldout-heldout and visible-heldout) into a single "held-out
        accuracy" number, because both measure generalization to models
        the agent never saw and should not be scored separately (a 3-model
        held-out set yields only 3 heldout-heldout pairs, far too few to
        be statistically meaningful on its own).

    Pairs whose actual scores tie (da == 0) are skipped, matching
    compute_metrics's convention. A predicted tie (dp == 0) counts toward
    `total` but not toward `correct`.

    Returns a dict keyed by pair type, each value holding:
        correct, total, accuracy (None if total == 0), concordant,
        discordant, tie_actual. Also returns a top-level "overall" entry
        that pools all pair types together (identical to compute_metrics's
        pairwise_accuracy when restricted to the same model subset).

    IMPORTANT: this function is ONLY for use by the harness's final,
    post-session held-out evaluation — it must never be called (and has no
    reason to be called) during the agent's session itself, since during
    the session the agent's workspace truth.json contains only visible
    models and there is no held-out data to compute this against.
    """
    held_out_set = set(held_out_models or [])
    models = sorted(set(predicted) & set(actual))

    def _bucket(m1: str, m2: str) -> str:
        if m1 not in held_out_set and m2 not in held_out_set:
            return "visible-visible"
        return "held-out-involved"

    stats: dict[str, dict[str, int]] = {
        "visible-visible":   {"correct": 0, "total": 0, "concordant": 0, "discordant": 0, "tie_actual": 0},
        "held-out-involved": {"correct": 0, "total": 0, "concordant": 0, "discordant": 0, "tie_actual": 0},
    }
    overall = {"correct": 0, "total": 0, "concordant": 0, "discordant": 0, "tie_actual": 0}

    for i in range(len(models)):
        for j in range(i + 1, len(models)):
            m1, m2 = models[i], models[j]
            dp = predicted[m1] - predicted[m2]
            da = actual[m1] - actual[m2]
            bucket = _bucket(m1, m2)

            def _tally(s: dict[str, int]) -> None:
                if da == 0:
                    s["tie_actual"] += 1
                    return
                s["total"] += 1
                if dp == 0:
                    return
                if (dp > 0) == (da > 0):
                    s["correct"] += 1
                    s["concordant"] += 1
                else:
                    s["discordant"] += 1

            _tally(stats[bucket])
            _tally(overall)

    out: dict[str, Any] = {}
    for k, s in stats.items():
        out[k] = {**s, "accuracy": (s["correct"] / s["total"]) if s["total"] else None}
    out["overall"] = {**overall, "accuracy": (overall["correct"] / overall["total"]) if overall["total"] else None}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--responses", type=Path, required=True,
                        help="Path to agent-authored responses.json (100 tasks x model responses)")
    parser.add_argument("--verify", type=Path, required=True, help="Path to verify.py")
    parser.add_argument("--truth", type=Path, required=True,
                        help="Path to truth.json: {model_name: real_swebench_pass_rate}")
    parser.add_argument("--output", type=Path, required=True, help="Output report JSON")
    parser.add_argument("--models", type=str, default=None,
                        help="Optional comma-separated subset of model names to evaluate "
                             "(for quick debugging). If omitted, uses every model in --truth.")
    args = parser.parse_args(argv)

    truth: dict[str, float] = json.loads(args.truth.read_text(encoding="utf-8"))
    tasks = json.loads(args.responses.read_text(encoding="utf-8"))
    if not isinstance(tasks, list):
        tasks = tasks.get("tasks", [])

    if len(tasks) < 2:
        print(f"ERROR: responses.json must contain at least 2 tasks (found {len(tasks)})", file=sys.stderr)
        return 1

    selected_models = set(args.models.split(",")) if args.models else set(truth)
    print(f"[evaluate] {len(tasks)} tasks x {len(selected_models)} models")

    verify_fn = load_verify(args.verify)

    per_model_predicted: dict[str, list[float]] = defaultdict(list)
    per_task_results: list[dict[str, Any]] = []

    for t in tasks:
        task_id = t.get("task_id", "unknown")
        prompt = t.get("prompt", "")
        reference_answer = t.get("reference_answer") or t.get("answer")
        model_responses = t.get("model_responses", {}) or {}

        for model, response in model_responses.items():
            if model not in selected_models:
                continue
            task_for_verify = {
                "task_id": task_id,
                "prompt": prompt,
                "reference_answer": reference_answer,
                "model": model,
            }
            try:
                predicted_score = float(verify_fn(response, reference_answer, task_for_verify))
                predicted_score = max(0.0, min(1.0, predicted_score))
            except Exception:
                predicted_score = 0.0

            per_model_predicted[model].append(predicted_score)
            per_task_results.append({
                "task_id": task_id,
                "model": model,
                "predicted": predicted_score,
            })

    missing_models = selected_models - set(per_model_predicted)
    if missing_models:
        print(f"WARNING: no responses found for models: {sorted(missing_models)}", file=sys.stderr)

    predicted_rates = {
        model: (sum(scores) / len(scores) if scores else 0.0)
        for model, scores in per_model_predicted.items()
    }
    actual_rates = {m: truth[m] for m in selected_models if m in truth}

    metrics = compute_metrics(predicted_rates, actual_rates)

    report = {
        "summary": {
            "num_tasks": len(tasks),
            "num_models": len(set(predicted_rates) & set(actual_rates)),
            "num_scored_pairs": len(per_task_results),
        },
        "model_metrics": metrics,
        "per_model": {
            model: {
                "predicted_score": predicted_rates.get(model),
                "actual_swebench_pass_rate": actual_rates.get(model),
                "num_tasks_scored": len(per_model_predicted.get(model, [])),
            }
            for model in sorted(set(predicted_rates) | set(actual_rates))
        },
        "per_task": per_task_results,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Report written to {args.output}")
    print(f"Models: {report['summary']['num_models']}, Tasks: {report['summary']['num_tasks']}")
    print(f"Pairwise accuracy: {metrics['pairwise_accuracy']}")
    print(f"Spearman: {metrics['spearman']}")
    print(f"Top-1 match: {metrics['top1_match']}")
    print(f"MAE (raw): {metrics['mae']}")
    print(f"  bias (predicted - actual, mean offset): {metrics['bias']}")
    print(f"  mae_centered (bias-corrected MAE): {metrics['mae_centered']}")
    print(f"  mae_zscore (fully normalized MAE): {metrics['mae_zscore']}")
    print(f"  spread_ratio (pred_std / actual_std, <<1 means scores don't "
          f"discriminate between models): {metrics['spread_ratio']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
