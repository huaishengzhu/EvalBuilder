#!/usr/bin/env python3
"""OpenCode entry point for a benchmark-design research harness.

The agent's job: design a LIGHTWEIGHT, from-scratch substitute for SWE-Bench
Verified — brand-new tasks that don't require spinning up a repo/sandbox and
running the real test suite to score (unlike SWE-Bench itself), scored
instead by a self-authored verify.py (programmatic checks, rubrics,
LLM-as-judge, etc.). It collects each candidate model's real response to
every task (by calling the model APIs itself), writes verify.py, and checks
— via evaluate.py — whether that lightweight benchmark + verifier reproduces
the same model ranking as the models' real, heavyweight SWE-Bench Verified
pass rates (computed once, up front, from results_agent/, and handed to the
agent as truth.json — a model-level aggregate only, with no per-task answer
key, so there is nothing to reverse-engineer for the agent's own tasks). To
help it design tasks/verify.py that track genuine SWE-Bench-style signal
rather than something arbitrary, the agent also gets read-only access to a
sample of REAL SWE-Bench Verified trajectories for the visible models (see
REFERENCE TRAJECTORIES below) — it must still design brand-new tasks, not
reuse or lightly reword any of these real ones.

REFERENCE TRAJECTORIES (real SWE-Bench data, VISIBLE models only)
-------------------------------------------------------------------
workspace/reference_trajectories/ is a SYMLINK into DEFAULT_DATA_TRAIN_DIR
(data_train/, a fixed, repo-level directory sibling to data/) containing the
real SWE-Bench Verified trajectory files for every VISIBLE model — the same
files that live in data/results_agent/, minus the HELD-OUT models'
directories, which are never copied into data_train/ in the first place (see
link_reference_trajectories). It's a symlink rather than a per-run copy
because these files are large (tens of MB each) and identical across runs.
The agent may read as much of this as it wants to understand real
SWE-Bench-style task difficulty/style and where weaker vs. stronger models
concretely diverge, to better inform its own lightweight task/verify.py
design — but its own benchmark tasks must remain brand-new, not reused or
lightly-reworded SWE-Bench tasks.

HELD-OUT MODELS (generalization check)
---------------------------------------
Of all the models in results_agent/, a fixed subset (default: claude-opus-4.7,
gpt-5.5, kimi-k2.6 — see DEFAULT_HELD_OUT_MODELS/--held-out-models) is kept
COMPLETELY hidden from the agent for the entire session: they never appear
in the workspace's truth.json, and the CANDIDATE_MODEL_ENDPOINT proxy
physically refuses (403) any request naming one of them (see
`_candidate_model_proxy`'s allowed_models check) — this isn't just an
instruction the agent is asked to follow, there is no code path from inside
the workspace/session that can reach them. Only AFTER the agent's entire
session has ended does `run_held_out_evaluation` call these held-out models
directly (from run.py's own process, never from anything the agent can
invoke) on the agent's own final task prompts, then re-run the fixed
evaluate.py against the FULL model set (visible + held-out) with the FULL
truth.json. The resulting report_full.json breaks metrics out three ways:
overall (all models), visible-only, and held-out-only — so it's possible to
see directly whether the agent's benchmark design generalizes to models it
never had a way to see or tune against, rather than merely fitting the 7 it
could observe.

STRUCTURED ITERATION MODEL
---------------------------
run.py creates a workspace, computes a VISIBLE-models-only truth.json from
results_agent/ (see HELD-OUT MODELS above), copies verify.py + evaluate.py
into it, and then drives OpenCode through a series of EXPLICIT, VERSIONED
ITERATIONS bounded by a TOTAL wall-clock time budget (default: 4 hours).
Unlike a single freeform session, every iteration is materialized on disk
as its own directory:

    workspace/
      truth.json            (fixed, read-only ground truth)
      evaluate.py            (fixed, agent must not edit)
      verify.py               <- agent's current, "live" working copy
      responses.json          <- agent's current, "live" working copy
      reference_trajectories/ (fixed, read-only symlink to data_train/ —
                                real SWE-Bench trajectories, visible models
                                only; see REFERENCE TRAJECTORIES above)
      iterations/
        iter_1/
          verify.py            snapshot of verify.py at the end of iter 1
          responses.json       snapshot of responses.json at the end of iter 1
          report.json          evaluate.py output for this iteration
          summary.json         agent-authored write-up (see below)
        iter_2/
          ...
        iter_N/
      tmp_operations/        <- ALL scratch/helper scripts + intermediate
                                 artifacts go here (collection scripts,
                                 judge scripts, draft task/response JSON,
                                 retry scripts, etc.) — see "WORKSPACE ROOT
                                 MUST STAY CLEAN" below. Nothing the agent
                                 writes should ever land directly in the
                                 workspace root except the four live files
                                 above, reference_trajectories/, and the
                                 iterations/ directory.

The workspace root is intentionally kept to EXACTLY the files the final,
official evaluation needs (truth.json, evaluate.py, verify.py,
responses.json) plus the iterations/ history and the reference_trajectories/
symlink — every helper/scratch script and every intermediate JSON draft the
agent writes while working (e.g. "collect_v2.py", "judge_new.py",
"harder_tasks.json", retry scripts, etc.) must be written into
workspace/tmp_operations/ instead of the workspace root, so the root stays
exactly as clean as what gets graded.

The agent calls candidate/judge models via the CANDIDATE_MODEL_ENDPOINT
environment variable (an Anthropic-Messages-compatible local HTTP proxy
that run.py starts and tears down around each OpenCode round — see
_candidate_model_proxy below) rather than any file in the workspace. This
keeps all internal credential/routing details (which live in a LOCAL,
NOT-CHECKED-IN config file — see load_candidate_model_routes) completely
out of both the workspace and this open-sourceable codebase.

Before each iteration, the agent is instructed to first read every prior
iter_*/summary.json + report.json to decide whether to keep iterating on
the current live verify.py/responses.json, or to ROLL BACK to an earlier
iteration's snapshot (copying an old iter_K's files back into the live
verify.py/responses.json) if a later change regressed the metrics.

At the end of each iteration the agent must write iterations/iter_N/
summary.json explaining: what it changed this round, why, how it differs
from the previous iteration, what it saw when validating mid-round, and
this round's final metrics (also captured automatically by run.py via
evaluate.py). run.py then snapshots the live verify.py/responses.json into
iterations/iter_N/ and runs the fixed evaluate.py to produce that
iteration's report.json — this happens regardless of what the agent did,
so no iteration is ever lost even if the agent forgets a step.

run.py drives this in TWO explicit phases, both bounded by a single TOTAL
wall-clock time budget. There is NO fixed per-iteration time limit — the
agent itself decides how long each iteration round takes (a round simply
ends when the agent finishes its turn, typically right after running
evaluate.py and writing summary.json). The only hard subprocess timeout
that exists is a per-round BACKSTOP set to "whatever total budget remains"
— it exists purely to protect against a stuck/runaway round, and is not a
pacing mechanism; pacing is left entirely to the agent, which is shown its
own historical iteration lengths + the remaining total budget so it can
plan accordingly:

  PHASE 1 (iteration loop): keeps launching iteration rounds (each a
  `--continue` of the same OpenCode session, so full conversation
  history/context carries over). It does NOT stop early just because a
  round already produced a "valid" result — the whole point is to keep
  refining for as long as budget allows. This loop stops (without ever
  mentioning "the last iteration" to the agent) once the remaining total
  budget drops below a small minimum threshold.

  PHASE 2 (finalize round): exactly ONE dedicated final round, using an
  entirely separate prompt whose only job is telling the agent time is
  almost up and that it must, right now, either (a) pick whichever single
  iteration it judges best and copy that exact verify.py + responses.json
  into the workspace root, or (b) hand-assemble a final responses.json by
  merging the best task records out of several iterations. This round gets
  whatever total budget remains (no reserve was held back for it) and IS
  hard-killed if it runs over, since there is no round after it to fall
  back on — that forced kill is what guarantees the agent actually submits
  something rather than exploring indefinitely.

After PHASE 2 ends (cleanly or killed), run.py runs the OFFICIAL,
authoritative evaluation EXACTLY ONCE, against ONLY the verify.py +
responses.json that are sitting directly in the workspace root at that
moment — i.e. whatever the agent itself chose as its final submission.
There is deliberately NO automatic fallback to iterations/iter_K/: once
the time budget is gone, there is no more evaluation opportunity, so the
agent must have copied/assembled its best files into the workspace root
itself during PHASE 2. That single run is recorded as the run's final
report.json, regardless of what the agent did during the session.

Usage:
    python run.py --run-name my_experiment --time-budget-minutes 360 \\
        --num-tasks 50
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator


REPO = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Anthropic stream proxy (normalizes usage fields)
# ---------------------------------------------------------------------------


def _fill_usage_defaults(usage: dict) -> None:
    for field in ("input_tokens", "output_tokens",
                  "cache_creation_input_tokens", "cache_read_input_tokens"):
        usage.setdefault(field, 0)


def _normalize_event(line: bytes) -> bytes:
    if not line.startswith(b"data:"):
        return line
    payload = line[5:].lstrip()
    try:
        event = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return line
    if isinstance(event, dict):
        # Top-level usage (e.g. message_delta events: {"usage": {...}})
        usage = event.get("usage")
        if isinstance(usage, dict):
            _fill_usage_defaults(usage)
        # Nested usage inside "message" (e.g. message_start events:
        # {"message": {..., "usage": {...}}})
        message = event.get("message")
        if isinstance(message, dict):
            nested_usage = message.get("usage")
            if isinstance(nested_usage, dict):
                _fill_usage_defaults(nested_usage)
    return b"data: " + json.dumps(event, separators=(",", ":")).encode() + b"\n"


@contextmanager
def _stream_proxy(upstream: str) -> Iterator[str]:
    upstream = upstream.rstrip("/")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {
                k: v for k, v in self.headers.items()
                if k.lower() not in {"host", "content-length", "connection", "accept-encoding"}
            }
            req = urllib.request.Request(
                f"{upstream}{self.path}", data=body, headers=headers, method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=900) as response:
                    self.send_response(response.status)
                    for k, v in response.headers.items():
                        if k.lower() not in {"content-length", "transfer-encoding", "connection"}:
                            self.send_header(k, v)
                    self.end_headers()
                    try:
                        for resp_line in response:
                            normalized = _normalize_event(resp_line)
                            self.wfile.write(normalized)
                            self.wfile.flush()
                            if b'"type":"message_stop"' in normalized:
                                break
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                self.send_response(exc.code)
                self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
                self.end_headers()
                self.wfile.write(payload)

        def log_message(self, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Candidate-model proxy — lets the agent call any candidate/judge model by
# name (e.g. "glm-5.1", "claude-opus-4.7", "gpt-5.5", ...) via a single
# local HTTP endpoint, with NO notion of internal credentials/routing
# exposed to the agent at all.
#
# Routing (which real endpoint/credential/model-name a given candidate name
# maps to) is loaded from a LOCAL, NOT-CHECKED-IN config file (like
# ~/.claude/settings.json is for the harness's own model). This keeps
# run.py itself free of any hardcoded internal credentials, which matters
# because this repo is meant to be open-sourced: the calling rules for an
# internal gateway won't even apply outside the company, so they must live
# in local environment config, not in code.
#
# Config file format (JSON), default path ~/.autoeval/candidate_models.json,
# overridable via --candidate-models-config:
#   {
#     "default": {"endpoint": "...", "api_key": "..."},
#     "overrides": {
#       "<candidate_model_name>": {
#         "endpoint": "...",        # optional, falls back to default's
#         "api_key": "...",         # optional, falls back to default's
#         "model_name": "..."       # optional, actual name sent upstream
#       }, ...
#     }
#   }
# If the config file is missing, every candidate model is called directly
# by its own name with no credential rewriting (the agent's HTTP calls will
# then simply fail/401 unless the upstream doesn't require auth — this is
# expected for anyone who hasn't set up the config file yet).
# ---------------------------------------------------------------------------

DEFAULT_CANDIDATE_MODELS_CONFIG = Path.home() / ".autoeval" / "candidate_models.json"

# HELD-OUT models: excluded from the agent entirely (not in the workspace's
# truth.json, not callable through CANDIDATE_MODEL_ENDPOINT — see
# `_candidate_model_proxy`'s allowed_models check) for the ENTIRE session.
# After the agent's final verify.py/responses.json are locked in, the
# harness (run_held_out_evaluation, called from main() only, never from
# anything reachable during the session) calls these models directly on the
# agent's own final task prompts and folds their responses into the TRUE
# final report, alongside a full truth.json that does include their real
# SWE-Bench pass rates. This makes them a genuine generalization test: the
# agent can optimize verify.py/tasks as hard as it wants against the 7
# VISIBLE models without that optimization being able to target these 3 at
# all, since it never has any data about them, nor any network path to
# query them, until after its session has already ended.
DEFAULT_HELD_OUT_MODELS = ["claude-opus-4.7", "gpt-5.5", "kimi-k2.6"]

# Fixed, repo-level directory (sibling of data/) holding the REAL SWE-Bench
# Verified trajectory files for the VISIBLE models only (i.e. every
# data/results_agent/<model>/ directory EXCEPT DEFAULT_HELD_OUT_MODELS,
# copied in once, out of band — NOT regenerated per run). Every run's
# workspace gets a symlink to this SAME directory (see
# link_reference_trajectories) instead of a fresh per-run copy, since these
# trajectory files are large (tens of MB each) and identical across runs.
# If the held-out model set is ever changed, this directory must be rebuilt
# by hand to match — it is not derived automatically from --held-out-models.
DEFAULT_DATA_TRAIN_DIR = REPO / "data_train"


def load_candidate_model_routes(config_path: Path) -> dict[str, Any]:
    """Load the local (not-checked-in) candidate-model routing config.

    Returns {} if the file doesn't exist — callers should treat that as
    "no special routing, call every model directly by name."
    """
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: failed to read {config_path}: {exc}", file=sys.stderr)
        return {}


def _resolve_candidate_route(routes: dict[str, Any], model_name: str) -> tuple[str | None, str | None, str]:
    """Return (endpoint, api_key, actual_model_name) for `model_name` given
    the routes config (as returned by load_candidate_model_routes)."""
    default = routes.get("default") or {}
    override = (routes.get("overrides") or {}).get(model_name) or {}
    endpoint = override.get("endpoint") or default.get("endpoint")
    api_key = override.get("api_key") or default.get("api_key")
    actual_model_name = override.get("model_name") or model_name
    return endpoint, api_key, actual_model_name


@contextmanager
def _candidate_model_proxy(
    routes: dict[str, Any], allowed_models: set[str] | None = None,
) -> Iterator[str]:
    """Start a local HTTP server the agent can POST Anthropic-Messages-style
    requests to (`{"model": "<candidate_model_name>", "messages": [...]}`),
    with no path/auth requirements beyond that. It rewrites the request's
    endpoint, Authorization header, and "model" field according to `routes`
    before forwarding upstream, and streams the response back unchanged.

    This is the ONLY thing agent code needs to know: POST to this URL with
    a candidate model's name (exactly as it appears in truth.json) — no
    appid/credential concept is ever exposed to the agent.

    If `allowed_models` is given, any request for a model name NOT in that
    set is rejected with a 403 and never forwarded upstream at all — this
    is how the HELD-OUT models are kept physically unreachable from inside
    the agent's workspace/session, not just "asked nicely" not to be
    called: even if the agent somehow guesses a held-out model's exact
    name (e.g. from a stale memory of a similar benchmark), this proxy is
    the only network path it has to any model, and it simply will not
    route requests for names outside `allowed_models`, regardless of what
    routes/credentials exist in the underlying config.
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_POST(self) -> None:
            raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            try:
                payload = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                payload = {}
            model_name = payload.get("model", "")
            if allowed_models is not None and model_name not in allowed_models:
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": {
                        "message": f"Model '{model_name}' is not one of the candidate "
                                   f"models available to you in this session."
                    }
                }).encode("utf-8"))
                return
            endpoint, api_key, actual_model_name = _resolve_candidate_route(routes, model_name)
            if not endpoint:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": {
                        "message": f"No endpoint configured for candidate model "
                                   f"'{model_name}'. Set up a local candidate-models "
                                   f"config (see run.py --candidate-models-config)."
                    }
                }).encode("utf-8"))
                return
            payload["model"] = actual_model_name
            body = json.dumps(payload).encode("utf-8")
            headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=180) as response:
                    resp_body = response.read()
                    self.send_response(response.status)
                    for k, v in response.headers.items():
                        if k.lower() not in {"content-length", "transfer-encoding", "connection"}:
                            self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(resp_body)
            except urllib.error.HTTPError as exc:
                payload_bytes = exc.read()
                self.send_response(exc.code)
                self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
                self.end_headers()
                self.wfile.write(payload_bytes)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": {"message": str(exc)}}).encode("utf-8"))

        def log_message(self, *_args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.block_on_close = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_evaluate_subprocess(
    evaluate_path: Path, responses_path: Path, verify_path: Path, truth_path: Path,
    output_path: Path, cwd: Path, candidate_model_routes: dict[str, Any] | None,
) -> subprocess.CompletedProcess:
    """Thin wrapper around `subprocess.run([... evaluate.py ...])` that ALSO
    spins up a short-lived `_candidate_model_proxy` (unrestricted — no
    `allowed_models` cap, since by the time this runs the agent's session
    is already over and there is nothing left to keep hidden from it) and
    injects its URL as CANDIDATE_MODEL_ENDPOINT into the subprocess's
    environment.

    This matters because verify.py's LLM-judge cache is keyed by
    `(task_id, model, sha1(response))` — ANY time the exact response text
    being scored differs even slightly from what was cached when the
    verify.py author (the agent) built it (e.g. because it's the harness's
    OWN freshly re-collected response, as in `run_held_out_evaluation` /
    `run_final_evaluation`, or simply a different sampling of the same
    model), verify.py falls back to `_judge_live()`, which calls out to
    `CANDIDATE_MODEL_ENDPOINT`. Every place run.py invokes evaluate.py as a
    subprocess (validity checks between iterations, the official/held-out/
    anti-cheat-confirmation evaluations after the session ends) runs from
    run.py's own process, which never has this env var set — only the
    per-iteration `round_env` handed to the OpenCode agent subprocess did,
    and that proxy is torn down at the end of every round. Without this
    wrapper, every cache-miss judge call silently fails (empty endpoint ->
    "" response -> score defaults to 0.5), which after verify.py's own
    score calibration can look like a catastrophic, systematic collapse in
    every model's predicted score that has nothing to do with actual
    response quality.

    If `candidate_model_routes` is falsy (e.g. no candidate-models config
    was ever loaded), this degrades gracefully to plain `subprocess.run`
    with no CANDIDATE_MODEL_ENDPOINT — same behavior as before this
    wrapper existed.
    """
    cmd = [
        sys.executable, str(evaluate_path),
        "--responses", str(responses_path),
        "--verify", str(verify_path),
        "--truth", str(truth_path),
        "--output", str(output_path),
    ]
    if not candidate_model_routes:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    with _candidate_model_proxy(candidate_model_routes) as candidate_proxy_url:
        env = dict(os.environ)
        env["CANDIDATE_MODEL_ENDPOINT"] = f"{candidate_proxy_url}/v1/messages"
        return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


def _opencode_bin() -> str:
    configured = os.environ.get("OPENCODE_BIN")
    if configured:
        return configured
    found = shutil.which("opencode")
    if found:
        return found
    raise RuntimeError("opencode executable not found; install it (see "
                        "https://opencode.ai) or set OPENCODE_BIN to its path")


def _opencode_env(settings_path: Path, requested_model: str | None) -> tuple[dict[str, str], str]:
    settings = json.loads(settings_path.expanduser().read_text(encoding="utf-8"))
    env_map = settings.get("env") or {}
    base_url = env_map.get("ANTHROPIC_BASE_URL")
    auth_token = env_map.get("ANTHROPIC_AUTH_TOKEN")
    if not base_url:
        raise ValueError("ANTHROPIC_BASE_URL missing from settings")
    if not auth_token:
        raise ValueError("ANTHROPIC_AUTH_TOKEN missing from settings")
    model_id = requested_model or env_map.get("ANTHROPIC_MODEL")
    if not model_id:
        raise ValueError("Provide --model or set ANTHROPIC_MODEL")
    opencode_base_url = f"{base_url.rstrip('/')}/v1"
    env = os.environ.copy()
    env["OPENCODE_ANTHROPIC_BASE_URL"] = opencode_base_url
    env["ANTHROPIC_AUTH_TOKEN"] = auth_token
    env["ANTHROPIC_API_KEY"] = auth_token
    provider_model = model_id if "/" in model_id else f"custom/{model_id}"
    if "OPENCODE_CONFIG_CONTENT" not in env:
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                "custom": {
                    "npm": "@ai-sdk/anthropic",
                    "name": "Custom Anthropic-Compatible Gateway",
                    "options": {
                        "baseURL": "{env:OPENCODE_ANTHROPIC_BASE_URL}",
                        "apiKey": "{env:ANTHROPIC_API_KEY}",
                        "headers": {"Authorization": "Bearer {env:ANTHROPIC_AUTH_TOKEN}"},
                    },
                    "models": {
                        model_id: {"name": model_id, "limit": {"context": 200000, "output": 16384}},
                    },
                }
            },
            "model": provider_model,
        })
    return env, env.get("OPENCODE_MODEL", provider_model)


# ---------------------------------------------------------------------------
# truth.json builder — ground-truth model ranking signal
# ---------------------------------------------------------------------------


def build_truth_json(results_agent_dir: Path, output_path: Path) -> dict[str, float]:
    """Compute each model's real SWE-Bench Verified pass rate from
    results_agent/ trajectories and write it out as a flat, model-level
    mapping: {model_name: pass_rate}.

    This is the ONLY ground truth the agent gets. It is a model-level
    aggregate with no per-task answer key, so it cannot be used to
    reverse-engineer correct answers to the agent's own new tasks —
    it can only be used, after the fact, to judge whether the agent's
    from-scratch benchmark + verifier reproduces the same model ordering.
    """
    truth: dict[str, float] = {}

    for model_dir in sorted(results_agent_dir.iterdir()):
        if not model_dir.is_dir():
            continue
        model_name = model_dir.name
        total = 0
        passed = 0
        for traj_file in model_dir.iterdir():
            if traj_file.suffix != ".jsonl":
                continue
            with traj_file.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    code_test = rec.get("code_test", {})
                    correct = rec.get("correct", code_test.get("is_resolved", False))
                    total += 1
                    if correct:
                        passed += 1
        if total:
            truth[model_name] = round(passed / total, 4)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
    print(f"Built truth.json: {len(truth)} models from {results_agent_dir}")
    return truth


# ---------------------------------------------------------------------------
# Reference trajectories — a SYMLINK (not a copy) into the workspace pointing
# at DEFAULT_DATA_TRAIN_DIR, a fixed, pre-populated, repo-level directory
# (sibling of data/) that holds the REAL SWE-Bench Verified trajectory files
# for the VISIBLE models only. It is built ONCE, up front (see the module
# docstring / DEFAULT_DATA_TRAIN_DIR), by copying every data/results_agent/
# model directory EXCEPT the held-out ones into data_train/ — held-out
# models' directories are simply never copied into it in the first place, so
# there is nothing in data_train/ to accidentally leak even if the agent
# reads every byte of it. Because this is a symlink into a directory that
# already lives outside any single run's workspace, run.py never re-copies
# these (large, ~50-100MB each) trajectory files per run — every run just
# links to the same on-disk data.
# ---------------------------------------------------------------------------


def link_reference_trajectories(data_train_dir: Path, output_link: Path) -> None:
    """Create `output_link` (a path inside the agent's workspace) as a
    symlink pointing at `data_train_dir` — the fixed, repo-level directory
    that was already populated (once, out of band, not per-run) with only
    the VISIBLE models' real SWE-Bench Verified trajectory files.

    This function does NOT filter/copy anything itself and does NOT know
    about held-out models at all: by the time this runs, data_train_dir is
    assumed to already contain only what's safe for the agent to see (see
    the module docstring for how it's populated). If data_train_dir doesn't
    exist, this is a no-op (with a warning) rather than a hard failure, so
    an unrelated missing-reference-data setup issue doesn't block a run.
    """
    if not data_train_dir.is_dir():
        print(f"WARNING: {data_train_dir} does not exist or is not a directory — "
              f"skipping reference_trajectories symlink (agent will not see any "
              f"reference SWE-Bench trajectories this run)", file=sys.stderr)
        return
    if output_link.exists() or output_link.is_symlink():
        return
    output_link.symlink_to(data_train_dir, target_is_directory=True)
    print(f"Linked reference_trajectories/ -> {data_train_dir}")


# ---------------------------------------------------------------------------
# Result validity check — decides whether a given verify.py/responses.json
# pair is actually usable (used both for the live workspace files and for
# validating/snapshotting each iteration)
# ---------------------------------------------------------------------------


_PLACEHOLDER_VERIFY_MARKER = "Placeholder default: naive non-empty-response heuristic"


def check_valid_result(
    responses_path: Path, verify_path: Path, truth_path: Path, evaluate_path: Path,
    candidate_models: list[str], num_tasks: int, min_task_fraction: float = 0.8,
    run_cwd: Path | None = None,
    candidate_model_routes: dict[str, Any] | None = None,
) -> tuple[bool, str, dict[str, Any] | None, float]:
    """Check whether a given (responses.json, verify.py) pair is genuinely
    usable:

      - responses.json exists, is a list, and has at least
        min_task_fraction * num_tasks task records
      - each task record has a model_responses entry for every candidate
        model
      - verify.py has actually been edited away from the placeholder stub
      - evaluate.py can run against these files and produce a real
        (non-null) spearman metric

    Returns (is_valid, reason, report_dict_or_None, evaluate_seconds).
    report_dict is the parsed evaluate.py output when it could be run, even
    if the result is ultimately judged invalid (e.g. spearman is null) —
    callers can use it for logging/snapshotting. evaluate_seconds is how
    long the actual `python evaluate.py ...` subprocess call took (0.0 if
    it was never reached, e.g. because an earlier cheap check already
    failed) — this is surfaced back up to the agent so it can see how
    expensive each evaluate.py run is and budget its own time accordingly.
    """
    if not responses_path.exists():
        return False, "responses.json does not exist yet", None, 0.0

    try:
        tasks = json.loads(responses_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return False, f"responses.json is not valid JSON: {exc}", None, 0.0

    if not isinstance(tasks, list):
        return False, "responses.json is not a JSON list of task records", None, 0.0

    min_tasks = max(2, int(num_tasks * min_task_fraction))
    if len(tasks) < min_tasks:
        return False, f"only {len(tasks)}/{num_tasks} tasks present (need >= {min_tasks})", None, 0.0

    missing_model_tasks = 0
    for t in tasks:
        model_responses = t.get("model_responses") if isinstance(t, dict) else None
        if not isinstance(model_responses, dict):
            missing_model_tasks += 1
            continue
        if not all(m in model_responses and model_responses[m] for m in candidate_models):
            missing_model_tasks += 1
    covered_fraction = 1 - (missing_model_tasks / len(tasks) if tasks else 1)
    if covered_fraction < min_task_fraction:
        return False, (
            f"only {covered_fraction*100:.0f}% of tasks have responses from ALL "
            f"{len(candidate_models)} candidate models (need >= {min_task_fraction*100:.0f}%)"
        ), None, 0.0

    if not verify_path.exists():
        return False, "verify.py does not exist", None, 0.0
    verify_text = verify_path.read_text(encoding="utf-8")
    if _PLACEHOLDER_VERIFY_MARKER in verify_text:
        return False, "verify.py is still the untouched placeholder stub", None, 0.0

    if not truth_path.exists():
        return False, "truth.json is missing", None, 0.0

    # Try actually running evaluate.py to make sure it produces a real
    # metric — timed, so the harness (and, via summary.json, the agent) can
    # see how expensive each evaluate.py invocation is as num_tasks/model
    # count grow.
    tmp_report = (run_cwd or verify_path.parent) / f"_validity_check_report_{os.getpid()}.json"
    eval_start = time.monotonic()
    result = run_evaluate_subprocess(
        evaluate_path, responses_path, verify_path, truth_path, tmp_report,
        cwd=(run_cwd or verify_path.parent), candidate_model_routes=candidate_model_routes,
    )
    evaluate_seconds = round(time.monotonic() - eval_start, 1)
    if result.returncode:
        return False, f"evaluate.py failed: {result.stderr[-500:]}", None, evaluate_seconds
    try:
        report = json.loads(tmp_report.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError, UnicodeDecodeError):
        return False, "evaluate.py did not produce a readable report", None, evaluate_seconds
    finally:
        tmp_report.unlink(missing_ok=True)

    metrics = report.get("model_metrics", {})
    if metrics.get("spearman") is None:
        return (False, "evaluate.py ran but spearman metric is null (not enough model coverage)",
                report, evaluate_seconds)

    return (True, "valid: responses.json + verify.py produce real ranking metrics",
            report, evaluate_seconds)


# ---------------------------------------------------------------------------
# Iteration bookkeeping — iterations/iter_N/{verify.py, responses.json,
# report.json, summary.json} on disk, plus helpers to snapshot the live
# workspace files into a new iteration and to summarize prior iterations
# for the agent to read before starting the next one.
# ---------------------------------------------------------------------------


_REQUIRED_SUMMARY_FIELDS = [
    "what_changed", "why", "compared_to_previous", "intermediate_validation",
]


def _iter_dir(workspace: Path, iteration: int) -> Path:
    return workspace / "iterations" / f"iter_{iteration}"


def _existing_iterations(workspace: Path) -> list[int]:
    iterations_root = workspace / "iterations"
    if not iterations_root.exists():
        return []
    found = []
    for child in iterations_root.iterdir():
        if child.is_dir() and child.name.startswith("iter_"):
            suffix = child.name[len("iter_"):]
            if suffix.isdigit():
                found.append(int(suffix))
    return sorted(found)


def snapshot_iteration(
    workspace: Path, iteration: int, truth_path: Path, evaluate_path: Path,
    candidate_models: list[str], num_tasks: int, round_elapsed_seconds: int | None = None,
    round_budget_seconds: int | None = None,
    candidate_model_routes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy the live workspace/verify.py + responses.json into
    iterations/iter_N/, run evaluate.py against that snapshot to produce
    iter_N/report.json, and make sure iter_N/summary.json exists (writing a
    minimal auto-generated placeholder if the agent forgot to write one, so
    no iteration is ever silently lost).

    round_elapsed_seconds/round_budget_seconds (how long this OpenCode round
    actually ran vs. how long it was allowed to run) and the harness's own
    evaluate_seconds (how long the validity-check evaluate.py call itself
    took) are recorded into summary.json's "timing" field, so the agent can
    see — via build_history_digest on the NEXT round — exactly how it (and
    evaluate.py) spent time historically and plan its remaining iterations
    accordingly, instead of flying blind on pacing.

    Returns a small dict describing what happened, used for logging and for
    building the history digest shown to the agent before the next round:
        {iteration, has_live_files, is_valid, reason, metrics_or_None,
         summary_written_by_agent, evaluate_seconds, round_elapsed_seconds,
         round_budget_seconds}
    """
    live_verify = workspace / "verify.py"
    live_responses = workspace / "responses.json"

    iter_dir = _iter_dir(workspace, iteration)
    iter_dir.mkdir(parents=True, exist_ok=True)

    has_live_files = live_verify.exists() and live_responses.exists()
    if live_verify.exists():
        shutil.copy2(live_verify, iter_dir / "verify.py")
    if live_responses.exists():
        shutil.copy2(live_responses, iter_dir / "responses.json")

    is_valid, reason, report, evaluate_seconds = check_valid_result(
        iter_dir / "responses.json", iter_dir / "verify.py", truth_path, evaluate_path,
        candidate_models, num_tasks, run_cwd=iter_dir,
        candidate_model_routes=candidate_model_routes,
    )
    if report is not None:
        (iter_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    timing = {
        "round_elapsed_seconds": round_elapsed_seconds,
        "round_budget_seconds": round_budget_seconds,
        "harness_evaluate_seconds": evaluate_seconds,
    }
    # is_valid/reason come from check_valid_result above (run by run.py
    # itself, NOT self-reported by the agent) — this is the harness's own
    # verdict on this round's snapshot, and MUST be persisted into
    # summary.json so build_history_digest can surface it back to the agent
    # on the next round. Before this was added, is_valid/reason only lived
    # in this function's in-memory return value and were never written to
    # disk, so the agent had no way to see WHY a round was rejected (e.g.
    # insufficient candidate-model coverage) from the history digest alone —
    # it could only infer "no metrics" and had to guess why.
    validity = {"is_valid": is_valid, "reason": reason}

    summary_path = iter_dir / "summary.json"
    summary_written_by_agent = summary_path.exists()
    ready_to_submit = False
    if not summary_written_by_agent:
        # Agent forgot (or ran out of time) to write its own summary — leave
        # an honest auto-generated placeholder rather than losing the round.
        auto_summary = {
            "iteration": iteration,
            "what_changed": "(auto-generated — agent did not write summary.json this round)",
            "why": None,
            "compared_to_previous": None,
            "intermediate_validation": None,
            "rolled_back_to": None,
            "ready_to_submit": False,
            "final_metrics_this_iter": (report or {}).get("model_metrics"),
            "timing": timing,
            "validity": validity,
            "auto_generated": True,
        }
        summary_path.write_text(json.dumps(auto_summary, indent=2) + "\n", encoding="utf-8")
    else:
        # Backfill the metrics/timing/validity run by run.py itself into the
        # agent's summary (in case the agent only pasted its own ad-hoc
        # numbers), without touching the fields the agent wrote.
        try:
            existing = json.loads(summary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            existing = {}
        if isinstance(existing, dict):
            existing.setdefault("iteration", iteration)
            existing["final_metrics_this_iter_official"] = (report or {}).get("model_metrics")
            existing["timing"] = timing
            existing["validity"] = validity
            summary_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
            # The agent may voluntarily signal it believes the current live
            # verify.py/responses.json are already its best possible
            # submission and no further iteration is needed (see
            # "ready_to_submit" in `_build_iteration_prompt`). Only honor
            # this early-submit signal when the harness's OWN validity
            # check (is_valid above) agrees the snapshot is actually usable
            # — an agent claiming "done" on a snapshot the harness itself
            # would reject (e.g. insufficient model coverage, placeholder
            # verify.py) is almost certainly a mistake, so the iteration
            # loop keeps going in that case regardless of what the agent
            # wrote.
            if isinstance(existing.get("ready_to_submit"), bool):
                ready_to_submit = bool(existing["ready_to_submit"]) and is_valid

    return {
        "iteration": iteration,
        "has_live_files": has_live_files,
        "is_valid": is_valid,
        "reason": reason,
        "metrics": (report or {}).get("model_metrics"),
        "summary_written_by_agent": summary_written_by_agent,
        "ready_to_submit": ready_to_submit,
        "evaluate_seconds": evaluate_seconds,
        "round_elapsed_seconds": round_elapsed_seconds,
        "round_budget_seconds": round_budget_seconds,
    }


def build_history_digest(workspace: Path, max_iterations_shown: int = 8) -> str:
    """Build a compact, human-readable digest of all prior iterations
    (summary.json + report.json highlights) to hand to the agent at the
    start of the next round, so it can decide whether to keep iterating or
    roll back to an earlier iteration's snapshot.

    Also includes a per-iteration TIMING line (how long that OpenCode round
    actually ran vs. its budget, plus how long the harness's own evaluate.py
    validity-check call took) and a trailing TIME PACING SUMMARY aggregating
    all iterations seen so far (all-iteration total, average round length,
    average evaluate.py cost) — this is the only place the agent can see its
    own historical pacing, since it has no visibility into other iterations'
    wall-clock behavior otherwise.
    """
    iterations = _existing_iterations(workspace)
    if not iterations:
        return "(no prior iterations yet — this is iteration 1)"

    shown = iterations[-max_iterations_shown:]
    omitted = len(iterations) - len(shown)
    lines = []
    if omitted > 0:
        lines.append(f"(showing the most recent {len(shown)} of {len(iterations)} "
                      f"prior iterations; earlier ones are still on disk under iterations/)")

    round_elapsed_all: list[float] = []
    evaluate_seconds_all: list[float] = []

    # Track (is_valid, reason) across ALL prior iterations (not just the
    # `shown` window) so we can detect "stuck" streaks — e.g. many
    # consecutive rounds all rejected for the exact same reason (most
    # commonly: insufficient candidate-model response coverage) — and force
    # a loud, impossible-to-miss warning in front of the agent, instead of
    # relying on it noticing a subtle pattern buried in per-iteration
    # metrics lines. This directly targets a failure mode observed in
    # practice: an agent can convince itself "nothing needs to change" for
    # hundreds of rounds in a row while every single one of those rounds was
    # actually being rejected by the harness for the same fixable reason.
    all_validity: list[tuple[int, bool, str | None]] = []
    for n in iterations:
        s_path = _iter_dir(workspace, n) / "summary.json"
        if not s_path.exists():
            continue
        try:
            s = json.loads(s_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        v = s.get("validity") if isinstance(s.get("validity"), dict) else {}
        all_validity.append((n, bool(v.get("is_valid")), v.get("reason")))

    for n in shown:
        iter_dir = _iter_dir(workspace, n)
        summary_path = iter_dir / "summary.json"
        summary: dict[str, Any] = {}
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                summary = {}
        metrics = summary.get("final_metrics_this_iter_official") or summary.get("final_metrics_this_iter")
        metrics_line = "no metrics recorded"
        if isinstance(metrics, dict):
            def _fmt(key):
                v = metrics.get(key)
                return f"{v:.4f}" if isinstance(v, (int, float)) else "null"
            metrics_line = (
                f"spearman={_fmt('spearman')} pairwise_acc={_fmt('pairwise_accuracy')} "
                f"kendall_tau_b={_fmt('kendall_tau_b')} bias={_fmt('bias')} "
                f"spread_ratio={_fmt('spread_ratio')} mae={_fmt('mae')} "
                f"top1_match={metrics.get('top1_match')}"
            )

        validity = summary.get("validity") if isinstance(summary.get("validity"), dict) else {}
        is_valid = validity.get("is_valid")
        reason = validity.get("reason")
        if is_valid is None:
            validity_line = "not recorded"
        elif is_valid:
            validity_line = "VALID — this snapshot would be a fully acceptable final submission"
        else:
            validity_line = f"*** REJECTED by the harness *** reason: {reason}"

        timing = summary.get("timing") if isinstance(summary.get("timing"), dict) else {}
        round_elapsed = timing.get("round_elapsed_seconds")
        round_budget = timing.get("round_budget_seconds")
        eval_secs = timing.get("harness_evaluate_seconds")
        if isinstance(round_elapsed, (int, float)):
            round_elapsed_all.append(round_elapsed)
        if isinstance(eval_secs, (int, float)):
            evaluate_seconds_all.append(eval_secs)
        if isinstance(round_elapsed, (int, float)) and isinstance(round_budget, (int, float)) and round_budget:
            pct = round_elapsed / round_budget * 100
            timing_line = (
                f"used {round_elapsed/60:.1f}min of its {round_budget/60:.1f}min budget "
                f"({pct:.0f}%)"
                + (" — RAN OVER / got killed by timeout" if round_elapsed >= round_budget else "")
            )
        else:
            timing_line = "not recorded"
        if isinstance(eval_secs, (int, float)):
            timing_line += f"; harness's own evaluate.py validity-check took {eval_secs:.1f}s"

        lines.append(
            f"--- iter_{n} ---\n"
            f"  what_changed: {summary.get('what_changed', '(missing)')}\n"
            f"  why: {summary.get('why', '(missing)')}\n"
            f"  compared_to_previous: {summary.get('compared_to_previous', '(missing)')}\n"
            f"  intermediate_validation: {summary.get('intermediate_validation', '(missing)')}\n"
            f"  rolled_back_to: {summary.get('rolled_back_to')}\n"
            f"  metrics: {metrics_line}\n"
            f"  harness verdict: {validity_line}\n"
            f"  timing: {timing_line}\n"
            f"  files available at: iterations/iter_{n}/(verify.py, responses.json, report.json, summary.json)"
        )

    if round_elapsed_all:
        avg_round_min = (sum(round_elapsed_all) / len(round_elapsed_all)) / 60
        total_round_min = sum(round_elapsed_all) / 60
        pacing_line = (
            f"\n--- TIME PACING SUMMARY across {len(round_elapsed_all)} recorded iteration(s) ---\n"
            f"  average time actually spent per iteration: {avg_round_min:.1f} min\n"
            f"  total time spent across these iterations: {total_round_min:.1f} min"
        )
        if evaluate_seconds_all:
            avg_eval = sum(evaluate_seconds_all) / len(evaluate_seconds_all)
            pacing_line += (
                f"\n  average harness evaluate.py validity-check cost: {avg_eval:.1f}s per iteration "
                "(re-running evaluate.py many times yourself mid-iteration adds up — budget for it)"
            )
        lines.append(pacing_line)

    # Detect a "stuck streak": count how many of the MOST RECENT iterations
    # (across the full history, not just the `shown` window) were all
    # invalid for the exact same reason, with no successful valid round in
    # between. Once this streak reaches a threshold, inject a loud warning
    # the agent cannot plausibly miss or rationalize away as "already
    # optimal" — because "already optimal" is contradicted by the harness
    # rejecting every single one of those rounds.
    streak_len = 0
    streak_reason: str | None = None
    for n, is_valid, reason in reversed(all_validity):
        if is_valid:
            break
        if streak_reason is None:
            streak_reason = reason
            streak_len = 1
        elif reason == streak_reason:
            streak_len += 1
        else:
            break
    if streak_len >= 4 and streak_reason:
        lines.append(
            f"\n*** STUCK-STREAK WARNING: the last {streak_len} consecutive iterations were ALL "
            f"rejected by the harness for the SAME reason: \"{streak_reason}\" ***\n"
            "If you were about to write \"no changes, already optimal\" or similar in this "
            "round's summary.json — DON'T. The harness's own verdict above proves the current "
            "live verify.py/responses.json is NOT an acceptable final submission and has not "
            "been for several rounds in a row. Whatever you tried before did not fix this "
            "specific problem, so repeating the same \"no changes\" round again will just burn "
            "through your remaining time budget for zero benefit. Concretely diagnose and fix "
            "the root cause named in the reason string above THIS round (e.g. if it names "
            "insufficient candidate-model response coverage, go find which task/model pairs in "
            "responses.json are missing or empty and actually call those models now — see the "
            "CRITICAL RULE: THE FINAL SCORE IS BASED ON ALL CANDIDATE MODELS section above for "
            "why this matters)."
        )

    return "\n".join(lines)


def write_iteration_log_line(workspace: Path, text: str) -> None:
    """Append a one-line, run.py-authored (not agent-authored) record to a
    top-level ITERATIONS_LOG.txt — a machine log of round timing/outcomes,
    separate from the agent's own per-iteration summary.json write-ups."""
    log_path = workspace / "ITERATIONS_LOG.txt"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(text.rstrip("\n") + "\n")


# ---------------------------------------------------------------------------
# OpenCode runner (multi-round session bounded by a total time budget)
# ---------------------------------------------------------------------------


def _run_opencode_once(workspace: Path, env: dict[str, str], opencode_model: str,
                        prompt: str, timeout_seconds: int,
                        continue_session: bool,
                        candidate_model_routes: dict[str, Any],
                        allowed_models: set[str] | None = None) -> tuple[bool, int]:
    """Run a single OpenCode subprocess round (fresh or --continue) bounded
    by `timeout_seconds`. Returns (timed_out, elapsed_seconds).

    `allowed_models`, when given, is forwarded to `_candidate_model_proxy`
    so that only those model names are reachable through
    CANDIDATE_MODEL_ENDPOINT during this round — this is how HELD-OUT
    models stay physically unreachable from the agent's session (see
    `_candidate_model_proxy`'s docstring).
    """
    command = [
        _opencode_bin(), "run", "--pure", "--auto", "--model", opencode_model,
        "--format", "json", "--dir", str(workspace),
    ]
    if continue_session:
        command.append("--continue")
    command.append(prompt)

    start = time.monotonic()
    timed_out = False
    completed = None
    with _stream_proxy(env["OPENCODE_ANTHROPIC_BASE_URL"]) as proxy_url, \
            _candidate_model_proxy(candidate_model_routes, allowed_models) as candidate_proxy_url:
        round_env = dict(env)
        round_env["OPENCODE_ANTHROPIC_BASE_URL"] = proxy_url
        # Exposed to any code the agent runs inside the workspace (OpenCode
        # subprocesses inherit this env) — the ONE thing it needs to call
        # any candidate/judge model: POST {"model": name, "messages": [...]}
        # here, Anthropic-Messages-compatible. No appid/credential involved.
        round_env["CANDIDATE_MODEL_ENDPOINT"] = f"{candidate_proxy_url}/v1/messages"
        try:
            completed = subprocess.run(
                command, cwd=workspace, env=round_env, text=True, capture_output=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = True

            def _to_text(value: str | bytes | None) -> str:
                if value is None:
                    return ""
                if isinstance(value, bytes):
                    return value.decode("utf-8", errors="replace")
                return value

            (workspace / "opencode_stdout.jsonl").write_text(_to_text(exc.stdout), encoding="utf-8")
            (workspace / "opencode_stderr.txt").write_text(_to_text(exc.stderr), encoding="utf-8")

    elapsed = int(time.monotonic() - start)
    if timed_out:
        return timed_out, elapsed

    (workspace / "opencode_stdout.jsonl").write_text(completed.stdout, encoding="utf-8")
    (workspace / "opencode_stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode:
        detail = completed.stderr[-2000:] or completed.stdout[-2000:]
        raise RuntimeError(detail or f"OpenCode exited {completed.returncode}")
    return timed_out, elapsed


def _build_iteration_prompt(
    iteration: int, workspace: Path, num_tasks: int, time_budget_minutes: int,
    remaining_minutes: int,
    candidate_models: list[str],
) -> str:
    """Build the prompt for one structured ITERATION round (design/refine
    tasks and verify.py). Iteration 1 gets the full task explanation;
    iteration 2+ get a shorter reminder plus the mandatory "read your
    history first" instruction, since the full explanation is already in
    the (continued) conversation history.

    This is only ever used for regular iteration rounds. The separate final
    FINALIZE round (see `_build_finalize_prompt`) has its own dedicated
    prompt instead of a branch inside this one, so the two concerns (keep
    improving vs. wrap up and submit) don't get tangled together.

    IMPORTANT: there is deliberately NO per-iteration time limit told to the
    agent here — run.py does not cut this round off on any fixed clock. The
    agent decides for itself, every round, how much to attempt before
    ending its turn (typically right after running evaluate.py and writing
    summary.json). The only number given is the shared TOTAL remaining
    budget plus the agent's own historical pacing (via the TIME PACING
    SUMMARY inside `history_digest`), so it can judge for itself whether to
    go for a quick/cheap round or a longer, more ambitious one.
    """
    candidate_models_list = "\n".join(f"  - {m}" for m in candidate_models)
    history_digest = build_history_digest(workspace)

    elapsed_minutes_so_far = max(0, time_budget_minutes - remaining_minutes)
    prior_iterations_count = max(0, iteration - 1)
    avg_minutes_per_prior_iteration = (
        elapsed_minutes_so_far / prior_iterations_count if prior_iterations_count else None
    )
    if avg_minutes_per_prior_iteration is not None:
        projected_remaining_iterations = (
            remaining_minutes / avg_minutes_per_prior_iteration
            if avg_minutes_per_prior_iteration > 0 else float("inf")
        )
        pacing_note = (
            f"So far: {prior_iterations_count} iteration(s) completed, "
            f"~{elapsed_minutes_so_far} min elapsed out of {time_budget_minutes} min total "
            f"(avg ~{avg_minutes_per_prior_iteration:.1f} min/iteration). At that pace, roughly "
            f"{projected_remaining_iterations:.1f} more iteration(s) fit in the "
            f"{remaining_minutes} min remaining if you keep a similar pace — but there is no "
            "fixed per-iteration limit, so this is only a planning aid: decide for yourself, "
            "based on this history, how much to attempt THIS round before wrapping up (don't "
            "start something that needs 3 more iterations' worth of work if only ~1 typical "
            "iteration's worth of time is left; conversely, if past iterations finished quickly "
            "with time to spare, feel free to be more ambitious or just keep going longer this "
            "round). See the TIME PACING SUMMARY inside the history digest below for the full "
            "breakdown.\n\n"
        )
    else:
        pacing_note = (
            "This is the first iteration, so there is no pacing history yet, and there is no "
            f"fixed per-iteration time limit — you decide when to wrap up this round. Just keep "
            f"in mind the overall budget is {remaining_minutes} min total across ALL remaining "
            "iterations combined; leave yourself enough of it for later refinement rounds rather "
            "than spending everything up front on iteration 1.\n\n"
        )

    common_header = (
        f"=== ITERATION {iteration} — structured multi-iteration benchmark research session ===\n\n"
        f"Total session time budget: {time_budget_minutes} minutes. Approximately "
        f"{remaining_minutes} minutes remain in TOTAL across all remaining iterations combined. "
        "There is NO fixed time limit for this individual iteration — you decide when this "
        "round is done (normally right after you finish running evaluate.py and writing "
        "summary.json for this round). The only hard constraint is the TOTAL remaining budget "
        "above: if the current round runs long enough to exhaust essentially the entire "
        "remaining total budget, run.py WILL forcibly kill the process wherever it is (as a "
        "backstop, not a target to aim for) and immediately move to the mandatory finalize "
        "step — so don't treat \"no per-iteration limit\" as license to run one enormous "
        "iteration forever; budget your own time across the iterations you still want to run.\n\n"
        f"{pacing_note}"
        "STEP 0 — READ YOUR HISTORY FIRST (mandatory, do this before changing anything):\n"
        "Read every prior iteration's summary.json and report.json under "
        "iterations/iter_*/ (also readable via ITERATIONS_LOG.txt for a quick machine-"
        "generated timeline). Here is a digest of what's recorded so far, including how much "
        "wall-clock time each prior iteration actually used (see each iter's \"timing\" line "
        "and the trailing TIME PACING SUMMARY) — use this to judge whether you're pacing "
        "yourself well or need to work faster/scope down:\n"
        f"{history_digest}\n\n"
        "Based on that history, decide ONE of:\n"
        "  (a) CONTINUE iterating on the current live verify.py / responses.json "
        "(the files directly in the workspace root, NOT inside iterations/) if the "
        "trend across iterations looks like it's improving or you have a concrete new "
        "idea to try, or\n"
        "  (b) ROLL BACK if the most recent iteration(s) made things worse (e.g. spearman "
        "dropped, spread_ratio collapsed, bias grew) — copy an earlier, better iteration's "
        "files back into the live copies, e.g.:\n"
        "        cp iterations/iter_K/verify.py verify.py\n"
        "        cp iterations/iter_K/responses.json responses.json\n"
        "      and continue improving from that known-good state instead of from the "
        "regressed one. If you roll back, say so explicitly in this iteration's "
        "summary.json (rolled_back_to: \"iter_K\").\n\n"
        "*** CRITICAL RULE ABOUT THE FINAL SCORE (applies EVERY iteration, not just the "
        "last one) ***\n"
        "When your total time budget reaches zero, the harness evaluates ONLY whatever "
        "verify.py + responses.json are sitting directly in the workspace ROOT at that "
        "exact moment (NOT the best iter_K/ snapshot, NOT whichever one \"looks best\" in "
        "hindsight — it does not search iterations/ at all). There is no second chance and "
        "no post-hoc smart fallback once time is up. This means the live verify.py + "
        "responses.json must ALWAYS be left as the best version you have — the one you'd "
        "want graded — at the end of every single iteration, not just the last one, "
        "because you never know for certain which iteration will be the last (a round can "
        "be cut off if it runs long enough to exhaust the entire remaining total budget, or "
        "the total budget can simply run out between rounds). Concretely: before ending ANY "
        "iteration, if the current live files are worse than some earlier iter_K/, copy "
        "iter_K's files back into the workspace root NOW rather than leaving the regression "
        "in place \"to fix next round.\"\n\n"
        "*** CRITICAL RULE: THE FINAL SCORE IS BASED ON ALL CANDIDATE MODELS ***\n"
        "The final evaluation looks at EVERY candidate model's performance on your benchmark, "
        "not just some of them. This means responses.json must have a real, non-empty response "
        "from EVERY candidate model for (ideally) EVERY task — a missing or empty response for "
        "a given model on a given task is scored as a wrong/zero-credit answer for that model on "
        "that task, which drags down that model's predicted score and can distort the whole "
        "ranking even if everything else about your benchmark is good. If you notice (e.g. from "
        "the harness verdict in the history digest below, or from your own inspection of "
        "responses.json) that some candidate model is missing responses on some tasks, fixing "
        "that is more valuable than almost anything else you could spend this iteration on — go "
        "call that model for those specific tasks now rather than leaving it for \"later.\"\n\n"
        "*** CRITICAL RULE: EVERY ITERATION EXISTS TO IMPROVE THE METRICS, NOT JUST TO STAY "
        "BUSY ***\n"
        "From iteration 2 onward especially, judge your own success this round strictly by "
        "whether report.json's model_metrics (spearman, kendall_tau_b, pairwise_accuracy, "
        "spread_ratio, bias) actually moved in the right direction compared to the previous "
        "iteration — not by how much you edited, how many tasks you added, or how busy the "
        "round felt. Making changes that don't demonstrably improve at least one of these "
        "numbers (while not badly regressing the others) accomplishes nothing: it is "
        "indistinguishable from doing nothing, except it burns budget you needed for later "
        "improvement. Always diagnose first (see PART A/PART B structure below, iteration "
        "2+), always re-run evaluate.py to confirm the fix actually worked, and always leave "
        "the workspace root at the version with the best metrics you have found so far — "
        "never end an iteration having quietly made things worse or having merely churned "
        "files without a measurable metric gain to show for it.\n\n"
    )

    if iteration == 1:
        body = (
            "You are a benchmark researcher working inside this workspace. Your job is to "
            f"build a brand-new, from-scratch benchmark of {num_tasks} tasks that can rank "
            "a set of candidate LLMs in the SAME order as their real SWE-Bench Verified "
            "performance — WITHOUT reusing any SWE-Bench task or looking at truth.json's "
            "per-task answers (there are none; it only contains model-level pass rates).\n\n"
            "AVAILABLE FILES (in this workspace):\n"
            "- truth.json: {model_name: real_swebench_verified_pass_rate} for every candidate "
            "model VISIBLE to you (see CANDIDATE MODELS list below — this is a strict subset "
            "of all the models that will ultimately be scored; see the HELD-OUT MODELS note "
            "further down). This is the ONLY ground truth you get for the models you can see — "
            "a model-level aggregate, no per-task labels. Use it to check how well your "
            "ranking matches on visible models, not to reverse-engineer answers.\n"
            "- verify.py: the scoring function you may edit freely. Called once per "
            "(task, model) pair as verify(response, answer, task) -> float in [0, 1]. This "
            "is your LIVE working copy — it is what gets snapshotted at the end of every "
            "iteration.\n"
            "- evaluate.py: fixed evaluation script (DO NOT EDIT). Computes ranking-agreement "
            "metrics between your verify() scores and truth.json.\n"
            "- Calling a candidate/judge model: the environment variable "
            "CANDIDATE_MODEL_ENDPOINT (already set for you) is an HTTP endpoint that "
            "accepts Anthropic-Messages-style POST requests: "
            "{\"model\": \"<model_name>\", \"max_tokens\": ..., \"messages\": "
            "[{\"role\": \"user\", \"content\": \"...\"}]}. Pass model_name exactly as it "
            "appears in truth.json (or any other model you want as a judge) — no API key, "
            "appid, or other credential is needed; it's handled for you. The response is a "
            "normal Anthropic Messages JSON object; read response[\"content\"] for the "
            "text.\n"
            "- responses.json: YOU must create this file yourself (see format below). Also "
            "a LIVE working copy, snapshotted every iteration.\n"
            "- reference_trajectories/<model_name>/*.jsonl: REAL SWE-Bench Verified "
            "trajectories (the actual heavy benchmark you're building a lightweight "
            "substitute for) for every VISIBLE candidate model — one subdirectory per "
            "model, each line a JSON record with the real issue prompt, that model's full "
            "agentic trajectory (reasoning + tool calls) on that task, and the "
            "ground-truth correct/incorrect verdict. Read as much or as little of this as "
            "you find useful (these files are large, tens of MB each — write a script to "
            "sample/grep/parse rather than trying to load one whole-hog into context) to "
            "calibrate what real SWE-Bench-style tasks/difficulty/failure-modes look like, "
            "and concretely where weaker vs. stronger models diverge — this should inform "
            "what your OWN brand-new tasks probe and what verify.py actually checks for. "
            "See reference_trajectories/README.md. Do NOT copy these prompts (or lightly "
            "reworded versions) into your own responses.json — your tasks must still be "
            "brand-new and self-designed, not reused SWE-Bench tasks. This directory is "
            "read-only reference material, not a live/graded file, and it does NOT "
            "necessarily include every model that will ultimately be scored (see HELD-OUT "
            "MODELS below).\n"
            "- iterations/iter_N/: read-only snapshots of your live files + that round's "
            "report.json + summary.json, one directory per completed iteration. Created "
            "automatically by run.py at the end of each iteration — you don't create these "
            "directories yourself except by writing summary.json into the CURRENT one (see "
            "STEP LAST below).\n"
            "- tmp_operations/: the ONLY place for scratch work (see WORKSPACE ROOT MUST "
            "STAY CLEAN below). Create this directory yourself the first time you need it.\n\n"
            "*** WORKSPACE ROOT MUST STAY CLEAN — ALL SCRATCH FILES GO IN tmp_operations/ "
            "***\n"
            "The workspace root must contain ONLY: truth.json, evaluate.py, verify.py, "
            "responses.json, report.json, the iterations/ directory, and the "
            "reference_trajectories/ directory (read-only, pre-populated for you — see "
            "AVAILABLE FILES above; leave it exactly as-is) — i.e. exactly what the final "
            "official evaluation reads plus the versioned history plus that reference "
            "material. You will naturally want to write one-off helper scripts as you work "
            "(a script that calls the candidate models and writes responses.json, a script "
            "that runs an LLM-as-judge pass, a script that retries empty responses, draft "
            "tasks.json/harder_tasks.json files before you fold them into responses.json, "
            "etc.) — ALL of that goes into a tmp_operations/ directory (e.g. "
            "tmp_operations/collect_responses.py, tmp_operations/judge_v2.py, "
            "tmp_operations/draft_tasks.json), never directly in the workspace root. Only "
            "the FOUR live files above (verify.py, responses.json, truth.json, evaluate.py), "
            "the iterations/ directory, and reference_trajectories/ should ever sit at the "
            "workspace root — nothing "
            "else. Before ending every iteration, check `ls` at the workspace root and move/"
            "delete anything that doesn't belong (stray .py helper scripts, draft .json "
            "files, __pycache__/, etc.) into tmp_operations/ instead of leaving it at the "
            "root. This keeps the workspace root exactly as clean as what gets graded, and "
            "keeps your own scratch history organized and out of the way of the real "
            "artifacts.\n\n"
            f"CANDIDATE MODELS — call ALL of these for every task, every iteration:\n"
            f"{candidate_models_list}\n\n"
            "*** IF A MODEL TIMES OUT OR RETURNS EMPTY: RETRY IT, DO NOT SIMPLIFY THE TASK "
            "***\n"
            "Some candidate models may be slower or less reliable than others and "
            "occasionally time out or return an empty/truncated response. When that happens, "
            "the fix is to retry that specific (task, model) call (with backoff, a longer "
            "timeout, or a few attempts) until you get a real response — NOT to shorten, "
            "simplify, or dumb down the task itself so that the flaky model is less likely to "
            "time out on it. Designing tasks around one particular model's API reliability "
            "(e.g. making prompts \"ultra-short\" so a slow model responds faster) biases your "
            "whole benchmark's difficulty/style to accommodate one model's infrastructure "
            "quirks rather than to genuinely discriminate model quality, and it silently "
            "changes what your benchmark is actually measuring. Task difficulty and design "
            "should be decided ONLY by what best discriminates real model quality (see the "
            "diversity guidance below) — never by which tasks happen to be \"safe\" for your "
            "least reliable model's API. If a model still fails after reasonable retries, "
            "leave the resulting gap in responses.json and go fix it with more retries in a "
            "later iteration — don't route around it by changing the tasks.\n\n"
            "STEP-BY-STEP WORKFLOW (within an iteration):\n"
            "0. (Optional but recommended, especially on iteration 1) Skim a sample of "
            "   reference_trajectories/<model_name>/*.jsonl (see AVAILABLE FILES above) to "
            "   get a feel for real SWE-Bench issue style/difficulty and concretely where "
            "   weaker vs. stronger models diverge — use this to inform step 1, not to copy "
            "   from.\n"
            "1. Design your own set of tasks (prompts) — NOT copied from SWE-Bench. They can "
            "   be coding, reasoning, debugging, or any task type you think discriminates "
            f"   model quality well. Aim for {num_tasks} tasks.\n"
            "   MAXIMIZE DIVERSITY, AVOID DUPLICATES/NEAR-DUPLICATES: a set of near-identical "
            "   tasks (same bug pattern, same algorithm reworded, same boilerplate CRUD "
            "   exercise with different variable names, etc.) wastes your task budget — "
            "   every model tends to either pass or fail a whole cluster of similar tasks "
            "   together, which adds noise/variance to the ranking without adding "
            "   discriminative signal. Instead, deliberately spread tasks across MULTIPLE "
            "   axes of variation, e.g.:\n"
            "     - task type: bug-fixing, greenfield implementation, refactoring, code "
            "       review/critique, algorithm design, debugging from a stack trace, API "
            "       design, performance optimization, writing tests, explaining/reasoning "
            "       about code, edge-case handling, multi-step agentic tool-use style "
            "       tasks, etc.\n"
            "     - domain/language: don't make everything Python string manipulation — mix "
            "       in different languages, libraries, problem domains (web backend, data "
            "       processing, systems/concurrency, CLI tools, math/algorithms, etc.).\n"
            "     - difficulty: include a spread from easy (should separate weak models "
            "       from the rest) to hard (should separate strong models from each "
            "       other) — an all-easy or all-hard set compresses the ranking (hurts "
            "       spread_ratio) because most/all models will score the same.\n"
            "     - failure mode being probed: some tasks should stress correctness, some "
            "       code quality/style, some instruction-following, some reasoning depth — "
            "       don't let every task reward the exact same skill.\n"
            "   Before finalizing your task list, skim your own prompts for near-duplicates "
            "   (two tasks that are really the same exercise with cosmetic differences) and "
            "   swap one of them out for something structurally different.\n"
            "2. For every task, call each of the candidate models via the "
            "   CANDIDATE_MODEL_ENDPOINT HTTP endpoint (see AVAILABLE FILES above) and "
            "   record its raw response.\n"
            "3. Write all of this to responses.json as a JSON list of task records:\n"
            "   [\n"
            "     {\n"
            "       \"task_id\": \"task_001\",\n"
            "       \"prompt\": \"...the task you wrote...\",\n"
            "       \"reference_answer\": \"...your own reference/rubric (optional)...\",\n"
            "       \"model_responses\": {\n"
            "         \"<model_name>\": \"<raw text response from that model>\",\n"
            "         ... one entry per candidate model ...\n"
            "       }\n"
            "     },\n"
            "     ... one object per task ...\n"
            "   ]\n"
            "4. Edit verify.py to implement a scoring strategy for your tasks (exact match, "
            "   rubric, LLM-as-judge, programmatic check, etc.).\n"
            "5. Run:\n"
            "   python evaluate.py --responses responses.json --verify verify.py "
            "--truth truth.json --output report.json\n"
            "   Inspect report.json's model_metrics (pairwise_accuracy, spearman, "
            "   kendall_tau_b, pearson, mae, top1_match, plus the MAE-derived diagnostics "
            "   bias, mae_centered, mae_zscore, pred_std, actual_std, spread_ratio) to see "
            "   how well your benchmark's ranking matches the real SWE-Bench ordering.\n"
            "   WHAT TO OPTIMIZE FOR — ranking agreement AND not straying too far from the "
            "   real scale, not just a small raw mae:\n"
            "     - RANKING (most important): maximize pairwise_accuracy, spearman, and "
            "       kendall_tau_b (all closer to 1.0 is better), and aim for top1_match=true. "
            "       A benchmark whose scores move in the same order as truth.json across "
            "       models is far more valuable than one that merely has a small average "
            "       error.\n"
            "     - DON'T STRAY TOO FAR FROM THE REAL SCALE: keep bias (mean predicted - "
            "       mean actual) small — your average predicted score per model shouldn't "
            "       be systematically much higher/lower than its real SWE-Bench pass rate.\n"
            "     - DON'T LET SCORES COLLAPSE TOGETHER: watch spread_ratio (pred_std / "
            "       actual_std). If it's much less than 1.0, your verify() scores are too "
            "       bunched up across models (e.g. everyone gets ~0.9) and can't actually "
            "       discriminate model quality even if pairwise_accuracy looks OK by luck — "
            "       push scores to spread out more in proportion to real quality "
            "       differences. mae_centered / mae_zscore isolate this shape/spread error "
            "       after removing any constant bias, so use them (not just raw mae) to "
            "       judge whether your scoring is genuinely discriminative.\n"
            "6. Iterate WITHIN this round as much as time allows: adjust your tasks and/or "
            "   verify.py to improve the ranking agreement among the candidate models — "
            "   improve spearman/kendall_tau_b/pairwise_accuracy while keeping bias small "
            "   and spread_ratio close to 1.0. Repeat evaluate.py as many times as you "
            "   like.\n\n"
            "*** THERE ARE ALSO HELD-OUT MODELS YOU CANNOT SEE OR CALL ***\n"
            f"The {len(candidate_models)} candidate models listed above are not the complete "
            "picture: the TRUE final evaluation (run once, by the harness, after your entire "
            "session ends) additionally scores your final verify.py + task prompts against a "
            "small set of HELD-OUT models that are NOT in truth.json, NOT reachable through "
            "CANDIDATE_MODEL_ENDPOINT (calling one by name will simply fail — there is no "
            "route to it from inside this workspace/session, no matter how you ask), and "
            "whose real SWE-Bench pass rates you never see at any point. After your session "
            "ends, the harness calls those held-out models directly — outside this workspace, "
            "on infrastructure you have no access to — using the EXACT SAME task prompts from "
            "your final responses.json, scores them with your final verify.py, and reports "
            "both a visible-models-only breakdown and a held-out-only breakdown side by side.\n"
            "Practically, this means: feel free to use evaluate.py's feedback against "
            "truth.json as much as you want while iterating — tune weights, calibrate "
            "verify.py, adjust tasks, whatever gets the best ranking agreement on the models "
            "you can see. There is no artificial rule against that. But any tuning that only "
            "works because it exploits quirks specific to these particular 7 models (rather "
            "than building a genuinely general-purpose way of judging response quality) will "
            "simply score worse on the 3 models you can't see and don't know about — that's "
            "the real, unavoidable check on overfitting here, not an instruction you have to "
            "remember to follow. The best strategy is exactly what it would be if there were "
            "no held-out models at all: build verify.py and tasks around genuine, "
            "general-purpose signals of response quality (correctness, reasoning depth, "
            "instruction-following, etc.) that would work for ANY competent model's output, "
            "not tricks that only happen to line up with these 7 specific models' known "
            "quirks.\n\n"
            "*** DO NOT SPEND YOUR ENTIRE BUDGET ON ITERATION 1 ***\n"
            "A first pass that reaches SOME working verify.py + responses.json quickly is "
            "far more valuable than a \"perfect\" first attempt that consumes almost the "
            "whole session — this benchmark is built through many rounds of measure-"
            "diagnose-fix (see the diagnosis requirement for iteration 2+ below), not in one "
            "monolithic pass. Get a complete, working, evaluate.py-passing version of "
            "verify.py + responses.json done reasonably quickly (even if the metrics are "
            "mediocre at first), run evaluate.py, write iteration 1's summary.json, and end "
            "the round — leave the bulk of the total time budget for the diagnose-and-"
            "improve iterations that follow, which is where most of the real gains in "
            "spearman/pairwise_accuracy/spread_ratio actually come from.\n\n"
        )
    else:
        body = (
            "You already know the full task from earlier in this conversation (design a "
            f"from-scratch, {num_tasks}-task benchmark, collect real model responses, write "
            "verify.py, and check ranking agreement against truth.json via evaluate.py). "
            "This message starts a NEW iteration on top of your existing live "
            "verify.py/responses.json — don't restart from scratch unless STEP 0 above told "
            "you to roll back.\n\n"
            "*** THE POINT OF THIS ROUND IS TO ACTUALLY MOVE THE METRICS, NOT JUST TO EDIT "
            "FILES ***\n"
            "\"Did something this round\" is not the goal — \"the numbers got measurably "
            "better than last iteration\" is the goal. Before you end this iteration, the "
            "most recent report.json's model_metrics MUST show real movement in the right "
            "direction relative to the previous iteration on at least one of: spearman, "
            "kendall_tau_b, pairwise_accuracy (all should trend toward 1.0), spread_ratio "
            "(should trend toward 1.0, not collapse toward 0), or |bias| (should trend "
            "toward 0) — without any of the others regressing badly in exchange. If you try "
            "a change and re-running evaluate.py shows it did NOT help, do not shrug and "
            "move on: try a different, more targeted fix based on the diagnosis (Part A "
            "below) within this same iteration if time allows, or explicitly roll back to "
            "the previous iteration's files so you don't end the round with a regression "
            "live in the workspace root. An iteration that ends with identical or worse "
            "metrics than it started with (and no honest explanation of what was tried and "
            "why it didn't work) is a wasted iteration — every single round, including the "
            "last one before finalize, is expected to leave the benchmark strictly better "
            "than it found it, not just \"different.\"\n\n"
            "Quick reminders:\n"
            "- Call ALL candidate models for every task, every iteration:\n"
            f"{candidate_models_list}\n"
            "- Do not edit evaluate.py. Do not access anything outside this workspace.\n"
            "- Remember: there are also HELD-OUT models (not in truth.json, not callable "
            "through CANDIDATE_MODEL_ENDPOINT) that the harness will score your FINAL "
            "verify.py + task prompts against after your whole session ends (see iteration "
            "1's full explanation). Tune against truth.json/evaluate.py as freely as you "
            "want — there's no rule against it — but keep in mind that tuning which only "
            "works by exploiting quirks specific to these 7 visible models won't transfer "
            "to the 3 you can't see, so prefer changes justified by genuine response-quality "
            "reasoning over ones that only look good by matching truth.json's numbers.\n"
            "- If a candidate model timed out or returned empty on some tasks, retry that "
            "model on those specific tasks — do not simplify/shorten tasks to accommodate a "
            "flaky model's API.\n"
            "- Keep the task SET diverse: before adding any new task, check it isn't a "
            "near-duplicate of one already in responses.json (same bug pattern/algorithm/"
            "exercise reworded with different names). If you're replacing weak tasks, "
            "replace them with a genuinely different task type/domain/difficulty, not just "
            "another variant of the same idea — review the spread across task type, domain, "
            "difficulty, and skill-probed (see iteration 1's diversity guidance) and fill in "
            "whichever axis is under-represented.\n"
            "- WORKSPACE ROOT MUST STAY CLEAN: any scratch/helper script or draft JSON file "
            "you write (collection scripts, judge scripts, retry scripts, draft task lists, "
            "etc.) goes in tmp_operations/, never directly in the workspace root. Before "
            "ending this iteration, check `ls` at the workspace root and move anything that "
            "doesn't belong (stray .py files, draft .json files, __pycache__/, etc.) into "
            "tmp_operations/ — the root should only ever contain truth.json, evaluate.py, "
            "verify.py, responses.json, report.json, iterations/, and reference_trajectories/ "
            "(read-only, leave it as-is).\n\n"
            "WHAT TO DO THIS ITERATION (mandatory two-part structure: DIAGNOSE, then FIX):\n"
            "This iteration is NOT \"make some changes and see what happens\" — it is a "
            "targeted measure-diagnose-fix cycle. Do these two parts, in order:\n\n"
            "  PART A — DIAGNOSE (do this FIRST, before changing anything):\n"
            "  Look at the most recent report.json / summary.json (from your STEP 0 history "
            "review) and write down, concretely, what is actually wrong with the CURRENT "
            "metrics. Go through this checklist and identify which failure mode(s) apply "
            "right now — don't just eyeball spearman and move on:\n"
            "    - LOW SPREAD (spread_ratio << 1.0): are model scores bunched together "
            "(e.g. most/all models scoring 0.85-0.95)? If so, which tasks is EVERY model "
            "acing or failing together? Those tasks contribute zero discriminative signal — "
            "identify them by task_id, not just in the abstract.\n"
            "    - BIAS (|bias| large): are your verify() scores systematically too "
            "generous or too harsh compared to truth.json's real pass rates, across the "
            "board?\n"
            "    - RANKING ERRORS (spearman/pairwise_accuracy not close to 1.0): which "
            "SPECIFIC pair(s) of models are ranked in the wrong relative order by your "
            "benchmark vs. truth.json? Look at report.json's per-model predicted scores "
            "side-by-side with truth.json and name the actual model pairs that are "
            "inverted.\n"
            "    - COVERAGE GAPS: is any candidate model missing/empty responses on any "
            "tasks (dragging its score down for reasons unrelated to task design)?\n"
            "    - TASK-LEVEL DIAGNOSIS: for a handful of the worst-behaving tasks (ones "
            "that don't discriminate, or where verify.py's scoring looks unreliable), "
            "actually read the model responses and your verify.py logic for that task and "
            "figure out WHY — e.g. task too easy for all models, task ambiguous so scoring "
            "is noisy, verify.py's rubric/regex/heuristic doesn't capture real quality "
            "differences, judge-model prompt is too lenient, etc.\n"
            "  Write this diagnosis down explicitly (you'll paste a summary of it into "
            "summary.json's \"diagnosis\" field in STEP LAST below) BEFORE writing any code "
            "changes — a vague diagnosis like \"scores could be better\" is not acceptable; "
            "name specific tasks, specific model pairs, or specific verify.py logic that is "
            "the root cause.\n\n"
            "  PART B — FIX (only after Part A is written down):\n"
            "  Make concrete changes that directly address what you just diagnosed — e.g.:\n"
            "    - low spread → replace the identified always-easy/always-hard tasks with "
            "harder/more discriminative ones, or rework verify.py so partial-credit scoring "
            "spreads out more instead of clustering near 1.0 or 0.0.\n"
            "    - bias → recalibrate verify.py's scoring scale/threshold so its average "
            "lines up better with real pass rates.\n"
            "    - a specific inverted model pair → find/add a task type where you'd expect "
            "those two models to actually differ, or fix a verify.py rubric that isn't "
            "sensitive to the quality gap between them.\n"
            "    - coverage gaps → retry the missing (task, model) calls now.\n"
            "    - unreliable task-level scoring → fix verify.py's logic for that task "
            "type, or replace an inherently ambiguous task with a more cleanly-checkable "
            "one.\n"
            "  Avoid changes you can't tie back to a diagnosed problem — random tweaking "
            "without a stated cause is how iterations end up flat or regressing. Re-run "
            "evaluate.py after your fix and confirm (quantitatively, in summary.json) "
            "whether the specific metric you targeted actually moved in the right "
            "direction; if it didn't, say so honestly rather than declaring success.\n\n"
        )

    final_step = (
        "STEP LAST (mandatory, before this iteration ends) — write your iteration "
        f"summary to iterations/iter_{iteration}/summary.json (create the directory if it "
        "doesn't exist yet) as a JSON object with these fields:\n"
        "   {\n"
        f"     \"iteration\": {iteration},\n"
        "     \"diagnosis\": \"...(iteration 2+) the CONCRETE problem(s) you identified in "
        "PART A before making any changes this round — name specific task_ids, specific "
        "model pairs that were mis-ranked, or specific verify.py logic at fault; "
        "'metrics could be better' is not an acceptable answer here...\",\n"
        "     \"what_changed\": \"...concrete description of what you changed this round "
        "(tasks added/removed, verify.py logic changes, etc.)...\",\n"
        "     \"why\": \"...why you made this change, what problem (from \\\"diagnosis\\\" "
        "above) it was meant to fix...\",\n"
        "     \"compared_to_previous\": \"...how this iteration's approach/results differ "
        "from the previous one...\",\n"
        "     \"intermediate_validation\": \"...what you observed when you ran evaluate.py "
        "mid-round while iterating (which attempts worked/didn't)...\",\n"
        "     \"did_it_work\": \"...(iteration 2+) did the metric(s) targeted in "
        "\\\"diagnosis\\\" actually improve after your fix? Compare this iteration's "
        "final_metrics_this_iter to the previous iteration's, explicitly, and say yes/no/"
        "partially — be honest if it didn't help or made things worse...\",\n"
        "     \"rolled_back_to\": null,  // or \"iter_K\" if you rolled back this round per "
        "STEP 0\n"
        "     \"ready_to_submit\": false,  // see EARLY SUBMIT note below\n"
        "     \"final_metrics_this_iter\": { ... paste this iteration's final report.json "
        "model_metrics here ... }\n"
        "   }\n"
        "run.py will automatically snapshot your current verify.py + responses.json into "
        f"iterations/iter_{iteration}/ and re-run the official evaluate.py against that "
        "snapshot right after you finish — so summary.json is the one thing ONLY you can "
        "provide; don't skip it. This is what makes it possible (for you in the next "
        "iteration, and for the human operator) to see why each iteration changed things "
        "and whether it should be kept or rolled back.\n\n"
        "*** EARLY SUBMIT (optional) ***\n"
        "You do NOT have to use up the entire time budget. If, after honestly reviewing "
        "the metric trend across iterations (not just this one), you believe the current "
        "live verify.py + responses.json already represent your best achievable submission "
        "and further iterations are very unlikely to meaningfully improve the ranking "
        "metrics (spearman/pairwise_accuracy/spread_ratio/bias) any further, set "
        "\"ready_to_submit\": true in this iteration's summary.json instead of false. "
        "run.py will then automatically: (1) skip straight past the remaining iteration "
        "rounds — it will NOT keep launching new ones just to burn the clock — (2) run an "
        "automatic coverage-repair pass that finds any (task, candidate model) pair still "
        "missing/empty in responses.json and retries those exact API calls for real "
        "(catching leftover gaps from earlier transient failures — rate limits, timeouts, "
        "5xx errors — that you may not have fully cleaned up), and (3) still give you one "
        "final dedicated wrap-up round afterward to review/fix verify.py and responses.json "
        "one last time before the real grading happens — so saying you're done does not "
        "skip that final safety net, it just reclaims the unused time budget instead of "
        "wasting it on non-improving rounds. Only set this to true if the harness's own "
        "validity verdict for this round (shown in the history digest above) says the "
        "current snapshot is VALID — if it says REJECTED for any reason (bad coverage, "
        "placeholder verify.py, etc.), \"ready_to_submit\": true will be ignored and "
        "iteration will continue regardless, so fix that first. Be honest here: claiming "
        "you're done while metrics are still trending upward across recent iterations "
        "wastes budget you could have used to actually improve; the default (and safe "
        "choice if unsure) is false.\n"
    )

    return common_header + body + final_step


def _build_finalize_prompt(
    workspace: Path, num_tasks: int, finalize_minutes: int,
) -> str:
    """Build the prompt for the single, dedicated FINALIZE round that runs
    once, right at the end of the total time budget, after the normal
    iteration loop has stopped launching new iterations.

    This is a separate, explicit phase (not just "the last regular
    iteration") specifically so the instruction is unambiguous: time is
    almost up, stop improving, and submit your answer. The agent is told
    plainly that this round will be killed (SIGKILL-equivalent hard
    subprocess timeout) after `finalize_minutes`, whether or not it has
    finished, and that whatever verify.py + responses.json are sitting in
    the workspace root at that instant is what gets graded — no exceptions,
    no do-overs.
    """
    history_digest = build_history_digest(workspace)
    return (
        "=== FINALIZE ROUND — this is your LAST turn, time is almost up ===\n\n"
        f"You have approximately {finalize_minutes} minutes left, and then this process "
        "will be KILLED automatically (a hard subprocess timeout, not a polite request) "
        "whether or not you are done. There will be NO further iteration after this and "
        "NO further chance to fix anything. Whatever verify.py + responses.json are "
        "sitting directly in the workspace ROOT at the moment you are killed (or at the "
        "moment you finish, if you finish first) is EXACTLY what the harness will grade — "
        "it does not look inside iterations/ at all, there is no automatic fallback, and "
        "no one will pick a better iteration on your behalf. Remember also that the harness "
        "will additionally run your final verify.py against a few HELD-OUT models you never "
        "saw during this session (see iteration 1's explanation) — so a verify.py/task set "
        "that reflects genuine, general-purpose response-quality judgment will hold up "
        "better than one narrowly tuned to only the visible models.\n\n"
        "Here is the full history of every iteration you completed, including each one's "
        "actual wall-clock time usage and a TIME PACING SUMMARY at the end — if past "
        "iterations show evaluate.py taking many seconds per run, budget for that now: do NOT "
        f"burn your {finalize_minutes}-minute finalize window re-running evaluate.py more than "
        "once or twice just to double-check; spend the time assembling/copying files instead:\n"
        f"{history_digest}\n\n"
        "YOUR ONLY JOB RIGHT NOW: make sure the workspace ROOT verify.py + responses.json "
        f"represent your best possible submission of {num_tasks} tasks, using ONE of these "
        "two approaches — pick whichever gets you to a complete, correct result fastest:\n\n"
        "  OPTION A — pick ONE best iteration wholesale: if one earlier iteration (or the "
        "current live state) is clearly your best complete attempt, copy that exact pair "
        "into the workspace root right now, e.g.:\n"
        "        cp iterations/iter_K/verify.py verify.py\n"
        "        cp iterations/iter_K/responses.json responses.json\n\n"
        "  OPTION B — assemble a hybrid responses.json AND a matching verify.py from "
        "multiple iterations: if different iterations produced different good tasks (e.g. "
        "iter_2 had 20 discriminative tasks, iter_4 had 30 different discriminative tasks, "
        f"but no single iteration alone reaches a complete, high-quality set of "
        f"{num_tasks}), hand-pick and merge the best task records out of several "
        f"iterations' responses.json files into ONE final responses.json in the workspace "
        f"root totaling {num_tasks} tasks. This is NOT just a responses.json merge — "
        "verify.py must be merged/rewritten too, as long as: (i) every task record you "
        "keep has the exact same schema (task_id, prompt, model_responses for every "
        "candidate model, etc.), (ii) task_ids stay unique after merging (rename if "
        "needed) and you drop any duplicate/near-duplicate tasks pulled in from different "
        "iterations (favor keeping the more discriminative variant and keeping the merged "
        "set spread across different task types/domains/difficulties rather than several "
        "near-identical tasks from different iterations), and (iii) the FINAL verify.py you "
        "leave in the workspace root correctly "
        "scores EVERY task type you merged in — if the iterations you pulled tasks from "
        "used different scoring logic (e.g. iter_2's verify.py only knows how to score its "
        "own task type, iter_4's verify.py only knows its own), you must combine their "
        "scoring logic into ONE verify.py (e.g. branch on task[\"task_id\"]/task type, or "
        "on fields you stored in each task's reference_answer) so it handles ALL merged "
        "task types, not just the ones from whichever iteration you copied verify.py from. "
        "Spot-check with evaluate.py if you have even a minute left — don't assemble a "
        "responses.json your own verify.py can't actually score.\n\n"
        "Do NOT spend time on further task design, further verify.py redesign, or further "
        "model calls unless verify.py is STILL the untouched placeholder (in which case "
        "fixing that, even with a simple heuristic, is your #1 and only priority — a "
        "cheap-but-real verify.py beats a perfect-but-fake one).\n\n"
        "Priority order for whatever time you have left:\n"
        "  1. verify.py in the workspace root must be real (non-placeholder) and must "
        "correctly score every task type present in the final responses.json.\n"
        f"  2. responses.json in the workspace root must have exactly {num_tasks} (or as "
        f"close as possible) complete task records, each with responses from every "
        f"candidate model.\n"
        "  3. If time allows, do ONE final `python evaluate.py --responses responses.json "
        "--verify verify.py --truth truth.json --output report.json` run against the "
        "workspace-root files (no --models flag) just to confirm it produces real, "
        "non-null metrics — but do not let this block you if you're nearly out of time.\n"
        "  4. If time allows, write iterations/iter_FINAL/summary.json noting which option "
        "(A/B) you used and, for option B, which iterations you pulled tasks from — but "
        "this is the LOWEST priority; a correct workspace-root submission with no summary "
        "beats a summary with a broken submission.\n"
        "  5. ONLY once 1-2 are done and if you have a spare moment: the workspace root "
        "should contain just truth.json, evaluate.py, verify.py, responses.json, "
        "report.json, and iterations/ — if any stray scratch scripts/draft JSON files are "
        "still sitting in the root from earlier iterations, move them into "
        "tmp_operations/ (create it if needed). This is cosmetic cleanup ONLY — never let "
        "it delay or risk items 1-2 above.\n\n"
        "A complete-but-simple final submission beats a sophisticated but incomplete or "
        "broken one. Stop reading and start finalizing now."
    )


_MIN_ITERATION_SECONDS = 60  # not worth launching another iteration round for less than this

# If the agent produces this many consecutive iterations in a row with no
# real improvement in the composite metric score (see `_composite_score`),
# stop the iteration loop early and move straight to the finalize round
# instead of burning the rest of the total time budget on rounds that are
# demonstrably not helping. Set to 0 (or a negative number) to disable this
# early-stop behavior entirely and always run until the time budget itself
# runs out.
_DEFAULT_STAGNATION_PATIENCE = 3

# An improvement smaller than this (in composite score units, see
# `_composite_score`) is treated as noise/no-op rather than real progress,
# so tiny floating-point wiggles don't reset the stagnation counter.
_STAGNATION_MIN_DELTA = 0.01


def _composite_score(metrics: dict[str, Any] | None) -> float | None:
    """Collapse a round's ranking-agreement metrics into a single scalar
    used ONLY to detect stagnation across iterations (i.e. "did this round
    actually make the benchmark better, at all") — this is NOT used for any
    other purpose (the real, authoritative metrics the agent optimizes for
    are always the full model_metrics dict in report.json).

    Combines the two things prompts repeatedly ask the agent to jointly
    optimize: ranking agreement (spearman) and not letting scores collapse
    together (spread_ratio, penalized via distance from the ideal 1.0).
    Returns None if there isn't a real spearman to work with (e.g. an
    invalid/unscoreable round), so it never falsely looks like "no change".
    """
    if not metrics:
        return None
    spearman = metrics.get("spearman")
    if spearman is None:
        return None
    spread_ratio = metrics.get("spread_ratio")
    spread_penalty = 0.0 if spread_ratio is None else min(1.0, abs(spread_ratio - 1.0))
    return float(spearman) - 0.25 * spread_penalty


def run_opencode(workspace: Path, model: str | None, settings: Path,
                  time_budget_minutes: int, num_tasks: int,
                  candidate_models: list[str],
                  candidate_models_config: Path,
                  stagnation_patience: int = _DEFAULT_STAGNATION_PATIENCE) -> tuple[str, bool, int, int]:
    """Drive OpenCode through TWO distinct phases, both bounded by a TOTAL
    wall-clock time budget. There is NO fixed per-iteration time limit —
    each iteration round's OpenCode subprocess is simply given whatever
    total budget remains as its timeout, and in practice the agent ends the
    round itself by finishing its turn (typically right after writing
    summary.json) long before that backstop is ever reached. Nothing in
    run.py tells the agent "you get N minutes for this round" — it only
    shows the agent the total remaining budget plus its own historical
    per-iteration lengths (see `_build_iteration_prompt`), and the agent
    freely decides how much work to attempt before wrapping up and
    submitting (running evaluate.py) each round. A round boundary is
    therefore defined operationally as "whenever the agent chooses to end
    its current turn", not by any clock run.py imposes.

    PHASE 1 — ITERATION LOOP: repeatedly launch iteration rounds (iter_1,
    iter_2, ...), each a `--continue` of the same OpenCode session
    (iteration 1 is the only fresh session) so conversation history/context
    persists. Each round's subprocess timeout is simply the entire
    remaining total budget (no reserve is held back) — this is a backstop
    only, protecting against a genuinely stuck/runaway round consuming the
    whole rest of the session, not a pacing mechanism. This loop keeps
    launching new rounds, unconditionally (it does NOT stop early just
    because a round already produced a "valid" result — the whole point is
    to keep refining across iterations for as long as budget allows), until
    the remaining total budget drops below `_MIN_ITERATION_SECONDS`.

    After EVERY round (whether the agent ended it naturally or it was
    killed by the backstop timeout), run.py unconditionally snapshots the
    live verify.py / responses.json into iterations/iter_N/, runs the fixed
    evaluate.py against that snapshot, and ensures a summary.json exists
    (auto-generating a placeholder if the agent forgot) — so no round's
    work is ever silently lost, even one that got killed mid-way.

    PHASE 2 — FINALIZE ROUND: exactly one dedicated round, run once the
    iteration loop above has stopped (i.e. remaining budget is too small
    for another iteration round), using an entirely separate prompt
    (`_build_finalize_prompt`) whose only job is to tell the agent time is
    almost up and it must pick (or assemble) its final answer into the
    workspace root right now. This round is hard-bounded by whatever tiny
    bit of total budget is left — a real subprocess timeout that KILLS the
    process if the agent hasn't finished in time, forcing it to submit
    (pick or combine an existing iteration) rather than keep exploring,
    since there is no round after it to fall back on. After it ends
    (cleanly or killed), run.py snapshots it too (as a final iter_N+1) and
    then the whole function returns; run.py itself does not do any more
    OpenCode calls after this point, no matter what state the agent left
    things in.

    Returns (opencode_model, ran_out_of_total_budget, elapsed_seconds,
    num_iterations_completed) — num_iterations_completed counts BOTH the
    regular iteration rounds and the final FINALIZE round.
    """
    env, opencode_model = _opencode_env(settings, model)
    candidate_model_routes = load_candidate_model_routes(candidate_models_config)

    total_deadline = time.monotonic() + time_budget_minutes * 60
    total_start = time.monotonic()
    iteration = 0

    def _run_and_snapshot_round(round_prompt: str, round_seconds: int, continue_session: bool,
                                 label: str) -> dict[str, Any]:
        """Run one OpenCode subprocess round (hard-killed after
        `round_seconds` if it doesn't finish first) and unconditionally
        snapshot the resulting live files into iterations/iter_N/. Returns
        the `snap` dict from `snapshot_iteration` (iteration, is_valid,
        metrics, ...) so callers can track things like stagnation across
        rounds."""
        nonlocal iteration
        iteration += 1
        print(f"[run_opencode] {label} (iter_{iteration}): budget {round_seconds}s "
              f"({'fresh session' if not continue_session else '--continue'})")
        write_iteration_log_line(
            workspace,
            f"iter_{iteration} START label={label} budget={round_seconds}s "
            f"continue={continue_session}",
        )
        try:
            round_timed_out, round_elapsed = _run_opencode_once(
                workspace, env, opencode_model, round_prompt, round_seconds, continue_session,
                candidate_model_routes, allowed_models=set(candidate_models),
            )
        except RuntimeError as exc:
            print(f"[run_opencode] {label} (iter_{iteration}) FAILED: {exc}", file=sys.stderr)
            round_timed_out, round_elapsed = False, 0
        print(f"[run_opencode] {label} (iter_{iteration}) ended after {round_elapsed}s "
              f"(killed_by_timeout={round_timed_out})")

        # Regardless of what the agent did (or whether it was killed),
        # snapshot whatever live verify.py/responses.json exist into this
        # round's directory, run the official evaluate.py against that
        # snapshot, and make sure summary.json exists — so this round's work
        # is never lost even if it got killed mid-way. Pass along this
        # round's actual elapsed time + its budget so they get recorded into
        # summary.json's "timing" field (surfaced back to the agent on the
        # NEXT round via build_history_digest's per-iteration timing line +
        # TIME PACING SUMMARY).
        snap = snapshot_iteration(
            workspace, iteration, workspace / "truth.json", workspace / "evaluate.py",
            candidate_models, num_tasks,
            round_elapsed_seconds=round_elapsed, round_budget_seconds=round_seconds,
            candidate_model_routes=candidate_model_routes,
        )
        metrics = snap["metrics"] or {}
        print(f"[run_opencode] {label} (iter_{iteration}) snapshot: valid={snap['is_valid']} "
              f"({snap['reason']}) spearman={metrics.get('spearman')} "
              f"summary_written_by_agent={snap['summary_written_by_agent']} "
              f"evaluate_seconds={snap['evaluate_seconds']}")
        write_iteration_log_line(
            workspace,
            f"iter_{iteration} END label={label} elapsed={round_elapsed}s "
            f"budget={round_seconds}s killed_by_timeout={round_timed_out} valid={snap['is_valid']} "
            f"reason={snap['reason']!r} spearman={metrics.get('spearman')} "
            f"pairwise_accuracy={metrics.get('pairwise_accuracy')} bias={metrics.get('bias')} "
            f"spread_ratio={metrics.get('spread_ratio')} "
            f"summary_written_by_agent={snap['summary_written_by_agent']} "
            f"evaluate_seconds={snap['evaluate_seconds']}",
        )
        return snap

    # ---- STAGNATION TRACKING ----------------------------------------------
    # If `stagnation_patience` consecutive iteration rounds each fail to
    # beat the best composite score (spearman + spread_ratio, see
    # `_composite_score`) seen so far by more than `_STAGNATION_MIN_DELTA`,
    # the agent is very likely just re-shuffling files without making real
    # progress — stop the iteration loop early and let the finalize round
    # lock in the best iteration found, instead of burning the rest of the
    # total time budget on rounds that demonstrably aren't helping.
    best_score = float("-inf")
    best_score_iteration = 0
    stagnant_rounds = 0

    # ---- PHASE 1: iteration loop -----------------------------------------
    # Keep launching iteration rounds back-to-back, unconditionally, until
    # there isn't enough remaining total budget left to be worth starting
    # another one. Each round's subprocess timeout is the ENTIRE remaining
    # budget (a backstop against a stuck/runaway round, not a per-iteration
    # allowance) — the agent decides for itself, each round, how much of
    # that remaining budget to actually use before ending its turn.
    while True:
        remaining_seconds = int(total_deadline - time.monotonic())
        if remaining_seconds < _MIN_ITERATION_SECONDS:
            # Too little total budget left to be worth starting another
            # iteration round — stop the iteration loop and fall through to
            # the dedicated finalize round below, which gets whatever time
            # remains instead.
            break

        remaining_minutes = max(0, remaining_seconds // 60)
        next_iteration_num = iteration + 1
        round_prompt = _build_iteration_prompt(
            next_iteration_num, workspace, num_tasks, time_budget_minutes,
            remaining_minutes, candidate_models,
        )
        snap = _run_and_snapshot_round(
            round_prompt, remaining_seconds, continue_session=(iteration > 0),
            label="ITERATION",
        )

        # ---- stagnation check: did this round actually improve anything? --
        score = _composite_score(snap.get("metrics"))
        if score is not None and score > best_score + _STAGNATION_MIN_DELTA:
            best_score = score
            best_score_iteration = snap["iteration"]
            stagnant_rounds = 0
        else:
            stagnant_rounds += 1
            print(f"[run_opencode] iter_{snap['iteration']} did NOT improve the composite "
                  f"score (this round: {score}, best so far: "
                  f"{best_score if best_score != float('-inf') else None} @ "
                  f"iter_{best_score_iteration}) — stagnant_rounds={stagnant_rounds}"
                  f"{f'/{stagnation_patience}' if stagnation_patience > 0 else ''}")

        if stagnation_patience > 0 and stagnant_rounds >= stagnation_patience:
            write_iteration_log_line(
                workspace,
                f"EARLY STOP after iter_{snap['iteration']}: {stagnant_rounds} consecutive "
                f"iterations with no real improvement over best composite score "
                f"{best_score:.4f} (from iter_{best_score_iteration}) — ending the "
                "iteration loop early and moving to FINALIZE instead of continuing to "
                "spend the remaining time budget on non-improving rounds.",
            )
            print(f"[run_opencode] EARLY STOP: {stagnant_rounds} consecutive non-improving "
                  f"iterations (best composite score {best_score:.4f} was at "
                  f"iter_{best_score_iteration}) — skipping straight to the finalize round")
            break

        # ---- voluntary early submit: the agent itself said it's done -----
        # (see "ready_to_submit" in `_build_iteration_prompt` / snapshot_iteration).
        # Unlike the stagnation early-stop above (a harness-side heuristic
        # overriding the agent), this is the agent explicitly choosing to
        # stop and hand back whatever time budget remains rather than
        # having run.py force it to keep going or silently burn the clock.
        if snap.get("ready_to_submit"):
            write_iteration_log_line(
                workspace,
                f"EARLY SUBMIT after iter_{snap['iteration']}: agent set "
                "ready_to_submit=true in summary.json and the harness's own validity "
                "check agreed the snapshot is usable — ending the iteration loop now "
                "and moving to the coverage-repair + FINALIZE steps instead of "
                "continuing to spend the remaining time budget.",
            )
            print(f"[run_opencode] EARLY SUBMIT: agent declared iter_{snap['iteration']} "
                  "ready — skipping remaining iteration rounds")
            break

    # ---- PHASE 1.5: automatic candidate-model coverage repair -------------
    # Regardless of WHY the iteration loop just ended (voluntary early
    # submit, stagnation early-stop, or simply running out of total
    # budget), do one automatic pass over the live responses.json looking
    # for any (task, candidate model) pair that is still missing/empty —
    # almost always the result of a transient API failure earlier in the
    # session (rate limit, timeout, 5xx, a model burning its whole
    # max_tokens budget on a "thinking" block, etc.) rather than a real
    # design problem — and retry those exact calls for real before the
    # dedicated finalize round below locks in the workspace root as the
    # final submission. This is the harness actually *verifying* (not just
    # trusting the agent's word) that every candidate model can be called
    # successfully end-to-end through evaluate.py. Skipped entirely if
    # there's no live responses.json yet (nothing to repair) or if the
    # remaining budget is too small to be worth spending on network calls.
    remaining_for_repair = int(total_deadline - time.monotonic())
    live_responses_path = workspace / "responses.json"
    if live_responses_path.exists() and remaining_for_repair > 30:
        print(f"[run_opencode] running automatic candidate-model coverage repair "
              f"before finalize ({remaining_for_repair}s of budget remain)...")
        repair_report = repair_candidate_model_coverage(
            live_responses_path, candidate_models, candidate_models_config,
        )
        if repair_report:
            write_iteration_log_line(
                workspace,
                f"COVERAGE REPAIR: attempted={repair_report.get('attempted')} "
                f"repaired={repair_report.get('repaired')} "
                f"before={repair_report.get('before')} after={repair_report.get('after')} "
                f"still_missing={ {m: len(v) for m, v in repair_report.get('still_missing', {}).items()} }",
            )
            # Re-validate immediately after repair so the harness's own
            # verdict (and the agent, via the finalize round's history
            # digest) sees the up-to-date coverage/metrics rather than the
            # stale pre-repair snapshot.
            is_valid, reason, report, _ = check_valid_result(
                live_responses_path, workspace / "verify.py", workspace / "truth.json",
                workspace / "evaluate.py", candidate_models, num_tasks, run_cwd=workspace,
                candidate_model_routes=candidate_model_routes,
            )
            metrics = (report or {}).get("model_metrics") or {}
            print(f"[run_opencode] post-repair validity: is_valid={is_valid} ({reason}) "
                  f"spearman={metrics.get('spearman')} "
                  f"pairwise_accuracy={metrics.get('pairwise_accuracy')}")
    elif live_responses_path.exists():
        print(f"[run_opencode] skipping coverage repair — only {remaining_for_repair}s of "
              "budget remain, not worth spending on network calls")

    # ---- PHASE 2: single dedicated finalize round -------------------------
    # Whatever time is left (even if very little) goes to exactly one more
    # round whose only instructions are "pick/assemble your final answer and
    # put it in the workspace root" — never another regular iteration round.
    finalize_seconds = int(total_deadline - time.monotonic())
    ran_out_of_budget = finalize_seconds <= 30
    if not ran_out_of_budget:
        finalize_minutes = max(1, finalize_seconds // 60)
        finalize_prompt = _build_finalize_prompt(
            workspace, num_tasks, finalize_minutes,
        )
        _run_and_snapshot_round(
            finalize_prompt, finalize_seconds, continue_session=(iteration > 0),
            label="FINALIZE",
        )
    else:
        print("[run_opencode] essentially no budget left for a finalize round "
              f"({finalize_seconds}s) — skipping it; using whatever is already in "
              "the workspace root")

    elapsed = int(time.monotonic() - total_start)
    print(f"[run_opencode] session complete after {elapsed}s across {iteration} round(s) "
          f"total (iteration rounds + finalize round)")
    return opencode_model, ran_out_of_budget, elapsed, iteration


# ---------------------------------------------------------------------------
# Official evaluation (run by the harness, not the agent)
# ---------------------------------------------------------------------------


def run_official_evaluation(
    workspace: Path, truth_path: Path, output_path: Path,
    candidate_models_config: Path | None = None,
) -> dict[str, Any] | None:
    """Run evaluate.py ONCE against whatever the agent left behind in the
    workspace ROOT (verify.py + responses.json) — i.e. whatever the agent
    itself chose, before the time budget ran out, as its best/final
    submission. This is the authoritative, harness-triggered final
    submission.

    IMPORTANT: this deliberately does NOT fall back to iterations/iter_K/ if
    the root files are missing/broken/invalid. Once the total time budget is
    exhausted there is no more evaluation opportunity — the agent had to
    pick its best iteration and copy it into the workspace root itself
    (e.g. `cp iterations/iter_K/verify.py verify.py`) while it still had
    time. If it failed to do that, that failure is exactly what should show
    up in the final report, not be silently papered over by run.py picking
    a different iteration on the agent's behalf.

    NOTE: this evaluates only the VISIBLE candidate models the agent had
    access to (whatever models are already keyed in truth_path). The
    HELD-OUT models are added on top of this afterwards by
    `run_held_out_evaluation` below — this function's report is an
    intermediate result, not the final one, whenever held-out models are
    configured.
    """
    verify_path = workspace / "verify.py"
    responses_path = workspace / "responses.json"
    print(f"[official evaluation] using workspace root files (agent's own final choice): "
          f"{responses_path}, {verify_path}")
    if not responses_path.exists() or not verify_path.exists():
        print("[official evaluation] FAILED: agent did not leave responses.json/verify.py "
              "in the workspace root as its final submission", file=sys.stderr)
        return None
    candidate_model_routes = (
        load_candidate_model_routes(candidate_models_config) if candidate_models_config else None
    )
    result = run_evaluate_subprocess(
        workspace / "evaluate.py", responses_path, verify_path, truth_path, output_path,
        cwd=workspace, candidate_model_routes=candidate_model_routes,
    )
    print(result.stdout)
    if result.returncode:
        print(f"[official evaluation] FAILED:\n{result.stderr}", file=sys.stderr)
        return None
    if not output_path.exists():
        return None
    report = json.loads(output_path.read_text(encoding="utf-8"))
    report["_source"] = "workspace root (agent's final choice)"
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


# ---------------------------------------------------------------------------
# Held-out model evaluation — run ONLY by the harness, after the agent's
# session is completely over. The agent never sees these model names, never
# gets an API path to them (the CANDIDATE_MODEL_ENDPOINT proxy used during
# its session rejects anything outside its visible candidate_models list —
# see `_candidate_model_proxy`'s `allowed_models` argument), and truth.json
# inside the workspace never contains their pass rates either. This is what
# makes them a genuine, physically-unreachable held-out test set rather than
# just an instruction the agent is asked to respect: the agent's FINAL
# verify.py + task prompts are evaluated, after the fact, on model responses
# it never saw and could not have tuned anything against.
# ---------------------------------------------------------------------------


def _call_model_direct(
    routes: dict[str, Any], model_name: str, prompt: str, max_tokens: int = 16000,
    timeout: int = 240, max_retries: int = 6,
) -> str:
    """Call a single candidate model directly (Anthropic Messages API),
    using the same routing/credential-resolution logic as the in-session
    proxy (`_resolve_candidate_route`), but WITHOUT any allowed_models
    restriction — this is only ever called by the harness itself, after the
    agent's session has ended, to collect held-out models' responses.
    Returns the raw text response, or "" on any failure (network error,
    missing route, malformed response, etc.) so a single flaky call can't
    crash the whole final evaluation.

    Retries with exponential backoff, and specifically gives 429 (rate
    limit) responses much more room to succeed than other errors — a
    thundering-herd of concurrent held-out calls hitting a low-QPS model
    endpoint should eventually all get through rather than silently
    degrading to empty ("0-scored") responses for that model.

    IMPORTANT: max_tokens defaults to 16000 (not the previous 4096) because
    some candidate models emit extended "thinking"/reasoning content blocks
    BEFORE their final text answer, all counted against the same max_tokens
    budget. With a too-small budget, the whole response can be consumed by
    the thinking block, the call hits stop_reason="max_tokens" having never
    produced a single "text" content part, and this function would silently
    return "" — indistinguishable from a real failure, but actually just an
    output-budget problem, not a rate-limit/network/model problem. If a
    response is truncated (stop_reason == "max_tokens") with a non-empty
    thinking block but no text block, that's flagged explicitly below rather
    than being retried identically (retrying with the same max_tokens would
    just reproduce the same truncation).
    """
    endpoint, api_key, actual_model_name = _resolve_candidate_route(routes, model_name)
    if not endpoint:
        print(f"[held-out] no route configured for '{model_name}'", file=sys.stderr)
        return ""
    body = json.dumps({
        "model": actual_model_name,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    for attempt in range(max_retries):
        is_rate_limited = False
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            parts = payload.get("content") or []
            text = "".join(
                p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"
            )
            if text:
                return text
            stop_reason = payload.get("stop_reason")
            has_thinking = any(
                isinstance(p, dict) and p.get("type") == "thinking" for p in parts
            )
            if stop_reason == "max_tokens" and has_thinking:
                print(f"[held-out] '{model_name}' consumed its entire max_tokens={max_tokens} "
                      f"budget on a 'thinking' block and never reached a text answer "
                      f"(stop_reason=max_tokens) — this is an output-budget issue, not a "
                      f"transient failure; increase max_tokens if this persists.",
                      file=sys.stderr)
        except urllib.error.HTTPError as exc:
            is_rate_limited = exc.code == 429 or exc.code >= 500
            print(f"[held-out] attempt {attempt + 1}/{max_retries} failed for "
                  f"'{model_name}': HTTP {exc.code}", file=sys.stderr)
        except Exception as exc:
            print(f"[held-out] attempt {attempt + 1}/{max_retries} failed for '{model_name}': {exc}",
                  file=sys.stderr)
        if attempt < max_retries - 1:
            # Exponential backoff with jitter; rate-limit/server errors get a
            # longer base wait than generic transient errors (bad prompt,
            # timeout, etc.) since those are much less likely to self-heal
            # quickly.
            base = 8.0 if is_rate_limited else 2.0
            delay = base * (2 ** attempt) + random.uniform(0, 1.0)
            time.sleep(min(delay, 60.0))
    print(f"[held-out] giving up on '{model_name}' after {max_retries} attempts "
          f"— this model's score for this task will be recorded as empty", file=sys.stderr)
    return ""


def repair_candidate_model_coverage(
    responses_path: Path, candidate_models: list[str], candidate_models_config: Path,
    max_workers: int = 8, per_model_max_workers: int = 2, max_retries: int = 4,
) -> dict[str, Any]:
    """Scan the agent's current responses.json for any (task, candidate
    model) pair with a missing/empty response, retry those specific calls
    for real via the same direct-call path used for held-out models, and
    write the repaired file back to `responses_path` in place.

    This is run automatically once the agent signals it is done (see
    "ready_to_submit" in summary.json / `_build_iteration_prompt`) and
    right before the dedicated FINALIZE round, so a coverage gap caused by
    a transient API failure earlier in the session (rate limit, timeout,
    5xx, a model exhausting its max_tokens budget on a "thinking" block,
    etc.) doesn't get silently locked in as the agent's final submission
    just because the agent itself decided to stop early. It only ever
    touches responses that are already missing/empty — real responses the
    agent already collected are never re-fetched or overwritten.

    Returns a small report dict:
        {
            "total_tasks": int,
            "total_models": int,
            "before": {model: covered_count, ...},
            "attempted": int,               # number of (task, model) calls retried
            "repaired": int,                # how many of those attempts got a real response
            "after": {model: covered_count, ...},
            "still_missing": {model: [task_id, ...], ...},  # only non-empty entries
        }
    Returns {} if responses_path doesn't exist or isn't a JSON list.
    """
    import concurrent.futures

    if not responses_path.exists():
        return {}
    try:
        tasks = json.loads(responses_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(tasks, list) or not tasks:
        return {}

    def _coverage_counts(task_list: list[dict[str, Any]]) -> dict[str, int]:
        counts = {m: 0 for m in candidate_models}
        for t in task_list:
            mr = t.get("model_responses") if isinstance(t, dict) else None
            if not isinstance(mr, dict):
                continue
            for m in candidate_models:
                if mr.get(m):
                    counts[m] += 1
        return counts

    before_counts = _coverage_counts(tasks)

    missing_jobs = [
        (i, m, t.get("prompt", ""))
        for i, t in enumerate(tasks)
        for m in candidate_models
        if not (t.get("model_responses") or {}).get(m)
    ]
    print(f"[repair-coverage] {len(missing_jobs)} missing (task, model) pairs found "
          f"across {len(tasks)} tasks x {len(candidate_models)} candidate models "
          f"— before: {before_counts}")

    if not missing_jobs:
        return {
            "total_tasks": len(tasks), "total_models": len(candidate_models),
            "before": before_counts, "attempted": 0, "repaired": 0,
            "after": before_counts, "still_missing": {},
        }

    routes = load_candidate_model_routes(candidate_models_config)
    per_model_semaphores = {m: threading.Semaphore(per_model_max_workers) for m in candidate_models}

    def _call_with_model_limit(model: str, prompt: str) -> str:
        with per_model_semaphores[model]:
            return _call_model_direct(routes, model, prompt, max_retries=max_retries)

    results: dict[tuple[int, str], str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_job = {
            pool.submit(_call_with_model_limit, m, prompt): (i, m)
            for i, m, prompt in missing_jobs
        }
        done = 0
        for future in concurrent.futures.as_completed(future_to_job):
            i, m = future_to_job[future]
            try:
                results[(i, m)] = future.result()
            except Exception as exc:
                print(f"[repair-coverage] job (task={i}, model={m}) raised: {exc}", file=sys.stderr)
                results[(i, m)] = ""
            done += 1
            if done % 10 == 0 or done == len(missing_jobs):
                print(f"[repair-coverage] {done}/{len(missing_jobs)} retried")

    repaired = 0
    still_missing: dict[str, list[str]] = {}
    for (i, m), text in results.items():
        if not text:
            task_id = tasks[i].get("task_id", f"idx_{i}")
            still_missing.setdefault(m, []).append(task_id)
            continue
        t = tasks[i]
        model_responses = dict(t.get("model_responses") or {})
        model_responses[m] = text
        t["model_responses"] = model_responses
        repaired += 1

    responses_path.write_text(json.dumps(tasks, indent=2) + "\n", encoding="utf-8")

    after_counts = _coverage_counts(tasks)
    print(f"[repair-coverage] repaired {repaired}/{len(missing_jobs)} — after: {after_counts}")
    if still_missing:
        print(f"[repair-coverage] still missing after retry: "
              f"{ {m: len(v) for m, v in still_missing.items()} }", file=sys.stderr)

    return {
        "total_tasks": len(tasks), "total_models": len(candidate_models),
        "before": before_counts, "attempted": len(missing_jobs), "repaired": repaired,
        "after": after_counts, "still_missing": still_missing,
    }


def collect_held_out_responses(
    responses_path: Path, held_out_models: list[str], candidate_models_config: Path,
    max_workers: int = 8, per_model_max_workers: int = 2,
) -> dict[str, Any]:
    """Read the agent's FINAL responses.json (task prompts only — never the
    agent's own visible-model responses are reused for this), call every
    held-out model on every task's exact same prompt, and return a NEW list
    of task records that is the agent's original tasks with the held-out
    models' responses merged into each task's model_responses. Does NOT
    modify responses_path or the workspace's live files — callers write the
    result wherever the final held-out evaluation needs it (never back into
    the workspace root, so there is no way for a future iteration to ever
    see held-out models even if this were somehow run before the session
    ended).

    `per_model_max_workers` caps how many requests for the SAME model can be
    in flight at once (in addition to the global `max_workers` cap across
    all models combined) — some model endpoints have a much lower per-model
    QPS/concurrency quota than others, and without this a thundering herd of
    concurrent calls all targeting one low-quota model can get rate-limited
    (HTTP 429) so consistently that _call_model_direct's retries all fail
    together, silently degrading that entire model's responses to empty
    strings (which then score as 0 and wreck that model's ranking).
    """
    import concurrent.futures

    routes = load_candidate_model_routes(candidate_models_config)
    tasks = json.loads(responses_path.read_text(encoding="utf-8"))
    if not isinstance(tasks, list):
        tasks = tasks.get("tasks", [])

    jobs = [
        (i, model, t.get("prompt", ""))
        for i, t in enumerate(tasks)
        for model in held_out_models
    ]
    print(f"[held-out] collecting {len(jobs)} responses "
          f"({len(tasks)} tasks x {len(held_out_models)} held-out models)")

    per_model_semaphores = {
        model: threading.Semaphore(per_model_max_workers) for model in held_out_models
    }

    def _call_with_model_limit(model: str, prompt: str) -> str:
        sem = per_model_semaphores[model]
        with sem:
            return _call_model_direct(routes, model, prompt)

    results: dict[tuple[int, str], str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_job = {
            pool.submit(_call_with_model_limit, model, prompt): (i, model)
            for i, model, prompt in jobs
        }
        done = 0
        for future in concurrent.futures.as_completed(future_to_job):
            i, model = future_to_job[future]
            try:
                results[(i, model)] = future.result()
            except Exception as exc:
                print(f"[held-out] job (task={i}, model={model}) raised: {exc}", file=sys.stderr)
                results[(i, model)] = ""
            done += 1
            if done % 20 == 0 or done == len(jobs):
                print(f"[held-out] {done}/{len(jobs)} done")

    merged_tasks = []
    for i, t in enumerate(tasks):
        t = dict(t)
        model_responses = dict(t.get("model_responses") or {})
        for model in held_out_models:
            model_responses[model] = results.get((i, model), "")
        t["model_responses"] = model_responses
        merged_tasks.append(t)
    return merged_tasks


def run_held_out_evaluation(
    workspace: Path, run_dir: Path, full_truth_path: Path,
    visible_models: list[str], held_out_models: list[str],
    candidate_models_config: Path,
) -> dict[str, Any] | None:
    """The TRUE final evaluation: take the agent's final verify.py +
    responses.json (workspace root, visible-models only, exactly as the
    agent left them), call the HELD-OUT models directly (the agent never
    had any path to do this itself) on the same task prompts, and run the
    fixed evaluate.py against the FULL 10-model set (visible + held-out)
    using the FULL truth.json (which the agent's workspace copy never
    contained the held-out rows of).

    This is run entirely outside/after the agent's session — nothing here
    is reachable from inside the workspace during the session, so there is
    no way for the agent to see held-out responses, tune anything against
    them, or otherwise game this step.

    Returns a report dict with the usual evaluate.py shape (model_metrics/
    per_model/per_task computed over ALL 10 models) PLUS two extra keys,
    "visible_model_metrics" and "held_out_model_metrics", holding the same
    metrics recomputed restricted to just the 7 visible or just the 3
    held-out models respectively — so it's possible to see at a glance
    whether the benchmark generalizes to models the agent never saw, or
    only worked because the agent (implicitly or explicitly) fit its
    design to the 7 models it could actually observe.
    """
    verify_path = workspace / "verify.py"
    responses_path = workspace / "responses.json"
    if not responses_path.exists() or not verify_path.exists():
        print("[held-out evaluation] FAILED: no final verify.py/responses.json in "
              "workspace root", file=sys.stderr)
        return None

    merged_tasks = collect_held_out_responses(
        responses_path, held_out_models, candidate_models_config,
    )
    full_responses_path = run_dir / "responses_with_held_out.json"
    full_responses_path.write_text(json.dumps(merged_tasks, indent=2) + "\n", encoding="utf-8")

    candidate_model_routes = load_candidate_model_routes(candidate_models_config)
    full_report_path = run_dir / "report_full.json"
    result = run_evaluate_subprocess(
        workspace / "evaluate.py", full_responses_path, verify_path, full_truth_path,
        full_report_path, cwd=workspace, candidate_model_routes=candidate_model_routes,
    )
    print(result.stdout)
    if result.returncode:
        print(f"[held-out evaluation] evaluate.py failed:\n{result.stderr}", file=sys.stderr)
        return None
    if not full_report_path.exists():
        return None
    report = json.loads(full_report_path.read_text(encoding="utf-8"))

    # Recompute the same metrics restricted to just visible / just held-out
    # models, from the per_model breakdown evaluate.py already produced, so
    # both views are visible side by side in one report.
    per_model = report.get("per_model", {})

    def _subset_predicted_actual(model_subset: list[str]) -> tuple[dict[str, float], dict[str, float]]:
        predicted = {
            m: per_model[m]["predicted_score"] for m in model_subset
            if m in per_model and per_model[m].get("predicted_score") is not None
        }
        actual = {
            m: per_model[m]["actual_swebench_pass_rate"] for m in model_subset
            if m in per_model and per_model[m].get("actual_swebench_pass_rate") is not None
        }
        return predicted, actual

    def _subset_metrics(model_subset: list[str]) -> dict[str, Any]:
        predicted, actual = _subset_predicted_actual(model_subset)
        return compute_metrics_via_evaluate(predicted, actual)

    report["visible_model_metrics"] = _subset_metrics(visible_models)
    report["held_out_model_metrics"] = _subset_metrics(held_out_models)

    # Pairwise-accuracy breakdown by PAIR TYPE (not model subset): every
    # unordered pair among ALL models (visible + held-out) is bucketed into
    # "visible-visible" (both models visible) or "held-out-involved" (at
    # least one model is held-out — this MERGES the old heldout-heldout and
    # visible-heldout sub-categories into one number, since a 3-model
    # held-out set only yields 3 heldout-heldout pairs on its own, far too
    # few to be meaningful in isolation). This is the metric that actually
    # answers "does this agent's benchmark generalize to models it never
    # saw", and is computed ONLY here — after the agent's session is over —
    # never during the session itself (the agent's own/in-iteration
    # evaluate.py runs only ever see visible-models truth.json and have no
    # held-out data to compute this against in the first place).
    all_predicted, all_actual = _subset_predicted_actual(visible_models + held_out_models)
    breakdown = compute_pairwise_accuracy_breakdown_via_evaluate(
        all_predicted, all_actual, held_out_models,
    )
    report["pairwise_accuracy_breakdown"] = breakdown
    # Keep held_out_model_metrics's own "pairwise_accuracy" (pure
    # heldout-heldout, tiny sample) available under an explicit name, and
    # surface the merged held-out-involved number as the headline held-out
    # accuracy figure going forward.
    report["held_out_model_metrics"]["pairwise_accuracy_heldout_heldout_only"] = (
        report["held_out_model_metrics"].get("pairwise_accuracy")
    )
    report["held_out_model_metrics"]["pairwise_accuracy"] = breakdown["held-out-involved"]["accuracy"]

    report["_visible_models"] = sorted(visible_models)
    report["_held_out_models"] = sorted(held_out_models)
    report["_source"] = "workspace root + harness-collected held-out model responses"

    full_report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def collect_final_responses(
    responses_path: Path, all_models: list[str], candidate_models_config: Path,
    max_workers: int = 8, per_model_max_workers: int = 2,
    jsonl_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Read the agent's FINAL responses.json and RE-GENERATE every model's
    response from scratch (visible AND held-out models alike) by calling
    `_call_model_direct` concurrently on each (task, model) pair, using the
    EXACT prompt the agent wrote — never reusing the agent's own collected
    model_responses, which may be fabricated/stale/partially-broken.

    This is the harness's authoritative response-collection step, run ONLY
    after the agent's entire session has ended, from run.py's own process —
    there is no code path from inside the workspace/session that can reach
    this function or the held-out models it calls. The agent's role during
    the session is now ONLY to design task prompts + verify.py; it does NOT
    need to collect model responses itself anymore (and even if it did, any
    responses it collected are thrown away and replaced by the ones this
    function produces).

    Returns a NEW list of task records that is the agent's original tasks
    with FRESH model_responses freshly generated for every model in
    `all_models`. Does NOT modify `responses_path` or any live workspace
    file. If `jsonl_path` is provided, also writes a JSONL sidecar (one
    line per (task, model) pair) for inspection/debugging.
    """
    import concurrent.futures

    routes = load_candidate_model_routes(candidate_models_config)
    tasks = json.loads(responses_path.read_text(encoding="utf-8"))
    if not isinstance(tasks, list):
        tasks = tasks.get("tasks", [])

    jobs = [
        (i, model, t.get("prompt", ""))
        for i, t in enumerate(tasks)
        for model in all_models
    ]
    print(f"[final-responses] collecting {len(jobs)} responses "
          f"({len(tasks)} tasks x {len(all_models)} models) "
          f"(visible + held-out, ALL regenerated from agent's prompts)")

    per_model_semaphores = {
        model: threading.Semaphore(per_model_max_workers) for model in all_models
    }

    def _call_with_model_limit(model: str, prompt: str) -> str:
        sem = per_model_semaphores[model]
        with sem:
            return _call_model_direct(routes, model, prompt)

    results: dict[tuple[int, str], str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_job = {
            pool.submit(_call_with_model_limit, model, prompt): (i, model)
            for i, model, prompt in jobs
        }
        done = 0
        for future in concurrent.futures.as_completed(future_to_job):
            i, model = future_to_job[future]
            try:
                results[(i, model)] = future.result()
            except Exception as exc:
                print(f"[final-responses] job (task={i}, model={model}) raised: {exc}",
                      file=sys.stderr)
                results[(i, model)] = ""
            done += 1
            if done % 20 == 0 or done == len(jobs):
                print(f"[final-responses] {done}/{len(jobs)} done")

    if jsonl_path is not None:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with jsonl_path.open("w", encoding="utf-8") as fh:
            for i, t in enumerate(tasks):
                task_id = t.get("task_id", f"idx_{i}")
                prompt = t.get("prompt", "")
                for model in all_models:
                    fh.write(json.dumps({
                        "task_id": task_id,
                        "prompt": prompt,
                        "model": model,
                        "response": results.get((i, model), ""),
                    }, ensure_ascii=False) + "\n")

    merged_tasks: list[dict[str, Any]] = []
    for i, t in enumerate(tasks):
        t = dict(t)
        model_responses = dict(t.get("model_responses") or {})
        for model in all_models:
            model_responses[model] = results.get((i, model), "")
        t["model_responses"] = model_responses
        merged_tasks.append(t)
    return merged_tasks


def run_final_evaluation(
    workspace: Path, run_dir: Path, full_truth_path: Path,
    visible_models: list[str], held_out_models: list[str],
    candidate_models_config: Path,
) -> dict[str, Any] | None:
    """Anti-cheat confirmation pass: take the agent's final verify.py +
    task prompts from responses.json (workspace root), RE-GENERATE EVERY
    model's response from scratch (both visible AND held-out models) by
    calling them directly from the harness's own process — the agent's own
    collected model_responses are discarded and replaced with freshly-
    collected ones, so any fabricated/stale/partially-broken responses the
    agent may have written into responses.json cannot inflate the score.

    This runs AFTER (and in addition to) the normal `run_official_evaluation`
    (visible, agent-collected) and `run_held_out_evaluation` (held-out,
    harness-collected) steps — those two are kept as-is so the agent's own
    work during the session is still reflected in the primary reports.
    This function is purely an extra confirmation pass: it re-runs every
    model on the agent's prompts from scratch and scores them again, so if
    the numbers from this pass disagree wildly with the primary reports,
    that's a strong signal the agent gamed its own responses.json.

    Writes its own output to report_final_confirm.json (and a JSONL sidecar
    final_responses.jsonl of the freshly-collected responses) so it never
    overwrites the primary report.json / report_full.json from the two
    steps above. Returns the report dict, or None on failure.
    """
    verify_path = workspace / "verify.py"
    responses_path = workspace / "responses.json"
    if not responses_path.exists() or not verify_path.exists():
        print("[final confirm] FAILED: no final verify.py/responses.json in "
              "workspace root", file=sys.stderr)
        return None

    all_models = sorted(set(visible_models) | set(held_out_models))
    final_jsonl_path = run_dir / "final_responses.jsonl"
    merged_tasks = collect_final_responses(
        responses_path, all_models, candidate_models_config,
        jsonl_path=final_jsonl_path,
    )
    confirm_responses_path = run_dir / "responses_final_confirm.json"
    confirm_responses_path.write_text(json.dumps(merged_tasks, indent=2) + "\n", encoding="utf-8")

    candidate_model_routes = load_candidate_model_routes(candidate_models_config)
    confirm_report_path = run_dir / "report_final_confirm.json"
    result = run_evaluate_subprocess(
        workspace / "evaluate.py", confirm_responses_path, verify_path, full_truth_path,
        confirm_report_path, cwd=workspace, candidate_model_routes=candidate_model_routes,
    )
    print(result.stdout)
    if result.returncode:
        print(f"[final confirm] evaluate.py failed:\n{result.stderr}", file=sys.stderr)
        return None
    if not confirm_report_path.exists():
        return None
    report = json.loads(confirm_report_path.read_text(encoding="utf-8"))

    per_model = report.get("per_model", {})

    def _subset_predicted_actual(model_subset: list[str]) -> tuple[dict[str, float], dict[str, float]]:
        predicted = {
            m: per_model[m]["predicted_score"] for m in model_subset
            if m in per_model and per_model[m].get("predicted_score") is not None
        }
        actual = {
            m: per_model[m]["actual_swebench_pass_rate"] for m in model_subset
            if m in per_model and per_model[m].get("actual_swebench_pass_rate") is not None
        }
        return predicted, actual

    def _subset_metrics(model_subset: list[str]) -> dict[str, Any]:
        predicted, actual = _subset_predicted_actual(model_subset)
        return compute_metrics_via_evaluate(predicted, actual)

    report["visible_model_metrics"] = _subset_metrics(visible_models)
    report["held_out_model_metrics"] = _subset_metrics(held_out_models)

    all_predicted, all_actual = _subset_predicted_actual(visible_models + held_out_models)
    breakdown = compute_pairwise_accuracy_breakdown_via_evaluate(
        all_predicted, all_actual, held_out_models,
    )
    report["pairwise_accuracy_breakdown"] = breakdown
    report["held_out_model_metrics"]["pairwise_accuracy_heldout_heldout_only"] = (
        report["held_out_model_metrics"].get("pairwise_accuracy")
    )
    report["held_out_model_metrics"]["pairwise_accuracy"] = breakdown["held-out-involved"]["accuracy"]

    report["_visible_models"] = sorted(visible_models)
    report["_held_out_models"] = sorted(held_out_models)
    report["_source"] = (
        "ANTI-CHEAT CONFIRMATION: workspace root verify.py + task prompts; "
        "ALL model responses (visible + held-out) re-generated by harness "
        "from the agent's prompts (agent's own responses.json responses "
        "discarded)"
    )

    confirm_report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def compute_metrics_via_evaluate(predicted: dict[str, float], actual: dict[str, float]) -> dict[str, Any]:
    """Thin wrapper around evaluate.py's compute_metrics so run.py can reuse
    the exact same ranking-metric formulas for the visible-only /
    held-out-only breakdowns without duplicating that logic."""
    mod = _load_evaluate_module()
    return mod.compute_metrics(predicted, actual)


def compute_pairwise_accuracy_breakdown_via_evaluate(
    predicted: dict[str, float], actual: dict[str, float], held_out_models: list[str],
) -> dict[str, Any]:
    """Thin wrapper around evaluate.py's pairwise_accuracy_breakdown so
    run.py's final held-out report and evaluate.py's own CLI output use the
    exact same visible-visible / held-out-involved pair-type split logic —
    no duplicated implementation to drift out of sync."""
    mod = _load_evaluate_module()
    return mod.pairwise_accuracy_breakdown(predicted, actual, held_out_models)


def _load_evaluate_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("autoeval_evaluate", REPO / "evaluate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-name", required=True, help="Name for this run")
    parser.add_argument("--results-dir", type=Path, default=REPO / "data" / "results_agent",
                        help="Directory containing model subdirectories with traj .jsonl files, "
                             "used to compute truth.json (real SWE-Bench pass rate per model)")
    parser.add_argument("--runs-dir", type=Path, default=REPO / "runs")
    parser.add_argument("--time-budget-minutes", type=int, default=360,
                        help="Total wall-clock minutes for the whole session, spent across as "
                             "many structured iterations as fit (default: 360 = 6h)")
    parser.add_argument("--num-tasks", type=int, default=50,
                        help="Number of brand-new tasks the agent should design (default: 50)")
    parser.add_argument("--stagnation-patience", type=int, default=_DEFAULT_STAGNATION_PATIENCE,
                        help="Stop the iteration loop early (and move straight to the finalize "
                             "round) after this many CONSECUTIVE iterations in a row with no "
                             "real improvement in the composite spearman/spread_ratio score "
                             f"(default: {_DEFAULT_STAGNATION_PATIENCE}). Pass 0 to disable "
                             "early stopping and always run until the time budget itself runs "
                             "out, even if the agent stops making progress.")
    parser.add_argument("--model", help="OpenCode model id")
    parser.add_argument("--settings", type=Path, default=Path.home() / ".claude" / "settings.json")
    parser.add_argument("--candidate-models-config", type=Path,
                        default=DEFAULT_CANDIDATE_MODELS_CONFIG,
                        help="Local (not-checked-in) JSON file mapping candidate model names "
                             "to their real endpoint/api_key/model_name, used by the "
                             "CANDIDATE_MODEL_ENDPOINT proxy the agent calls from inside the "
                             f"workspace (default: {DEFAULT_CANDIDATE_MODELS_CONFIG}). If "
                             "missing, candidate models are called directly with no "
                             "credential rewriting.")
    parser.add_argument("--held-out-models", type=str,
                        default=",".join(DEFAULT_HELD_OUT_MODELS),
                        help="Comma-separated list of model names to keep held out from the "
                             "agent for the ENTIRE session (not in its truth.json, not "
                             "callable through CANDIDATE_MODEL_ENDPOINT) and only score "
                             f"afterwards, by the harness itself (default: "
                             f"{','.join(DEFAULT_HELD_OUT_MODELS)}). Must be a subset of the "
                             "models found in --results-dir. Pass an empty string to disable "
                             "held-out models entirely (agent sees/uses all models).")
    parser.add_argument("--data-train-dir", type=Path, default=DEFAULT_DATA_TRAIN_DIR,
                        help="Fixed, pre-populated directory (sibling of data/) holding real "
                             "SWE-Bench Verified trajectory files for the VISIBLE models only "
                             "(built once, out of band — see DEFAULT_DATA_TRAIN_DIR). The "
                             "workspace gets a symlink to this directory as "
                             f"reference_trajectories/ (default: {DEFAULT_DATA_TRAIN_DIR}). If "
                             "missing, the agent simply won't have reference trajectories this "
                             "run (not a hard failure).")
    parser.add_argument("--prepare-only", action="store_true", help="Only create workspace, don't run agent")
    args = parser.parse_args(argv)

    # Build run directory
    run_dir = (args.runs_dir / args.run_name).resolve()
    if run_dir.exists():
        raise FileExistsError(f"Run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True)

    workspace = (run_dir / "workspace").resolve()
    workspace.mkdir()

    # Copy verify.py into workspace (agent editable)
    shutil.copy2(REPO / "verify.py", workspace / "verify.py")

    # Copy evaluate.py into workspace (agent should NOT edit)
    shutil.copy2(REPO / "evaluate.py", workspace / "evaluate.py")

    # Compute the FULL truth (ALL models found in results_agent/, including
    # held-out ones) once, up front, but keep it OUTSIDE the workspace (in
    # run_dir, one level up) — the agent's workspace only ever gets the
    # VISIBLE-models-only truth.json below. The full version is only read
    # again at the very end, by run_held_out_evaluation, after the agent's
    # session has completely ended.
    full_truth_path = run_dir / "truth_full.json"
    full_truth = build_truth_json(args.results_dir, full_truth_path)

    if not full_truth:
        print("ERROR: no models found in results_dir", file=sys.stderr)
        return 1

    held_out_models = sorted(
        m.strip() for m in args.held_out_models.split(",") if m.strip()
    )
    unknown_held_out = [m for m in held_out_models if m not in full_truth]
    if unknown_held_out:
        print(f"ERROR: --held-out-models contains model(s) not found in --results-dir: "
              f"{unknown_held_out}", file=sys.stderr)
        return 1

    visible_models = sorted(m for m in full_truth if m not in held_out_models)
    if len(visible_models) < 2:
        print("ERROR: fewer than 2 visible models remain after removing held-out models",
              file=sys.stderr)
        return 1

    # The workspace's truth.json — the ONLY ground truth the agent ever
    # sees — contains ONLY the visible models' real pass rates. Held-out
    # models' names/pass rates never touch the workspace at any point.
    truth_path = workspace / "truth.json"
    visible_truth = {m: full_truth[m] for m in visible_models}
    truth_path.write_text(json.dumps(visible_truth, indent=2) + "\n", encoding="utf-8")

    candidate_models = visible_models

    # Symlink (not copy) reference_trajectories/ into the workspace, pointing
    # at the fixed, repo-level data_train/ directory that was already
    # populated (once, out of band) with only the visible models' real
    # SWE-Bench Verified trajectory files. See link_reference_trajectories's
    # docstring — this never touches held_out_models at all, by construction.
    link_reference_trajectories(args.data_train_dir, workspace / "reference_trajectories")

    if args.prepare_only:
        print(f"Prepared workspace: {workspace}")
        print(f"  truth.json (visible to agent): {len(visible_truth)} models")
        for m, rate in sorted(visible_truth.items(), key=lambda kv: -kv[1]):
            print(f"    - {m}: {rate*100:.1f}%")
        if held_out_models:
            print(f"  HELD-OUT models (never visible/callable during the session, "
                  f"scored only at the very end): {held_out_models}")
        print(f"  verify.py: editable")
        print(f"  evaluate.py: fixed")
        print(f"  responses.json: agent must create ({args.num_tasks} tasks)")
        print(f"  reference_trajectories/: symlink -> {args.data_train_dir}")
        return 0

    # Drive OpenCode through a series of structured, versioned iterations
    # (iterations/iter_1/, iter_2/, ... under the workspace), back-to-back
    # until the TOTAL time budget runs out. There is no fixed per-iteration
    # limit — each round runs until the agent itself ends its turn. Only
    # `candidate_models` (== visible_models) is ever passed in here, and
    # that's also the exact allowlist the CANDIDATE_MODEL_ENDPOINT proxy
    # enforces for the whole session (see run_opencode -> _run_opencode_once
    # -> _candidate_model_proxy) — held_out_models never appears below this
    # point until run_held_out_evaluation, after the session is over.
    print(f"\n=== Research session: {args.time_budget_minutes} minute TOTAL budget "
          f"(no fixed per-iteration limit — agent self-paces), {args.num_tasks} tasks, "
          f"{len(candidate_models)} VISIBLE candidate models "
          f"({len(held_out_models)} held out) ===")
    _, ran_out_of_budget, elapsed, num_iterations = run_opencode(
        workspace, args.model, args.settings, args.time_budget_minutes,
        args.num_tasks, candidate_models,
        args.candidate_models_config,
        stagnation_patience=args.stagnation_patience,
    )
    print(f"Session ended after {elapsed}s across {num_iterations} iteration(s) "
          f"(ran_out_of_budget={ran_out_of_budget})")

    # Official, harness-triggered final submission, always run regardless of
    # what the agent did during its session — evaluates ONLY the workspace
    # ROOT verify.py/responses.json, i.e. whatever the agent itself chose as
    # its final/best iteration and copied there before time ran out, against
    # the VISIBLE models only. No automatic fallback to iterations/iter_K/
    # happens here: once the budget is exhausted there is no more evaluation
    # opportunity, so the agent's own choice (or failure to choose) is
    # exactly what gets scored. This is an intermediate report — see below
    # for the TRUE final report that also includes held-out models.
    print("\n=== Official final submission (visible models only) ===")
    final_report_path = run_dir / "report.json"
    report = run_official_evaluation(
        workspace, truth_path, final_report_path,
        candidate_models_config=args.candidate_models_config,
    )
    if report is not None:
        print(f"\n=== Visible-Models Report ===")
        print(f"Source: {report.get('_source')}")
        print(f"Models: {report['summary']['num_models']}")
        print(f"Tasks: {report['summary']['num_tasks']}")
        print(f"Pairwise accuracy: {report['model_metrics']['pairwise_accuracy']}")
        print(f"Spearman: {report['model_metrics']['spearman']}")
        print(f"Top-1 match: {report['model_metrics']['top1_match']}")
        print(f"Iterations completed: {num_iterations} "
              f"(see {workspace / 'iterations'} for the full history)")
    else:
        print("WARNING: official evaluation did not produce a report", file=sys.stderr)

    # TRUE final evaluation: now that the agent's session is completely
    # over, call the HELD-OUT models directly (the agent never had a way to
    # do this itself) on the agent's own final task prompts, and re-run
    # evaluate.py against the FULL model set (visible + held-out) using the
    # full truth.json. This is the number that actually matters — it shows
    # whether the agent's from-scratch benchmark + verify.py generalizes to
    # models it never saw, or only worked by fitting to the 7 it could
    # observe.
    if held_out_models:
        print(f"\n=== Held-out evaluation ({len(held_out_models)} models the agent never "
              f"saw: {held_out_models}) ===")
        full_report = run_held_out_evaluation(
            workspace, run_dir, full_truth_path, visible_models, held_out_models,
            args.candidate_models_config,
        )
        if full_report is not None:
            vm = full_report.get("visible_model_metrics", {})
            hm = full_report.get("held_out_model_metrics", {})
            breakdown = full_report.get("pairwise_accuracy_breakdown", {})
            vv = breakdown.get("visible-visible", {})
            hi = breakdown.get("held-out-involved", {})
            print(f"\n=== TRUE Final Report (all {len(full_truth)} models) ===")
            print(f"Overall — pairwise_accuracy: {full_report['model_metrics']['pairwise_accuracy']} "
                  f"spearman: {full_report['model_metrics']['spearman']}")
            print(f"Visible ({len(visible_models)} models) — pairwise_accuracy: "
                  f"{vm.get('pairwise_accuracy')} spearman: {vm.get('spearman')}")
            print(f"Held-out ({len(held_out_models)} models) — pairwise_accuracy: "
                  f"{hm.get('pairwise_accuracy')} spearman: {hm.get('spearman')}")
            print(f"  (this held-out pairwise_accuracy is the merged 'held-out-involved' "
                  f"pair-type breakdown — heldout-heldout + visible-heldout pairs combined — "
                  f"since heldout-heldout alone only has "
                  f"{len(held_out_models) * (len(held_out_models) - 1) // 2} pairs, too few to "
                  f"be meaningful; the pure heldout-heldout number is still available at "
                  f"held_out_model_metrics.pairwise_accuracy_heldout_heldout_only)")
            print(f"Pair-type breakdown — visible-visible: {vv.get('accuracy')} "
                  f"({vv.get('correct')}/{vv.get('total')}); "
                  f"held-out-involved: {hi.get('accuracy')} ({hi.get('correct')}/{hi.get('total')})")
            print(f"Full report written to {run_dir / 'report_full.json'}")
        else:
            print("WARNING: held-out evaluation did not produce a report", file=sys.stderr)
    else:
        print("\n(no held-out models configured — the visible-models report above is final)")

    # ------------------------------------------------------------------
    # ANTI-CHEAT CONFIRMATION PASS (extra, does NOT replace the reports
    # above). Re-generates EVERY model's response (visible + held-out) from
    # scratch using the agent's final task prompts, then scores them with the
    # agent's verify.py. This makes sure the numbers in report.json /
    # report_full.json above were not inflated by fabricated/stale/curated
    # responses the agent wrote into responses.json: if the agent cheated on
    # model_responses, the confirmation numbers will disagree wildly with
    # the primary reports, which is itself a strong cheating signal. The
    # primary reports above are kept as-is so the agent's own session work is
    # never thrown away; this pass only adds an independent cross-check.
    # ------------------------------------------------------------------
    print("\n=== Anti-cheat confirmation: re-generating ALL model responses "
          "from agent's prompts with harness, then re-scoring ===")
    confirm_report = run_final_evaluation(
        workspace, run_dir, full_truth_path, visible_models, held_out_models,
        args.candidate_models_config,
    )
    if confirm_report is not None:
        print(f"\n=== Anti-Cheat Confirmation Report (all {len(full_truth)} models, "
              f"harness-regenerated responses) ===")
        print(f"Source: {confirm_report.get('_source')}")
        print(f"Tasks: {confirm_report['summary']['num_tasks']}")
        print(f"Overall — pairwise_accuracy: {confirm_report['model_metrics']['pairwise_accuracy']} "
              f"spearman: {confirm_report['model_metrics']['spearman']}")
        cv = confirm_report.get("visible_model_metrics", {})
        ch = confirm_report.get("held_out_model_metrics", {})
        print(f"Visible — pairwise_accuracy: {cv.get('pairwise_accuracy')} "
              f"spearman: {cv.get('spearman')}")
        if held_out_models:
            print(f"Held-out — pairwise_accuracy: {ch.get('pairwise_accuracy')} "
                  f"spearman: {ch.get('spearman')}")
        print(f"Fresh responses written to {run_dir / 'final_responses.jsonl'} "
              f"+ {run_dir / 'responses_final_confirm.json'}")
        print(f"Confirmation report written to {run_dir / 'report_final_confirm.json'}")
    else:
        print("WARNING: anti-cheat confirmation did not produce a report", file=sys.stderr)

    print(f"\nRun complete: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
