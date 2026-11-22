"""GET /api/v1/prompts/registry 契约 + assets/quality 搬运 wiring。"""
from tagplatform.capabilities.prompts import mtg as mtg_prompts
from tagplatform.capabilities.prompts.assets import (
    SCENE_ANALYSIS_OUTPUT_TEXT, SCENE_ANALYSIS_ROLE_TEXT,
    SUMMARY_OUTPUT_TEXT, SUMMARY_REQUIREMENTS_TEXT, SUMMARY_TASK_TEXT,
)
from tagplatform.capabilities.prompts.quality import PROMPT_VERSION, RUBRIC_TEXT
from tagplatform.services.content.assets.builder import (
    ScenePlan, scene_messages, summary_messages,
)
from tagplatform.services.eval.tag_quality import build_quality_template

# client 为 tests/conftest.py 的 pytest fixture（真 app + 临时库 + mock
# LLM/嵌入器），测试以函数参数注入：def test_x(client): ...


def test_registry_endpoint_contract(client):
    resp = client.get("/api/v1/prompts/registry")
    assert resp.status_code == 200
    groups = resp.json()
    assert {g["module"] for g in groups} == {"closed", "mtg", "assets", "quality"}
    for group in groups:
        assert group["title"]
        assert group["items"]
        for item in group["items"]:
            assert item["name"]
            assert item["text"].strip()


def test_registry_closed_and_quality_content(client):
    groups = {g["module"]: g for g in client.get("/api/v1/prompts/registry").json()}
    closed_texts = [i["text"] for i in groups["closed"]["items"]]
    assert any("候选标签按目录分组" in t for t in closed_texts)      # CLOSED_TEMPLATE_TEXT 特征串
    quality_items = {i["name"]: i["text"] for i in groups["quality"]["items"]}
    assert quality_items["PROMPT_VERSION"] == "tag-quality-v4"
    assert "## 标签质量评估" in quality_items["RUBRIC_TEXT"]


def test_registry_mtg_items_match_constants(client):
    groups = {g["module"]: g for g in client.get("/api/v1/prompts/registry").json()}
    by_name = {i["name"]: i["text"] for i in groups["mtg"]["items"]}
    assert by_name["JUDGE_CRITERIA"] == mtg_prompts.JUDGE_CRITERIA  # 单一来源钉死
    assert by_name["SCENE_GOAL_TEXT"] == mtg_prompts.SCENE_GOAL_TEXT


def test_registry_does_not_break_prompt_contract_locks(client):
    assert client.post("/api/v1/prompts").status_code == 405


def test_summary_messages_use_central_copy():
    text = summary_messages("大都会", "1927", ["剧情"], "A romance...")[0]["content"]
    assert SUMMARY_TASK_TEXT in text
    assert SUMMARY_REQUIREMENTS_TEXT in text
    assert SUMMARY_OUTPUT_TEXT in text


def test_scene_messages_use_central_copy():
    plan = ScenePlan(scene_id="scene_1", start_ms=0, end_ms=1000,
                     source_description="A scene", frames=[], dialogues=[])
    content = scene_messages("大都会", "1927", ["剧情"], ["Cast"], plan, [])[0]["content"]
    # content 为多模态 parts 列表（text part + image parts）；无帧时仅一个 text part
    text = content[0]["text"] if isinstance(content, list) else content
    assert SCENE_ANALYSIS_ROLE_TEXT in text
    assert SCENE_ANALYSIS_OUTPUT_TEXT in text


def test_quality_template_uses_central_copy():
    content = build_quality_template()["messages"][0]["content"]
    assert RUBRIC_TEXT in content
