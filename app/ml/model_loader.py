from __future__ import annotations

from pathlib import Path

import logging

import torch

logger = logging.getLogger(__name__)


def _build_local_source_candidates(source: str, cache_dir: Path) -> list[Path]:
    """Build candidate local paths for a model source string.

    Priority:
      1) As-is path (absolute or cwd-relative)
      2) cache_dir / source (for short relative forms)
      3) cache_dir / models--org--repo (when source is org/repo)
    """

    candidates: list[Path] = []
    raw = Path(source).expanduser()
    candidates.append(raw)

    if not raw.is_absolute():
        candidates.append(cache_dir / raw)

    if "/" in source and not source.startswith("./") and not source.startswith("../"):
        mapped = "models--" + source.replace("/", "--")
        candidates.append(cache_dir / mapped)

    # de-duplicate while preserving order
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in candidates:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def _resolve_local_source_if_exists(source: str, cache_dir: Path) -> Path | None:
    for p in _build_local_source_candidates(source, cache_dir):
        if p.exists():
            return p
    return None


def _resolve_hf_local_source(source: str) -> str:
    """Resolve a HuggingFace local cache path to a loadable snapshot directory.

    Handles inputs like:
      /.../.cache/hf/models--org--repo
    by mapping to:
      /.../.cache/hf/models--org--repo/snapshots/<revision>
    """

    p = Path(source)
    if not p.exists() or not p.is_dir():
        return source

    # Already a concrete model directory.
    if (p / "config.json").exists():
        return str(p)

    snapshots_dir = p / "snapshots"
    refs_main = p / "refs" / "main"
    if not snapshots_dir.exists() or not snapshots_dir.is_dir():
        return source

    # Prefer the revision pointed by refs/main if available.
    if refs_main.exists():
        rev = refs_main.read_text(encoding="utf-8").strip()
        if rev:
            candidate = snapshots_dir / rev
            if (candidate / "config.json").exists():
                return str(candidate)

    # Fallback: choose the newest snapshot that has config.json.
    candidates = [d for d in snapshots_dir.iterdir() if d.is_dir() and (d / "config.json").exists()]
    if not candidates:
        return source
    candidates.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return str(candidates[0])


def load_embedding_model(
    model_name: str,
    cache_dir: Path,
    miewid_model_source: str = "conservationxlabs/miewid-msv3",
    miewid_load_pretrained: bool = True,
    miewid_local_files_only: bool = True,
    miewid_require_local_source: bool = False,
) -> torch.nn.Module:
    """Load an embedding model.

    Supported:
      - miewid (conservationxlabs/miewid-msv3)
      - mega-t, mega-l, mega-l-224 (BVRA MegaDescriptor)
      - clip, dinov2 (BVRA MegaDescriptor variants)

    Note: for PoC we rely on hf-hub downloads via timm/transformers caches.
    """

    name = model_name.lower().strip()
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    if name == "miewid":
        from transformers import AutoConfig, AutoModel

        local_source_path = _resolve_local_source_if_exists(miewid_model_source, cache_dir)

        if miewid_require_local_source:
            if local_source_path is None:
                checked = ", ".join(str(p) for p in _build_local_source_candidates(miewid_model_source, cache_dir))
                raise FileNotFoundError(
                    f"miewid local source not found: {miewid_model_source} "
                    f"(checked: {checked}) "
                    "(set *_MIEWID_MODEL_SOURCE to a local cache/snapshot path)"
                )

        source_for_load = str(local_source_path) if local_source_path is not None else miewid_model_source
        resolved_source = _resolve_hf_local_source(source_for_load)
        logger.info(
            "Loading miewid model from source=%s (cached at %s, pretrained=%s, local_only=%s)",
            resolved_source,
            cache_dir,
            miewid_load_pretrained,
            miewid_local_files_only,
        )
        # trust_remote_code=True is needed by the model repo.
        if miewid_load_pretrained:
            model = AutoModel.from_pretrained(
                resolved_source,
                trust_remote_code=True,
                cache_dir=str(cache_dir),
                local_files_only=miewid_local_files_only,
            )
        else:
            cfg = AutoConfig.from_pretrained(
                resolved_source,
                trust_remote_code=True,
                cache_dir=str(cache_dir),
                local_files_only=miewid_local_files_only,
            )
            model = AutoModel.from_config(cfg, trust_remote_code=True)
        return model

    # timm models
    import timm

    timm_name_map = {
        "mega-t": "hf-hub:BVRA/MegaDescriptor-T-224",
        "mega-l": "hf-hub:BVRA/MegaDescriptor-L-384",
        "mega-l-224": "hf-hub:BVRA/MegaDescriptor-L-224",
        "clip": "hf-hub:BVRA/MegaDescriptor-CLIP-336",
        "dinov2": "hf-hub:BVRA/MegaDescriptor-DINOv2-518",
    }

    if name not in timm_name_map:
        raise ValueError(f"Unsupported model_name: {model_name}")

    timm_id = timm_name_map[name]
    logger.info("Loading timm model %s (cached at %s)", timm_id, cache_dir)

    # Most of these provide pretrained weights via hf-hub.
    model = timm.create_model(timm_id, pretrained=True, cache_dir=str(cache_dir))
    return model
