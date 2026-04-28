from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import numpy as np
import torch
from PIL import Image

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DetectedInstance:
    class_id: int
    confidence: float
    # normalized bbox in [0,1]
    x1: float
    y1: float
    x2: float
    y2: float
    # optional mask (not used in this PoC; reserved)
    mask: Optional[np.ndarray] = None


class YoloDetector:
    """Ultralytics YOLO wrapper.

    Notes:
      - Designed for YOLO26x weights, but works with any Ultralytics YOLO .pt.
      - Returns normalized bboxes.
      - For this PoC, masks are not encoded into API responses.
    """

    def __init__(
        self,
        weights_path: Path,
        device: str,
        imgsz: int = 960,
        conf: float = 0.25,
        iou: float = 0.45,
        keep_class_ids: Optional[Iterable[int]] = None,
        task: str = "detect",
    ):
        self.weights_path = Path(weights_path)
        self.device = self._resolve_device(device)
        self.imgsz = int(imgsz)
        self.conf = float(conf)
        self.iou = float(iou)
        self.keep_class_ids = list(keep_class_ids) if keep_class_ids is not None else None
        self.task = task

        if not self.weights_path.exists():
            raise FileNotFoundError(
                f"YOLO weights not found: {self.weights_path}. "
                "Mount/copy your yolo26x(.pt) weights into ./weights/yolo/"
            )

        from ultralytics import YOLO

        logger.info("Loading YOLO detector | weights=%s | device=%s", self.weights_path, self.device)
        self.model = YOLO(str(self.weights_path), task=self.task)

    @staticmethod
    def _resolve_device(device_str: str) -> str:
        ds = str(device_str).strip().lower()
        if ds.startswith("cuda"):
            if torch.cuda.is_available():
                return device_str
            logger.warning("CUDA requested for detector but not available. Falling back to CPU.")
            return "cpu"
        return device_str

    def detect(self, image: Image.Image) -> List[DetectedInstance]:
        return self.detect_batch([image])[0]

    def detect_batch(self, images: List[Image.Image]) -> List[List[DetectedInstance]]:
        if not images:
            return []

        rgb_images = [image.convert("RGB") if image.mode != "RGB" else image for image in images]
        sizes = [image.size for image in rgb_images]
        sources = [np.asarray(image) for image in rgb_images]
        results = self.model.predict(
            source=sources,
            imgsz=self.imgsz,
            conf=self.conf,
            iou=self.iou,
            device=self.device,
            classes=self.keep_class_ids,
            verbose=False,
        )
        if len(results) != len(rgb_images):
            raise RuntimeError(f"Detector returned {len(results)} results for {len(rgb_images)} images")

        return [self._detections_from_result(result, width=w, height=h) for result, (w, h) in zip(results, sizes)]

    @staticmethod
    def _detections_from_result(result, *, width: int, height: int) -> List[DetectedInstance]:
        if result.boxes is None or len(result.boxes) == 0:
            return []

        boxes = result.boxes
        xyxy = boxes.xyxy.cpu().numpy()  # (N,4)
        confs = boxes.conf.cpu().numpy().astype(float)
        clss = boxes.cls.cpu().numpy().astype(int)

        out: List[DetectedInstance] = []
        for (x1, y1, x2, y2), c, cid in zip(xyxy, confs, clss):
            nx1 = float(max(0.0, min(1.0, x1 / width)))
            ny1 = float(max(0.0, min(1.0, y1 / height)))
            nx2 = float(max(0.0, min(1.0, x2 / width)))
            ny2 = float(max(0.0, min(1.0, y2 / height)))

            if nx2 <= nx1 or ny2 <= ny1:
                continue

            out.append(
                DetectedInstance(
                    class_id=int(cid),
                    confidence=float(c),
                    x1=nx1,
                    y1=ny1,
                    x2=nx2,
                    y2=ny2,
                    mask=None,
                )
            )
        return out
