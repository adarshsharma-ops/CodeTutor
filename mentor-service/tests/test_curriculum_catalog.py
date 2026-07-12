import json

import pytest

from mentor.curriculum_catalog import catalog_by_id, discover_catalogs, load_catalog, validate_catalog


def test_python_foundations_manifest_loads():
    catalog = load_catalog()
    assert catalog.raw["id"] == "python-foundations"
    assert len(catalog.modules) >= 5


def test_prerequisite_progression_is_ordered():
    catalog = load_catalog()
    assert catalog.next_module(set())["id"] == "values-and-variables"
    assert catalog.next_module({"values-and-variables"})["id"] == "decisions"


def test_each_module_contains_teaching_contract():
    for module in load_catalog().modules:
        assert module["mental_model"]
        assert module["evidence"]
        assert module["common_mistakes"]
        assert module["understanding_checks"]
        assert module["projects"]


def test_duplicate_module_ids_are_rejected():
    raw = load_catalog().raw
    duplicate = json.loads(json.dumps(raw))
    duplicate["modules"].append(dict(duplicate["modules"][0]))
    with pytest.raises(ValueError, match="unique"):
        validate_catalog(duplicate)


def test_unknown_concept_is_rejected():
    raw = json.loads(json.dumps(load_catalog().raw))
    raw["modules"][0]["concepts"].append("telepathy")
    with pytest.raises(ValueError, match="unknown concepts"):
        validate_catalog(raw)


def test_discovers_foundations_and_data_pathways():
    ids = {catalog.raw["id"] for catalog in discover_catalogs()}
    assert {"python-foundations", "python-for-data"} <= ids


def test_python_for_data_has_ordered_modules():
    catalog = catalog_by_id("python-for-data")
    assert catalog.next_module(set())["id"] == "tabular-mental-model"
    assert catalog.module("clean-and-validate")["common_mistakes"]


def test_discovers_ml_foundations_pathway():
    catalog = catalog_by_id("ml-foundations")
    assert catalog.next_module(set())["id"] == "frame-the-problem"
    assert catalog.module("split-without-leakage")["understanding_checks"]
