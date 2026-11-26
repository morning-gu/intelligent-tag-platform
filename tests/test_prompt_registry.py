"""GET /api/v1/prompts/registry 契约与完备性守门测试。"""
from tagplatform.capabilities.prompts import mtg as mtg_prompts
from tagplatform.capabilities.prompts import (
    assets as assets_prompts,
    open_review as open_review_prompts,
    registry as registry_mod,
    templates as templates_prompts,
)


def _public_str_constants(module) -> set[str]:
    """模块级公开 str 常量名集合（dunder/下划线开头排除）。"""
    return {name for name, value in vars(module).items()
            if not name.startswith("_") and isinstance(value, str)}


def test_registry_covers_all_central_constants():
    """完备性钉死（终审 Important #1）：registry 各组条目名集合必须与
    对应常量模块的公开 str 常量集合精确相等——未来加/改名常量漏同步
    registry 时在此红（"唯一全量清单"承诺的守门测试）。"""
    expected = {
        "closed": _public_str_constants(templates_prompts),
        "mtg": _public_str_constants(mtg_prompts),
        "assets": _public_str_constants(assets_prompts),
        "open_review": _public_str_constants(open_review_prompts),
    }
    actual = {g["module"]: {i["name"] for i in g["items"]}
              for g in registry_mod.list_prompt_registry()}
    assert set(actual) == set(expected)
    for module in expected:
        assert actual[module] == expected[module], (
            f"registry '{module}' 组与常量模块不同步："
            f"缺 {sorted(expected[module] - actual[module])} /"
            f" 多 {sorted(actual[module] - expected[module])}")


def test_registry_endpoint_contract(client):
    resp = client.get("/api/v1/prompts/registry")
    assert resp.status_code == 200
    groups = resp.json()
    assert {g["module"] for g in groups} == {
        "closed", "mtg", "assets", "open_review"}
    for group in groups:
        assert group["title"]
        assert group["items"]
        for item in group["items"]:
            assert item["name"]
            assert item["text"].strip()
