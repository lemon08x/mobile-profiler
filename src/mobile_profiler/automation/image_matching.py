"""Optional OpenCV template matching and deterministic verification.

The public automation contracts remain standard-library-only.  This adapter
imports NumPy and OpenCV lazily so normal profiler, recorder, and semantic UI
workflows keep working when the optional image extra is not installed.
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .contracts import (
    Artifact,
    Bounds,
    ComponentDescriptor,
    Observation,
    ScreenFrame,
    VerificationRequest,
    VerificationResult,
    VerificationStatus,
)
from .ports import EvidenceSink


def template_matching_dependency_status() -> tuple[bool, str]:
    """Return whether the optional NumPy/OpenCV runtime can be imported."""

    try:
        import cv2  # type: ignore[import-not-found]
        import numpy  # type: ignore[import-not-found]
    except (ImportError, OSError) as exc:
        return False, f"OpenCV template matching is unavailable: {exc}"
    return True, f"OpenCV {cv2.__version__} / NumPy {numpy.__version__}"


@dataclass(frozen=True)
class NormalizedRegion:
    """A left/top/right/bottom crop expressed in the inclusive 0..1 range."""

    left: float = 0.0
    top: float = 0.0
    right: float = 1.0
    bottom: float = 1.0

    def __post_init__(self) -> None:
        values = (self.left, self.top, self.right, self.bottom)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("normalized region values must be finite")
        if not (0.0 <= self.left < self.right <= 1.0):
            raise ValueError("normalized region must satisfy 0 <= left < right <= 1")
        if not (0.0 <= self.top < self.bottom <= 1.0):
            raise ValueError("normalized region must satisfy 0 <= top < bottom <= 1")

    def pixel_bounds(self, width: int, height: int) -> Bounds:
        if width <= 0 or height <= 0:
            raise ValueError("image dimensions must be positive")
        left = min(width - 1, max(0, int(round(self.left * width))))
        top = min(height - 1, max(0, int(round(self.top * height))))
        right = min(width, max(left + 1, int(round(self.right * width))))
        bottom = min(height, max(top + 1, int(round(self.bottom * height))))
        return Bounds(left, top, right, bottom)


@dataclass(frozen=True)
class TemplateSpec:
    """One versioned template and its matching policy."""

    template_id: str
    template_path: str = ""
    template_data: bytes = b""
    threshold: float = 0.90
    region: NormalizedRegion = field(default_factory=NormalizedRegion)
    scales: tuple[float, ...] = (1.0,)
    grayscale: bool = True

    def __post_init__(self) -> None:
        template_id = str(self.template_id or "").strip()
        if not template_id:
            raise ValueError("template_id cannot be empty")
        template_path = str(self.template_path or "").strip()
        if not template_path and not self.template_data:
            raise ValueError("template requires template_path or template_data")
        if not 0.0 <= float(self.threshold) <= 1.0:
            raise ValueError("template threshold must be within 0..1")
        scales = tuple(dict.fromkeys(float(scale) for scale in self.scales))
        if not scales or any(not math.isfinite(scale) or scale <= 0 for scale in scales):
            raise ValueError("template scales must contain positive finite values")
        object.__setattr__(self, "template_id", template_id)
        object.__setattr__(self, "template_path", template_path)
        object.__setattr__(self, "threshold", float(self.threshold))
        object.__setattr__(self, "scales", scales)


@dataclass(frozen=True)
class TemplateMatch:
    template_id: str
    matched: bool
    score: float
    threshold: float
    bounds: Optional[Bounds]
    region_bounds: Bounds
    scale: float
    elapsed_s: float
    frame_sha256: str = ""

    @property
    def center(self) -> Optional[tuple[int, int]]:
        return self.bounds.center if self.bounds is not None else None


class OpenCvTemplateMatcher:
    """Multi-scale normalized template matcher with a small template cache."""

    def __init__(self, *, cv2_module: object = None, numpy_module: object = None) -> None:
        self._cv2 = cv2_module
        self._numpy = numpy_module
        self._template_cache: dict[tuple[object, ...], object] = {}
        self._frame_cache: dict[tuple[str, bool], object] = {}

    def _modules(self) -> tuple[Any, Any]:
        if self._cv2 is None or self._numpy is None:
            try:
                import cv2  # type: ignore[import-not-found]
                import numpy  # type: ignore[import-not-found]
            except (ImportError, OSError) as exc:
                raise RuntimeError(
                    "OpenCV template matching requires the optional 'image' extra"
                ) from exc
            self._cv2 = cv2
            self._numpy = numpy
        return self._cv2, self._numpy

    @staticmethod
    def _artifact_bytes(artifact: Artifact) -> bytes:
        if artifact.data:
            return artifact.data
        path = Path(artifact.uri)
        if not path.is_file():
            raise RuntimeError(f"image artifact is not readable: {artifact.uri}")
        return path.read_bytes()

    def _decode(self, payload: bytes, *, grayscale: bool) -> object:
        cv2, numpy = self._modules()
        flags = cv2.IMREAD_GRAYSCALE if grayscale else cv2.IMREAD_COLOR
        image = cv2.imdecode(numpy.frombuffer(payload, dtype=numpy.uint8), flags)
        if image is None:
            raise RuntimeError("OpenCV could not decode the image payload")
        return image

    def _template_cache_key(self, spec: TemplateSpec) -> tuple[object, ...]:
        if spec.template_data:
            source: tuple[object, ...] = (
                "data",
                hashlib.sha256(spec.template_data).hexdigest(),
            )
        else:
            path = Path(spec.template_path).resolve()
            try:
                source = ("path", str(path), path.stat().st_mtime_ns, path.stat().st_size)
            except OSError as exc:
                raise RuntimeError(f"template is not readable: {path}") from exc
        return (*source, spec.grayscale)

    def _load_template(self, spec: TemplateSpec) -> object:
        key = self._template_cache_key(spec)
        cached = self._template_cache.get(key)
        if cached is not None:
            return cached
        payload = spec.template_data or Path(spec.template_path).read_bytes()
        template = self._decode(payload, grayscale=spec.grayscale)
        self._template_cache[key] = template
        return template

    def _load_frame(self, screen: ScreenFrame, *, grayscale: bool) -> object:
        payload = self._artifact_bytes(screen.artifact)
        digest = screen.artifact.sha256 or hashlib.sha256(payload).hexdigest()
        key = (digest, grayscale)
        cached = self._frame_cache.get(key)
        if cached is not None:
            return cached
        frame = self._decode(payload, grayscale=grayscale)
        self._frame_cache[key] = frame
        while len(self._frame_cache) > 4:
            self._frame_cache.pop(next(iter(self._frame_cache)))
        return frame

    def match(self, screen: ScreenFrame, spec: TemplateSpec) -> TemplateMatch:
        cv2, numpy = self._modules()
        started = time.perf_counter()
        frame = self._load_frame(screen, grayscale=spec.grayscale)
        template = self._load_template(spec)
        frame_height, frame_width = frame.shape[:2]
        if frame_width != screen.width or frame_height != screen.height:
            raise RuntimeError(
                "decoded screenshot dimensions do not match the ScreenFrame contract"
            )
        region_bounds = spec.region.pixel_bounds(screen.width, screen.height)
        crop = frame[
            region_bounds.top : region_bounds.bottom,
            region_bounds.left : region_bounds.right,
        ]
        crop_height, crop_width = crop.shape[:2]
        template_height, template_width = template.shape[:2]
        best_score = -1.0
        best_bounds: Optional[Bounds] = None
        best_scale = 1.0

        for scale in spec.scales:
            scaled_width = max(1, int(round(template_width * scale)))
            scaled_height = max(1, int(round(template_height * scale)))
            if scaled_width > crop_width or scaled_height > crop_height:
                continue
            if scaled_width == template_width and scaled_height == template_height:
                candidate = template
            else:
                interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
                candidate = cv2.resize(
                    template,
                    (scaled_width, scaled_height),
                    interpolation=interpolation,
                )
            if float(numpy.std(candidate)) < 1e-6:
                result = cv2.matchTemplate(crop, candidate, cv2.TM_SQDIFF_NORMED)
                minimum, _maximum, minimum_location, _maximum_location = cv2.minMaxLoc(result)
                score = 1.0 - float(minimum)
                best_location = minimum_location
            else:
                result = cv2.matchTemplate(crop, candidate, cv2.TM_CCOEFF_NORMED)
                _minimum, maximum, _minimum_location, maximum_location = cv2.minMaxLoc(result)
                score = float(maximum)
                best_location = maximum_location
            if not math.isfinite(score) or score <= best_score:
                continue
            left = region_bounds.left + int(best_location[0])
            top = region_bounds.top + int(best_location[1])
            best_score = score
            best_scale = scale
            best_bounds = Bounds(left, top, left + scaled_width, top + scaled_height)

        if best_bounds is None:
            raise RuntimeError("template is larger than every configured search region")
        return TemplateMatch(
            template_id=spec.template_id,
            matched=best_score >= spec.threshold,
            score=best_score,
            threshold=spec.threshold,
            bounds=best_bounds,
            region_bounds=region_bounds,
            scale=best_scale,
            elapsed_s=time.perf_counter() - started,
            frame_sha256=screen.artifact.sha256,
        )

    def annotated_artifact(
        self,
        screen: ScreenFrame,
        match: TemplateMatch,
        *,
        artifact_id: str,
    ) -> Artifact:
        cv2, _numpy = self._modules()
        frame = self._load_frame(screen, grayscale=False)
        color = (40, 200, 40) if match.matched else (40, 40, 220)
        if match.bounds is not None:
            cv2.rectangle(
                frame,
                (match.bounds.left, match.bounds.top),
                (match.bounds.right, match.bounds.bottom),
                color,
                3,
            )
            label = f"{match.template_id} {match.score:.4f}"
            cv2.putText(
                frame,
                label,
                (match.bounds.left, max(24, match.bounds.top - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                color,
                2,
                cv2.LINE_AA,
            )
        encoded, payload = cv2.imencode(".png", frame)
        if not encoded:
            raise RuntimeError("OpenCV could not encode template-match evidence")
        return Artifact(
            artifact_id=artifact_id,
            media_type="image/png",
            data=payload.tobytes(),
            metadata={
                "template_id": match.template_id,
                "matched": match.matched,
                "score": match.score,
                "threshold": match.threshold,
            },
        )


class TemplateMatchVerifier:
    """Deterministic verifier backed by named :class:`TemplateSpec` objects."""

    def __init__(
        self,
        specs: Mapping[str, TemplateSpec],
        *,
        matcher: Optional[OpenCvTemplateMatcher] = None,
        evidence_sink: Optional[EvidenceSink] = None,
        name: str = "template_match",
    ) -> None:
        self._specs = dict(specs)
        if not self._specs:
            raise ValueError("template verifier requires at least one template")
        for key, spec in self._specs.items():
            if key != spec.template_id:
                raise ValueError("template mapping keys must match template_id")
        self._matcher = matcher or OpenCvTemplateMatcher()
        self._evidence_sink = evidence_sink
        self._descriptor = ComponentDescriptor(
            name=name,
            version="1",
            description="OpenCV normalized multi-scale template verifier",
            capabilities=frozenset({"screenshot", "template_matching", "evidence_overlay"}),
        )

    @property
    def descriptor(self) -> ComponentDescriptor:
        return self._descriptor

    @staticmethod
    def _expected(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return True
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes", "present"}:
            return True
        if normalized in {"false", "0", "no", "absent"}:
            return False
        raise ValueError("expected must be a boolean or present/absent")

    def verify(
        self,
        request: VerificationRequest,
        observation: Observation,
    ) -> VerificationResult:
        if observation.screen is None:
            return VerificationResult(
                request.request_id,
                request.name,
                VerificationStatus.INCONCLUSIVE,
                "template verification requires a screenshot observation",
            )
        template_id = str(request.arguments.get("template_id") or "").strip()
        spec = self._specs.get(template_id)
        if spec is None:
            return VerificationResult(
                request.request_id,
                request.name,
                VerificationStatus.ERROR,
                f"unknown template: {template_id or '<empty>'}",
            )
        try:
            expected = self._expected(request.arguments.get("expected"))
            match = self._matcher.match(observation.screen, spec)
            evidence_ids: tuple[str, ...] = ()
            evidence_error = ""
            if self._evidence_sink is not None:
                try:
                    artifact = self._matcher.annotated_artifact(
                        observation.screen,
                        match,
                        artifact_id=f"{request.request_id}-overlay",
                    )
                    evidence_ids = (self._evidence_sink.store(artifact),)
                except Exception as exc:  # evidence failure must remain observable
                    evidence_error = str(exc)
            passed = match.matched is expected
            status = VerificationStatus.PASSED if passed else VerificationStatus.FAILED
            bounds = (
                [
                    match.bounds.left,
                    match.bounds.top,
                    match.bounds.right,
                    match.bounds.bottom,
                ]
                if match.bounds is not None
                else None
            )
            return VerificationResult(
                request.request_id,
                request.name,
                status,
                (
                    f"template {template_id} score={match.score:.4f} "
                    f"threshold={match.threshold:.4f} expected={'present' if expected else 'absent'}"
                ),
                evidence_ids=evidence_ids,
                metadata={
                    "template_id": template_id,
                    "matched": match.matched,
                    "expected": expected,
                    "score": match.score,
                    "threshold": match.threshold,
                    "scale": match.scale,
                    "bounds": bounds,
                    "elapsed_s": match.elapsed_s,
                    "frame_sha256": match.frame_sha256,
                    "evidence_error": evidence_error,
                },
            )
        except Exception as exc:
            return VerificationResult(
                request.request_id,
                request.name,
                VerificationStatus.ERROR,
                str(exc),
            )
