"""TagProducer 注册表与协议注册校验测试。

覆盖契约：
- 正常注册与按 mode 查询；重复注册同 mode 抛 ValueError；
- 未知 mode 抛 KeyError 且信息列出可用 mode；
- 协议成员缺失或类型不符时在 register_producer 阶段即抛 TypeError。
"""

import pytest

import tagplatform.services.generation.base as generation_base
from tagplatform.services.generation import (
    get_producer,
    list_producers,
    register_producer,
)


class _DummyProducer:
    """测试用最小生成器：满足 TagProducer 协议结构即可注册。"""

    mode = "dummy"
    builds_prompt = False
    prompt_version = "dummy"

    def produce(self, spec, session, llm, embedder):
        return []


@pytest.fixture()
def restore_registry():
    """快照模块级注册表并在测试后还原，避免测试污染全局状态。"""
    saved = dict(generation_base._REGISTRY)
    yield
    generation_base._REGISTRY.clear()
    generation_base._REGISTRY.update(saved)


# ---- 注册表三函数 ----


def test_register_and_get_producer(restore_registry):
    p = _DummyProducer()
    register_producer(p)
    assert get_producer("dummy") is p
    assert list_producers() == ["closed", "dummy", "open"]


def test_register_duplicate_mode_raises_valueerror(restore_registry):
    register_producer(_DummyProducer())
    with pytest.raises(ValueError):
        register_producer(_DummyProducer())


def test_unknown_mode_message_lists_available(restore_registry):
    register_producer(_DummyProducer())
    with pytest.raises(KeyError) as exc_info:
        get_producer("nope")
    # 契约：KeyError 信息列出可用 mode
    assert "dummy" in str(exc_info.value)


# ---- 协议违约：注册时校验（非调用时——动态扩展承诺的护栏）----


def test_register_missing_prompt_version_raises_typeerror(restore_registry):
    """缺 prompt_version 声明的假 producer → register_producer 抛 TypeError。

    注册时即拒绝，而非等 runner 调用时才暴露（漏实现成员不允许静默
    写空口径进 generation_task.prompt_version）。
    """

    class _NoPromptVersion:
        mode = "no_pv"
        builds_prompt = False

        def produce(self, spec, session, llm, embedder):
            return []

    with pytest.raises(TypeError):
        register_producer(_NoPromptVersion())


def test_register_missing_builds_prompt_raises_typeerror(restore_registry):
    """缺 builds_prompt 声明的假 producer → register_producer 抛 TypeError。"""

    class _NoBuildsPrompt:
        mode = "no_bp"
        prompt_version = "1"

        def produce(self, spec, session, llm, embedder):
            return []

    with pytest.raises(TypeError):
        register_producer(_NoBuildsPrompt())


def test_register_wrong_member_type_raises_typeerror(restore_registry):
    """声明成员类型不符协议（prompt_version 非 str）→ 注册时 TypeError。"""

    class _BadType:
        mode = "bad_type"
        builds_prompt = False
        prompt_version = 1  # 协议声明 str（TEXT 列写入侧即字符串）

        def produce(self, spec, session, llm, embedder):
            return []

    with pytest.raises(TypeError):
        register_producer(_BadType())
