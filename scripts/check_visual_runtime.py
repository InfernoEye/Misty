"""Check local visual-model artifacts, or explicitly run a synthetic image probe.

Default: filesystem metadata only. --run-model --confirm-models-stopped starts an
owned local model after the user acknowledges other heavy models are stopped.
No desktop is captured and no desktop/provider tool is ever executed.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import threading
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.brain.visual_decision import (
    VisualDecisionRuntime, VisualRuntimeConfig, VisualDecisionCancelled,
    VisualDecisionTimeout,
)


def probe(*, run_model=False, confirm_models_stopped=False, timeout=120.0,
          config=None, runtime_factory=VisualDecisionRuntime):
    if not isinstance(run_model, bool) or not isinstance(confirm_models_stopped, bool):
        raise ValueError("Probe choices must be boolean")
    if run_model and not confirm_models_stopped:
        raise ValueError("Explicit acknowledgement that other heavy models are stopped is required")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 1 <= timeout <= 300):
        raise ValueError("Timeout must be finite and between 1 and 300 seconds")
    config = config or VisualRuntimeConfig()
    artifacts = {name: Path(getattr(config, name)).is_file()
                 for name in ("server_path", "model_path", "projector_path")}
    summary = {
        "mode": "synthetic_image_probe" if run_model else "artifacts_only",
        "artifacts": artifacts,
        "model_probe_attempted": False,
        "runtime_ready": False,
        "image_input": None,
        "structured_output": None,
        "probe_passed": False,
        "grounding_verified": False,
        "input_ready": False,
        "cleanup_confirmed": True,
        "phase_gate": "artifacts_present" if all(artifacts.values()) else "artifacts_missing",
    }
    if not all(artifacts.values()):
        return summary, 2
    if not run_model:
        return summary, 0
    runtime = None
    cancel = threading.Event()
    deadline = time.monotonic() + timeout
    code = 2
    try:
        runtime = runtime_factory(config=config)
        summary["model_probe_attempted"] = True
        readiness = runtime.start(cancel=cancel, deadline=deadline)
        summary["runtime_ready"] = True
        summary["image_input"] = readiness.image_input is True
        summary["structured_output"] = readiness.structured_output is True
        summary["probe_passed"] = readiness.probe_passed is True
        if all(summary[key] is True for key in ("image_input", "structured_output", "probe_passed")):
            summary["phase_gate"] = "synthetic_image_only_desktop_unverified"
            code = 0
        else:
            summary["phase_gate"] = "visual_probe_failed"
    except (KeyboardInterrupt, VisualDecisionCancelled):
        cancel.set()
        summary["phase_gate"] = "canceled"
        code = 130
    except VisualDecisionTimeout:
        summary["phase_gate"] = "timeout"
    except Exception:
        # Runtime/native failures may include prompts/paths; do not publish prose.
        summary["phase_gate"] = "visual_probe_failed"
    finally:
        if runtime is not None:
            try:
                runtime.close()
            except Exception:
                summary["cleanup_confirmed"] = False
                summary["phase_gate"] = "cleanup_unconfirmed"
                code = 2
    return summary, code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-model", action="store_true",
                        help="Run the real local synthetic image/JSON probe; no desktop capture.")
    parser.add_argument("--confirm-models-stopped", action="store_true",
                        help="Acknowledge Misty and other heavyweight model processes are stopped.")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    if args.run_model != args.confirm_models_stopped:
        parser.error("Use --run-model and --confirm-models-stopped together")
    try:
        summary, code = probe(run_model=args.run_model,
                             confirm_models_stopped=args.confirm_models_stopped,
                             timeout=args.timeout)
    except ValueError:
        parser.error("Timeout must be finite and between 1 and 300 seconds")
    print(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
