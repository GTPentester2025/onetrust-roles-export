"""Tests for the pure logic in onetrust_assessment_export (no browser, no Playwright needed).

Run: python test_onetrust_assessment_export.py
"""
import json
import os
import tempfile

import onetrust_assessment_export as mod


def test_describe_shape():
    assert mod.describe_shape([1, 2, 3]) == "list[3]"
    assert mod.describe_shape({"content": [1, 2], "page": {"a": 1}, "total": 2}) == \
        "dict{content: list[2], page: dict{1}, total: int}"
    assert mod.describe_shape("x") == "str"
    assert mod.describe_shape(None) == "NoneType"


def test_extract_templates_bare_array():
    assert mod.extract_templates([{"id": "1"}]) == [{"id": "1"}]


def test_extract_templates_common_wrappers():
    for key in ("content", "data", "templates", "results", "items", "details"):
        assert mod.extract_templates({key: [{"id": "x"}]}) == [{"id": "x"}]


def test_extract_templates_nested():
    assert mod.extract_templates({"data": {"content": [{"id": "n"}]}}) == [{"id": "n"}]


def test_extract_templates_unknown():
    assert mod.extract_templates({"oops": 1}) is None
    assert mod.extract_templates("nope") is None


def test_ci_get_case_insensitive_and_skips_empty():
    rec = {"TemplateId": "abc", "name": "", "Title": "Privacy"}
    assert mod.ci_get(rec, "templateid") == "abc"
    assert mod.ci_get(rec, "name", "title") == "Privacy"   # empty name skipped, falls through to Title
    assert mod.ci_get(rec, "missing") is None


def test_template_id_and_name_variants():
    assert mod.template_id({"templateId": "t1"}) == "t1"
    assert mod.template_id({"id": "t2"}) == "t2"
    assert mod.template_name({"templateName": "PIA Base"}) == "PIA Base"
    assert mod.template_name({"nope": 1}) == "<unnamed>"


def test_root_version_and_number_prefer_first_source():
    summary = {"rootVersionId": "rv-sum"}
    details = {"rootVersionId": "rv-det", "version": 44}
    assert mod.root_version_id(summary, details) == "rv-sum"     # summary wins when present
    assert mod.root_version_id({}, details) == "rv-det"          # falls back to details
    assert mod.version_number({}, details) == 44


def test_version_number_zero_is_valid():
    # 0 is a legitimate version and must not be treated as "missing"
    assert mod.version_number({"version": 0}) == 0


def test_load_and_save_combined_roundtrip_in_order():
    order = ["b", "a", "c"]
    results = {"a": {"id": "a", "name": "A"}, "b": {"id": "b", "name": "B"}}  # c not fetched
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "out.json")
        mod.save_combined(path, order, results)
        with open(path, encoding="utf-8") as f:
            on_disk = json.load(f)
        assert [r["id"] for r in on_disk] == ["b", "a"]   # discovery order, only fetched ones
        assert mod.load_previous(path) == results


def test_write_per_template_makes_unique_safe_files():
    records = [
        {"id": "1", "name": "PIA Base"},
        {"id": "2", "name": "PIA Base"},      # duplicate name -> id appended
        {"id": "3", "name": "EU/DPIA"},       # slash sanitized
    ]
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "assessment_templates")
        count = mod.write_per_template(out, records)
        assert count == 3
        assert sorted(os.listdir(out)) == ["EU_DPIA.json", "PIA Base-2.json", "PIA Base.json"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
