from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

from app.core.config import settings


_SUFFIX_RE = re.compile(r"^(?P<base>.+?)-(?P<num>\d+)$")


def pet_registry_path() -> Path:
    return Path(settings.reid_storage_dir) / "registry" / "pets.json"


def read_pet_name_map() -> Dict[str, str]:
    path = pet_registry_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in data.items():
        pet_id = str(key or "").strip()
        pet_name = str(value or "").strip()
        if pet_id and pet_name:
            out[pet_id] = pet_name
    return out


def write_pet_name_map(mapping: Dict[str, str]) -> None:
    path = pet_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = {str(k).strip(): str(v).strip() for k, v in mapping.items() if str(k).strip() and str(v).strip()}
    path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_pet_mapping(pet_id: str, pet_name: Optional[str] = None) -> str:
    pet_id = str(pet_id or "").strip()
    pet_name_clean = str(pet_name or pet_id).strip()
    if not pet_id:
        raise ValueError("pet_id is required")
    if not pet_name_clean:
        raise ValueError("pet_name is required")
    mapping = read_pet_name_map()
    current = mapping.get(pet_id)
    if current != pet_name_clean:
        mapping[pet_id] = pet_name_clean
        write_pet_name_map(mapping)
    return pet_id


def get_pet_name(pet_id: str) -> Optional[str]:
    pet_id_clean = str(pet_id or "").strip()
    if not pet_id_clean:
        return None
    return read_pet_name_map().get(pet_id_clean)


def find_pet_ids_by_name(pet_name: str) -> List[str]:
    target = str(pet_name or "").strip()
    if not target:
        return []
    mapping = read_pet_name_map()
    seen = set()
    matched: List[str] = []
    for pet_id, display_name in mapping.items():
        if pet_id == target or display_name == target:
            if pet_id in seen:
                continue
            seen.add(pet_id)
            matched.append(pet_id)
    return matched


def allocate_pet_id(pet_name: str) -> str:
    base = str(pet_name or "").strip()
    if not base:
        raise ValueError("pet_name is required")
    mapping = read_pet_name_map()
    if base not in mapping:
        mapping[base] = base
        write_pet_name_map(mapping)
        return base

    max_suffix = 1
    for existing in mapping.keys():
        if existing == base:
            max_suffix = max(max_suffix, 1)
            continue
        match = _SUFFIX_RE.match(existing)
        if not match:
            continue
        if match.group("base") != base:
            continue
        try:
            max_suffix = max(max_suffix, int(match.group("num")))
        except Exception:
            continue

    next_pet_id = f"{base}-{max_suffix + 1}"
    mapping[next_pet_id] = base
    write_pet_name_map(mapping)
    return next_pet_id
