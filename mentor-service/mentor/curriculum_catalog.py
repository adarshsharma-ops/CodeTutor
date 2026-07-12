"""Load and validate versioned, data-driven learning pathways."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .analyzer import CONCEPT_LABELS


@dataclass(frozen=True)
class CurriculumCatalog:
    raw: dict[str, Any]

    @property
    def modules(self) -> list[dict[str, Any]]:
        return self.raw["modules"]

    def module(self, module_id: str) -> dict[str, Any]:
        for module in self.modules:
            if module["id"] == module_id:
                return module
        raise KeyError(module_id)

    def next_module(self, completed: set[str]) -> dict[str, Any]:
        for module in self.modules:
            if module["id"] not in completed and set(module["prerequisites"]) <= completed:
                return module
        return self.modules[-1]


def default_manifest_path() -> Path:
    return Path(__file__).resolve().parent.parent / "curricula" / "python-foundations" / "v1" / "manifest.json"


def curricula_root() -> Path:
    return Path(__file__).resolve().parent.parent / "curricula"


def discover_catalogs() -> list[CurriculumCatalog]:
    return [load_catalog(path) for path in sorted(curricula_root().glob("*/v*/manifest.json"))]


def catalog_by_id(catalog_id: str) -> CurriculumCatalog:
    matches = [catalog for catalog in discover_catalogs() if catalog.raw["id"] == catalog_id]
    if not matches:
        raise KeyError(catalog_id)
    return matches[-1]


def load_catalog(path: str | Path | None = None) -> CurriculumCatalog:
    manifest = Path(path) if path else default_manifest_path()
    with manifest.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    validate_catalog(raw)
    return CurriculumCatalog(raw)


def validate_catalog(raw: dict[str, Any]) -> None:
    for key in ("id", "version", "title", "modules"):
        if not raw.get(key):
            raise ValueError(f"curriculum missing {key}")
    modules = raw["modules"]
    ids = [m.get("id") for m in modules]
    if len(ids) != len(set(ids)):
        raise ValueError("curriculum module ids must be unique")
    known = set(ids)
    required = {"id", "title", "prerequisites", "concepts", "mental_model", "evidence",
                "common_mistakes", "understanding_checks", "projects"}
    for module in modules:
        missing = required - set(module)
        if missing:
            raise ValueError(f"module {module.get('id')} missing: {sorted(missing)}")
        unknown_prereqs = set(module["prerequisites"]) - known
        if unknown_prereqs:
            raise ValueError(f"module {module['id']} has unknown prerequisites: {sorted(unknown_prereqs)}")
        unknown_concepts = set(module["concepts"]) - set(CONCEPT_LABELS)
        if unknown_concepts:
            raise ValueError(f"module {module['id']} has unknown concepts: {sorted(unknown_concepts)}")
        for field in ("mental_model", "evidence", "common_mistakes", "understanding_checks", "projects"):
            if not module[field]:
                raise ValueError(f"module {module['id']} has empty {field}")
