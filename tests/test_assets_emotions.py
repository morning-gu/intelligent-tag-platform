"""情绪词表测试：中文名 ↔ 大写码，词表外的词一律丢弃。"""
import pytest

from tagplatform.assets.emotions import (
    FEELINGS,
    MOODS,
    prompt_catalog,
    to_emotion,
    to_emotions,
)


def test_catalog_codes_are_unique_and_uppercase():
    codes = [code for _, code in FEELINGS + MOODS]
    assert len(codes) == len(set(codes))
    assert all(code == code.upper() and " " not in code for code in codes)


def test_to_emotion_by_name_or_code():
    assert to_emotion("愤怒") == {"name": "愤怒", "code": "ANGER"}
    assert to_emotion(" anger ") == {"name": "愤怒", "code": "ANGER"}
    assert to_emotion("TENSE_OPPRESSIVE") == {
        "name": "紧张压迫", "code": "TENSE_OPPRESSIVE"}


def test_unknown_word_is_dropped():
    assert to_emotion("狂喜") is None          # 词表外
    assert to_emotion("") is None
    assert to_emotion(None) is None


def test_pool_restricts_vocabulary():
    """人物情绪表里没有“紧张压迫”，场景氛围表里没有“愤怒”。"""
    assert to_emotion("紧张压迫", FEELINGS) is None
    assert to_emotion("愤怒", MOODS) is None
    assert to_emotion("愤怒", FEELINGS) == {"name": "愤怒", "code": "ANGER"}


def test_to_emotions_dedupes_and_limits():
    out = to_emotions(["愤怒", "ANGER", "恐惧", "乱写", "悲伤"], FEELINGS, limit=2)
    assert out == [{"name": "愤怒", "code": "ANGER"},
                   {"name": "恐惧", "code": "FEAR"}]
    assert to_emotions("不是列表", FEELINGS) == []


def test_prompt_catalog_lists_name_and_code():
    text = prompt_catalog(FEELINGS)
    assert "愤怒(ANGER)" in text and "恐惧(FEAR)" in text
    assert text.count("(") == len(FEELINGS)
