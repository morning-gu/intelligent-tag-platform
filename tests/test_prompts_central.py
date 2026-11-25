"""Prompt 文案集中（capabilities/prompts）的 wiring 与分层红线测试。"""
import re
from pathlib import Path

import pytest

from tagplatform.capabilities.prompts.mtg import (
    DESCRIPTION_INSTRUCTIONS, DESCRIPTION_RETRY_HINT,
    EXCERPT_DIR_GOAL_TEXT, FILM_GOAL_TEXT, GENERATION_ROLE_TEXT,
    HOW_TO_FILL_TEXT, JUDGE_CRITERIA, JUDGE_RETRY_HINT, JUDGE_ROLE_BRIEF,
    LIBRARY_HINT_HEAD_TEXT, TARGET_DIR_RULE_TEXT, VERIFY_INSTRUCTIONS,
    VERIFY_OUTPUT_FORMAT, VERIFY_RETRY_HINT,
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


@pytest.mark.parametrize("module", ["assets.py", "registry.py", "open_review.py"])
def test_central_modules_layering(module):
    _layering_clean(module)


@pytest.mark.parametrize("text", [GENERATION_ROLE_TEXT, EXCERPT_DIR_GOAL_TEXT,
    FILM_GOAL_TEXT, HOW_TO_FILL_TEXT, TARGET_DIR_RULE_TEXT,
    LIBRARY_HINT_HEAD_TEXT, JUDGE_CRITERIA, JUDGE_ROLE_BRIEF,
    DESCRIPTION_INSTRUCTIONS, DESCRIPTION_RETRY_HINT, VERIFY_INSTRUCTIONS,
    VERIFY_OUTPUT_FORMAT, VERIFY_RETRY_HINT, JUDGE_RETRY_HINT,
    CLOSED_RETRY_HINT])
def test_constants_non_empty(text):
    assert text.strip()


def test_generation_template_uses_central_copy():
    scene = build_generation_template("scene")
    scene_text = "".join(p["text"] for p in scene["messages"][0]["content"]
                         if p["type"] == "text")
    assert GENERATION_ROLE_TEXT in scene_text
    assert EXCERPT_DIR_GOAL_TEXT in scene_text
    assert TARGET_DIR_RULE_TEXT in scene_text
    film = build_generation_template("film")
    film_text = "".join(p["text"] for p in film["messages"][0]["content"]
                        if p["type"] == "text")
    assert FILM_GOAL_TEXT in film_text
    assert HOW_TO_FILL_TEXT in film_text
    assert LIBRARY_HINT_HEAD_TEXT in film_text
    assert TARGET_DIR_RULE_TEXT in film_text


def test_judge_messages_use_central_copy():
    text = render_judge_messages("正文", "- 候选｜维度｜理由")[0]["content"]
    assert JUDGE_ROLE_BRIEF in text
    assert JUDGE_CRITERIA in text


def test_judge_retry_suffix_is_central():
    assert build_judge_template()["retry_suffix"] == JUDGE_RETRY_HINT


def test_verify_uses_central_copy():
    template = build_verify_template()
    content = template["messages"][0]["content"]
    assert VERIFY_INSTRUCTIONS in content
    assert VERIFY_OUTPUT_FORMAT in content
    assert template["retry_suffix"] == VERIFY_RETRY_HINT


def test_description_uses_central_copy():
    template = build_description_template()
    assert DESCRIPTION_INSTRUCTIONS in template["messages"][0]["content"]
    assert template["retry_suffix"] == DESCRIPTION_RETRY_HINT
