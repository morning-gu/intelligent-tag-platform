"""装配两级化测试（四层重构 Task 5）。

契约（brief 三测试 + 支撑钉死）：
- test_build_spec_pure_memory：build_spec 纯内存零 DB——装配第二级不得
  触碰 try_load_library / get_prompt（模板已随 context 快照携带）；
- test_load_passes_session_through：load_assembly_context 是装配层唯一
  DB 交互点，session 参数透传 try_load_library（缺省 None = 全局引擎）；
- test_snapshot_isolation_after_db_change：context 装好后变更 DB 库行，
  context 与 build_spec 产出仍用旧库（评测快照语义的单元级验收；服务级
  验收见 test_closed_eval.py::test_run_uses_run_start_library_snapshot）；
- test_generation_and_eval_share_assembly：生成（services/generation/execute）
  与评测（closed_eval）import 的装配函数同源（零漂移的结构保证）；
- test_closed_build_spec_renders_template / test_policy_json_override /
  test_open_mode_skips_template：原 runner.assemble_spec 两测试的两级化
  迁移 + builds_prompt 规则钉死。
"""

import tagplatform.services.eval.service as closed_eval_mod
import tagplatform.services.generation.assemble as assemble_mod
import tagplatform.services.generation.execute as execute_mod
from tagplatform.services.generation.assemble import (
    AssemblyContext,
    build_spec,
    load_assembly_context,
)


class _FakeProducer:
    """装配假体：协议声明成员齐备的模板型生产器（不走注册表）。"""

    mode = "closed"
    builds_prompt = False
    prompt_version = "1"


def test_build_spec_pure_memory(monkeypatch, content_fixture):
    """build_spec 纯内存：构造 AssemblyContext 假体后装配，零 DB 触碰。

    装配层仅有的两条 DB/常量读取路径（try_load_library / get_prompt）在
    assemble 模块命名空间被计数桩替换——build_spec 全程零命中，证明
    「干跑循环零 DB 装配」在结构上成立而非依赖调用纪律。
    """
    calls: list[str] = []
    monkeypatch.setattr(
        assemble_mod, "try_load_library",
        lambda session=None: calls.append("library") or None)
    monkeypatch.setattr(
        assemble_mod, "get_prompt",
        lambda mode: calls.append("prompt") or "本模板不该被读")

    context = AssemblyContext(library=object(), prompt_text="模板 {content}",
                              policy=assemble_mod.GenerationPolicy(),
                              producer=_FakeProducer())
    spec = build_spec(context, content=content_fixture, task_id="t1",
                      asset_id="a1", run_id="r1")

    assert calls == []  # 零 DB、零模板常量读取
    assert spec.task_id == "t1" and spec.asset_id == "a1" and spec.run_id == "r1"
    assert spec.content is content_fixture
    assert spec.policy is context.policy  # 策略随 context 携带
    assert spec.library is context.library  # 库视图随 context 携带
    assert spec.prompt_text == f"模板 {assemble_mod._render_content_text(content_fixture)}"


def test_load_passes_session_through(monkeypatch):
    """load_assembly_context 是唯一 DB 交互点：session 透传
    try_load_library；缺省 None（全局引擎，原 assemble_spec 同款）。"""
    seen: list[object] = []
    sentinel = object()
    monkeypatch.setattr(
        assemble_mod, "try_load_library",
        lambda session=None: seen.append(session) or None)

    load_assembly_context("closed", session=sentinel)
    assert seen == [sentinel]
    load_assembly_context("closed")
    assert seen == [sentinel, None]


def test_snapshot_isolation_after_db_change(content_fixture):
    """快照隔离（单元级）：context 装好后变更 DB 库行，装配产出不变。

    write_library 走真实 DB 路径（tag_catalog/tag_define 重灌）——装配
    结果持有的旧库对象与 build_spec 产出完全不受影响。
    """
    from tests.library_fixture import library_root, write_library

    context = load_assembly_context("closed")
    library_before = context.library
    assert library_before is not None
    spec_before = build_spec(context, content=content_fixture, task_id="t1",
                             asset_id="a1", run_id="r1")

    # 变更 DB 库行：默认库 → 单目录单标签的另一套库
    write_library(library_root(), dirs=[{
        "dir_id": "dir_alt", "parent_id": None, "level": 1,
        "dir_name": "变更后目录", "dir_type": "dir_static",
        "applicable_scope": "content", "description": "装配后变更",
        "aliases": [],
    }], tags=[{
        "tag_id": "tag_alt", "dir_id": "dir_alt", "tag_name": "新标签",
        "tag_type": "val_fixed", "description": "装配后变更的标签",
        "status": "active",
    }])

    assert context.library is library_before  # 装配结果不随 DB 变化
    spec_after = build_spec(context, content=content_fixture, task_id="t1",
                            asset_id="a1", run_id="r1")
    assert spec_after.library is library_before
    assert spec_after.library.tag("tag_alt") is None  # 新库行不在快照里
    assert spec_after.library.tag("tag_action") is not None  # 旧库照常可用
    assert spec_before.library is spec_after.library


def test_generation_and_eval_share_assembly():
    """生成与评测同源：两条路径 import 的装配函数是同一对象。"""
    assert execute_mod.load_assembly_context is assemble_mod.load_assembly_context
    assert execute_mod.build_spec is assemble_mod.build_spec
    assert (closed_eval_mod.load_assembly_context
            is assemble_mod.load_assembly_context)
    assert closed_eval_mod.build_spec is assemble_mod.build_spec


def test_closed_build_spec_renders_template(content_fixture):
    """closed 模式：load 取本地模板常量（原文），build 渲染 content 文本。

    原 runner.assemble_spec 测试的两级化迁移；口径版本从 producer 协议
    声明派生（Task 4 起不按 mode 查表），meta 的整数口径在消费方转 int。
    """
    context = load_assembly_context("closed")
    assert context.library is not None
    assert "{content}" in context.prompt_text  # 模板原文，未渲染
    spec = build_spec(context, content=content_fixture, task_id="t1",
                      asset_id="a1", run_id="r1")
    assert "摘要" in spec.prompt_text and "{content}" not in spec.prompt_text
    assert int(context.producer.prompt_version) == 1


def test_policy_json_override(content_fixture):
    """非空 policy_json 在 load 级解析（默认 30 → 5），随 context 进 spec。"""
    context = load_assembly_context("closed", policy_json='{"max_tags": 5}')
    assert context.policy.max_tags == 5
    spec = build_spec(context, content=content_fixture, task_id="t1",
                      asset_id="a1", run_id="r1")
    assert spec.policy is context.policy and spec.policy.max_tags == 5


def test_open_mode_skips_template(content_fixture):
    """builds_prompt=True 的生产器（open）：两级都是空 prompt 文本。"""
    context = load_assembly_context("open")
    assert context.producer.builds_prompt is True
    assert context.prompt_text == ""
    spec = build_spec(context, content=content_fixture, task_id="t1",
                      asset_id="a1", run_id="r1")
    assert spec.prompt_text == ""
