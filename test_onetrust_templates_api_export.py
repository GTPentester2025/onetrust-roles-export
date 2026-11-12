"""Tests for onetrust_templates_api_export pure helpers. No network.

Run: python test_onetrust_templates_api_export.py
"""
import onetrust_templates_api_export as ex


def test_template_ref_prefers_root_version_id():
    assert ex.template_ref({"rootVersionId": "RV1", "id": "X"}) == "RV1"
    assert ex.template_ref({"templateRootVersionId": "RV2"}) == "RV2"
    assert ex.template_ref({"id": "ONLY"}) == "ONLY"
    assert ex.template_ref({}) is None


def test_name_and_type():
    assert ex.template_name({"name": "Vendor PIA"}) == "Vendor PIA"
    assert ex.template_name({}) == "<unnamed>"
    assert ex.template_type({"templateType": "PIA"}) == "PIA"
    assert ex.template_type({"type": "PIA"}) == "PIA"


def test_list_params_passes_types_array():
    assert ex.list_params(["PIA"]) == {"template-types": ["PIA"]}
    assert ex.list_params(["PIA", "VENDOR"]) == {"template-types": ["PIA", "VENDOR"]}


def test_export_params_version():
    assert ex.export_params("latest") == {"templateVersion": "latest"}
    assert ex.export_params(None) == {}


def test_dedupe_by_ref_keeps_first_seen_order():
    items = [{"rootVersionId": "A"}, {"rootVersionId": "B"}, {"rootVersionId": "A"}, {"name": "no-ref"}]
    templates, order = ex.dedupe_by_ref(items)
    assert order == ["A", "B"]
    assert len(templates) == 2


def test_build_record_shape():
    rec = ex.build_record({"rootVersionId": "A", "name": "Vendor PIA", "templateType": "PIA"},
                          {"questions": []})
    assert rec["rootVersionId"] == "A"
    assert rec["name"] == "Vendor PIA"
    assert rec["templateType"] == "PIA"
    assert rec["export"] == {"questions": []}
    assert rec["summary"]["name"] == "Vendor PIA"


def test_extract_templates_handles_bare_array():
    # the official list returns a bare JSON array
    data = [{"rootVersionId": "A"}, {"rootVersionId": "B"}]
    assert ex.extract_templates(data) == data


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
