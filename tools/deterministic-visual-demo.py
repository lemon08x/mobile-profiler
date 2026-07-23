"""Run a synthetic OpenCV template-matching spike and persist visual evidence."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mobile_profiler.automation import (  # noqa: E402
    Artifact,
    NormalizedRegion,
    OpenCvTemplateMatcher,
    ScreenFrame,
    TemplateSpec,
    template_matching_dependency_status,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an Android-sized synthetic frame and benchmark template matching."
    )
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "profiler-runs" / "deterministic-visual-spike",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    available, detail = template_matching_dependency_status()
    if not available:
        print(detail, file=sys.stderr)
        print('Install with: python -m pip install -e ".[image]"', file=sys.stderr)
        return 2
    if args.iterations <= 0:
        raise ValueError("--iterations must be positive")

    import cv2
    import numpy

    output_root = args.output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    rng = numpy.random.default_rng(20260722)
    frame = numpy.zeros((1280, 720, 3), dtype=numpy.uint8)
    frame[:] = (24, 28, 36)
    for _ in range(80):
        x = int(rng.integers(0, frame.shape[1]))
        y = int(rng.integers(0, frame.shape[0]))
        radius = int(rng.integers(2, 8))
        color = tuple(int(value) for value in rng.integers(40, 130, size=3))
        cv2.circle(frame, (x, y), radius, color, -1)

    template = rng.integers(0, 255, size=(96, 144, 3), dtype=numpy.uint8)
    cv2.rectangle(template, (4, 4), (139, 91), (255, 255, 255), 3)
    cv2.circle(template, (72, 48), 24, (30, 210, 250), -1)
    cv2.putText(
        template,
        "PLAY",
        (34, 56),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (10, 10, 10),
        2,
        cv2.LINE_AA,
    )
    expected_left, expected_top = 438, 846
    frame[
        expected_top : expected_top + template.shape[0],
        expected_left : expected_left + template.shape[1],
    ] = template

    encoded_frame, frame_payload = cv2.imencode(".png", frame)
    encoded_template, template_payload = cv2.imencode(".png", template)
    if not encoded_frame or not encoded_template:
        raise RuntimeError("unable to encode synthetic images")
    frame_bytes = frame_payload.tobytes()
    template_bytes = template_payload.tobytes()
    (output_root / "frame.png").write_bytes(frame_bytes)
    (output_root / "template.png").write_bytes(template_bytes)

    screen = ScreenFrame(
        Artifact("demo-frame", "image/png", data=frame_bytes),
        width=frame.shape[1],
        height=frame.shape[0],
    )
    spec = TemplateSpec(
        "play-button",
        template_data=template_bytes,
        threshold=0.98,
        region=NormalizedRegion(0.45, 0.55, 0.95, 0.95),
        scales=(0.9, 1.0, 1.1),
    )
    matcher = OpenCvTemplateMatcher()
    matcher.match(screen, spec)  # warm the template and same-frame caches
    cached_matches = [matcher.match(screen, spec) for _ in range(args.iterations)]
    fresh_screens = []
    for index in range(args.iterations):
        variant = frame.copy()
        variant[0, index % variant.shape[1]] = (
            index % 251,
            (index * 3) % 251,
            (index * 7) % 251,
        )
        encoded_variant, variant_payload = cv2.imencode(".png", variant)
        if not encoded_variant:
            raise RuntimeError("unable to encode a synthetic frame variant")
        fresh_screens.append(
            ScreenFrame(
                Artifact(
                    f"demo-frame-{index}",
                    "image/png",
                    data=variant_payload.tobytes(),
                ),
                width=screen.width,
                height=screen.height,
            )
        )
    fresh_matches = [matcher.match(fresh_screen, spec) for fresh_screen in fresh_screens]
    match = fresh_matches[-1]

    def timing_summary(matches):
        elapsed_ms = sorted(item.elapsed_s * 1000 for item in matches)
        p95_index = max(
            0,
            min(len(elapsed_ms) - 1, math.ceil(len(elapsed_ms) * 0.95) - 1),
        )
        return {
            "mean_ms": sum(elapsed_ms) / len(elapsed_ms),
            "p95_ms": elapsed_ms[p95_index],
            "minimum_ms": elapsed_ms[0],
            "maximum_ms": elapsed_ms[-1],
        }

    cached_timing = timing_summary(cached_matches)
    fresh_timing = timing_summary(fresh_matches)
    overlay = matcher.annotated_artifact(screen, match, artifact_id="demo-overlay")
    overlay_path = output_root / "match-overlay.png"
    overlay_path.write_bytes(overlay.data)
    result = {
        "dependency": detail,
        "iterations": args.iterations,
        "frame": {"width": screen.width, "height": screen.height},
        "matched": match.matched,
        "score": match.score,
        "threshold": match.threshold,
        "scale": match.scale,
        "bounds": (
            [match.bounds.left, match.bounds.top, match.bounds.right, match.bounds.bottom]
            if match.bounds is not None
            else None
        ),
        "expected_bounds": [
            expected_left,
            expected_top,
            expected_left + template.shape[1],
            expected_top + template.shape[0],
        ],
        "same_observation_timing": cached_timing,
        "fresh_observation_timing": fresh_timing,
        "mean_ms": fresh_timing["mean_ms"],
        "p95_ms": fresh_timing["p95_ms"],
        "artifacts": {
            "frame": str(output_root / "frame.png"),
            "template": str(output_root / "template.png"),
            "overlay": str(overlay_path),
        },
    }
    result_path = output_root / "result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if match.matched and result["bounds"] == result["expected_bounds"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
