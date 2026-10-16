#!/usr/bin/env python3
"""Verifier stub — the agent may edit this freely.

This is called once per (task, model) pair by evaluate.py, over the 100
tasks the agent itself designed (see responses.json). It must score a
single model response in [0.0, 1.0] — how well that response solves the
agent's own task. There is no ground-truth answer key for these new tasks
(the agent wrote them), so the agent must design a scoring strategy itself.

Signature:
    verify(response: str, answer: str | None, task: dict) -> float

task dict contains: task_id, prompt, reference_answer, model.

The agent is free to implement any strategy:
  - exact match / rubric against its own reference_answer
  - LLM-as-judge (call another model to grade the response)
  - programmatic checks (e.g. run generated code, check output)
  - partial credit

------------------------------------------------------------------------
CALLING AN LLM FROM verify.py (LLM-as-judge / generating new eval data)
------------------------------------------------------------------------
If your strategy needs to call a model (e.g. as a judge, or to generate a
fresh answer to compare against), POST to the CANDIDATE_MODEL_ENDPOINT
environment variable (already set for you), Anthropic-Messages-style:

    import os, json, urllib.request
    req = urllib.request.Request(
        os.environ["CANDIDATE_MODEL_ENDPOINT"],
        data=json.dumps({
            "model": model_name,   # exactly as it appears in truth.json
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "..."}],
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
        text = "".join(c["text"] for c in data["content"] if c["type"] == "text")

No API key, appid, or other credential is needed — it's handled for you.

"""

from __future__ import annotations


def verify(response: str, answer: str | None, task: dict) -> float:
    """Return a score in [0.0, 1.0] for how well `response` solves the task.

    Placeholder default: naive non-empty-response heuristic. Replace this
    with a real scoring strategy (rubric, LLM-judge, exact match, etc.).
    """
    if response and len(response.strip()) > 0:
        return 0.5
    return 0.0
