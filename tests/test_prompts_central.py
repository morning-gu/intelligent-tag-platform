"""Prompt 完整模板（capabilities/prompts）的 wiring 与分层红线测试。"""
import re
from pathlib import Path

import pytest

from tagplatform.capabilities.prompts.mtg import (
    DESCRIPTION_RETRY_HINT, DESCRIPTION_TEMPLATE,
    FILM_GENERATION_TEMPLATE, JUDGE_RETRY_HINT, JUDGE_TEMPLATE,
    SCENE_GENERATION_TEMPLATE, VERIFY_RETRY_HINT, VERIFY_TEMPLATE,
)
from tagplatform.capabilities.prompts.templates import CLOSED_RETRY_HINT
from tagplatform.services.generation.open.mtg.descriptions import (
    build_description_template,
)
from tagplatform.services.generation.open.mtg.judge import build_judge_template
from tagplatform.services.generation.open.mtg.prompts import (
    build_generation_template, render_judge_messages,
)
from tagplatform.services.generation.open.mtg.verify import build_verify_template

_SRC = Path("src/tagplatform/capabilities/prompts")

# 分层红线针对 import 语句（docstring 里陈述红线本身提及这些名字是合法的）
_FORBIDDEN_IMPORTS = (
    r"^\s*import\s+fastapi", r"^\s*from\s+fastapi",
    r"^\s*import\s+sqlalchemy", r"^\s*from\s+sqlalchemy",
    r"^\s*import\s+tagplatform\.services", r"^\s*from\s+tagplatform\.services",
)


def _layering_clean(module: str) -> None:
    text = (_SRC / module).read_text(encoding="utf-8")
    for pattern in _FORBIDDEN_IMPORTS:
        assert not re.search(pattern, text, re.M), f"{module} 违反分层红线: {pattern}"


def test_mtg_module_layering():
    _layering_clean("mtg.py")


@pytest.mark.parametrize("module", ["_evaluation.py", "assets.py", "registry.py", "open_review.py"])
def test_central_modules_layering(module):
    _layering_clean(module)


@pytest.mark.parametrize("text", [
    SCENE_GENERATION_TEMPLATE, FILM_GENERATION_TEMPLATE,
    JUDGE_TEMPLATE, JUDGE_RETRY_HINT,
    DESCRIPTION_TEMPLATE, DESCRIPTION_RETRY_HINT,
    VERIFY_TEMPLATE, VERIFY_RETRY_HINT,
    CLOSED_RETRY_HINT,
])
def test_constants_non_empty(text):
    assert text.strip()


def test_generation_template_uses_central_copy():
    scene = build_generation_template("scene")
    scene_text = "\n".join(p["text"] for p in scene["messages"][0]["content"]
                           if p["type"] == "text")
    assert "## 标签生成任务" in scene_text
    assert "根据当前片段，提炼核心含义符合目标目录定义" in scene_text
    assert "只生成核心含义属于当前目标目录的标签。" in scene_text
    assert "{content}" not in scene_text and "{target_directory}" not in scene_text

    film = build_generation_template("film")
    film_text = "\n".join(p["text"] for p in film["messages"][0]["content"]
                          if p["type"] == "text")
    assert "第二轮：结合影片材料和逐场候选" in film_text
    assert "## 如何填写" in film_text
    assert "## 库里已有的标签" in film_text
    assert "只生成核心含义属于当前目标目录的标签。" in film_text
    assert "{library_hint}" not in film_text and "{max_tags}" not in film_text


def test_judge_messages_use_central_copy():
    text = render_judge_messages("正文", "- 候选｜维度｜理由")[0]["content"]
    assert "你是影视内容标签审核员。" in text
    assert "## 评分标准" in text
    assert "内容呈现（presentation）" in text
    assert "{candidates}" not in text and "{content}" not in text


def test_judge_retry_suffix_is_central():
    assert build_judge_template()["retry_suffix"] == JUDGE_RETRY_HINT


def test_verify_uses_central_copy():
    template = build_verify_template()
    content = template["messages"][0]["content"]
    assert "## 相似度复核" in content
    assert "判断标准只有一条" in content
    assert '只输出一个 JSON 对象：{"results":' in content
    assert "{pairs}" not in content
    assert template["retry_suffix"] == VERIFY_RETRY_HINT


def test_description_uses_central_copy():
    template = build_description_template()
    content = template["messages"][0]["content"]
    assert "## 通用释义（标签描述补写）" in content
    assert "标签清单：" in content
    assert "{items}" not in content
    assert template["retry_suffix"] == DESCRIPTION_RETRY_HINT
