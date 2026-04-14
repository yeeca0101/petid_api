"""인식 시도(trial) 결과와 이미지를 저장하는 엔드포인트 모듈."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from app.utils.timezone import business_tz

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.schemas.trials import TrialUploadResponse

router = APIRouter()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(ts: Optional[str]) -> datetime:
    if not ts:
        return _utcnow()
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=business_tz())
        return parsed
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid timestamp: {ts}") from e


def _safe_ext(filename: Optional[str]) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return ext
    return ".jpg"


def _safe_folder_name(name: Optional[str]) -> str:
    if not name:
        return "unknown"
    safe = name.strip().replace("/", "_")
    if os.altsep:
        safe = safe.replace(os.altsep, "_")
    return safe or "unknown"


def _trial_dir(ts: datetime) -> Path:
    date_str = ts.date().isoformat()
    return Path(settings.verification_storage_dir) / "trials" / date_str


def _trial_paths(trial_id: str, ts: datetime) -> tuple[Path, Path]:
    base = _trial_dir(ts)
    return base / f"{trial_id}.json", base / f"{trial_id}.jpg"


def _pet_trial_paths(trial_id: str, ts: datetime, pet_id: str, pet_name: str) -> tuple[Path, Path]:
    date_str = ts.date().isoformat()
    base = (
        Path(settings.verification_storage_dir)
        / "pets"
        / pet_id
        / _safe_folder_name(pet_name)
        / "trials"
        / date_str
    )
    return base / f"{trial_id}.json", base / f"{trial_id}.jpg"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _compute_outcome(is_success: bool, user_feedback: bool) -> str:
    if is_success and user_feedback:
        return "TP"
    if is_success and not user_feedback:
        return "FP"
    if not is_success and user_feedback:
        return "FN"
    return "TN"


@router.post("/trials", response_model=TrialUploadResponse)
async def upload_trial(
    trial_id: str = Form(..., alias="id"),
    pet_id: str = Form(..., alias="petId"),
    pet_name: Optional[str] = Form(default=None, alias="petName"),
    facebank_id: str = Form(..., alias="facebankId"),
    facebank_version: int = Form(..., alias="facebankVersion"),
    score: float = Form(...),
    threshold: Optional[float] = Form(default=None),
    sharpness: Optional[float] = Form(default=None),
    is_success: bool = Form(..., alias="isSuccess"),
    user_feedback: bool = Form(..., alias="userFeedback"),
    timestamp: Optional[str] = Form(default=None),
    pose: Optional[str] = Form(default=None),
    trial_image: UploadFile = File(..., alias="trialImage"),
):
    ts = _parse_timestamp(timestamp)
    if pet_name:
        meta_path, img_path = _pet_trial_paths(trial_id, ts, pet_id, pet_name)
    else:
        meta_path, img_path = _trial_paths(trial_id, ts)

    if meta_path.exists():
        return TrialUploadResponse(
            trial_id=trial_id,
            status="duplicate",
            stored=False,
            storage_path=str(meta_path),
        )

    data = await trial_image.read()
    if len(data) > settings.max_image_bytes:
        raise HTTPException(status_code=413, detail=f"Image too large: {len(data)} bytes")

    img_ext = _safe_ext(trial_image.filename)
    img_path = img_path.with_suffix(img_ext)
    img_path.parent.mkdir(parents=True, exist_ok=True)

    await run_in_threadpool(img_path.write_bytes, data)

    outcome = _compute_outcome(is_success, user_feedback)
    payload = {
        "trial_id": trial_id,
        "pet_id": pet_id,
        "pet_name": pet_name,
        "facebank_id": facebank_id,
        "facebank_version": int(facebank_version),
        "score": float(score),
        "threshold": float(threshold) if threshold is not None else None,
        "sharpness": float(sharpness) if sharpness is not None else None,
        "is_success": bool(is_success),
        "user_feedback": bool(user_feedback),
        "outcome": outcome,
        "timestamp": ts.isoformat(),
        "pose": pose,
        "image_path": str(img_path),
    }

    await run_in_threadpool(_write_json, meta_path, payload)

    return TrialUploadResponse(
        trial_id=trial_id,
        status="stored",
        stored=True,
        storage_path=str(meta_path),
        outcome=outcome,
    )
