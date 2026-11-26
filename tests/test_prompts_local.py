"""capabilities/prompts 本地化契约测试（四层重构 Task 3）。

文案保真（Review Focus #2）：get_prompt 返回值与迁移文件的冻结字面量
逐字一致——closed 取 0016 播种版、open 取 0002 播种版（0013/0016 只
UPDATE closed 行，open v1 文案自 0002 起未再变）。迁移文件是历史快照，
是 models.py 常量删除后唯一自包含的逐字基准，经 importlib 加载其模块级
常量（不执行任何迁移操作）。

其余契约：未知 mode 抛 ValueError；模板常量满足原 `_validate_template_text`
的全部语义（模板为代码常量后运行时校验退役为测试断言——错了测试红而非
运行时拦截）；GET /api/v1/prompts 只读契约视图的结构与白名单一致。
"""

import importlib.util
import string
from pathlib import Path

import pytest

from tagplatform.capabilities.prompts import (
    ALLOWED_FIELDS,
    REQUIRED_PLACEHOLDERS,
    get_prompt,
    render_prompt,
)

# 仓库根（alembic/versions 所在目录）
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _migration_literal(filename: str, constant: str) -> str:
    """从迁移文件加载冻结字面量（模块级常量，加载即取值，不跑迁移）。"""
    path = _REPO_ROOT / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(
        f"_migration_snapshot_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, constant)


# ---------- 文案保真（Review Focus #2：错一字即评测口径漂移） ----------


def test_get_prompt_closed_matches_frozen_0016_literal():
    """closed 文案保真：本地常量 == 0016 播种版逐字（三模态 evidence 版）。"""
    frozen = _migration_literal(
        "0016_closed_prompt_multimodal_evidence.py", "_PROMPT_V1_CLOSED_TEXT")
    assert get_prompt("closed") == frozen


def test_get_prompt_open_retired_raises_value_error():
    """open 模板常量已退役（开放生成由内置 mtg 流水线自拼 prompt）。"""
    with pytest.raises(ValueError):
        get_prompt("open")


def test_render_prompt_output_matches_frozen_literal_render():
    """渲染输出保真：本地模板渲染结果与冻结字面量渲染结果逐字一致。"""
    frozen = _migration_literal(
        "0016_closed_prompt_multimodal_evidence.py", "_PROMPT_V1_CLOSED_TEXT")
    rendered_local = render_prompt(get_prompt("closed"), content="内容X")
    rendered_frozen = frozen.format(content="内容X")
    assert rendered_local == rendered_frozen


def test_get_prompt_unknown_mode_raises_value_error():
    """未知 mode 显式失败（ValueError），不静默回退。"""
    with pytest.raises(ValueError):
        get_prompt("nope")


# ---------- 模板常量契约（原 _validate_template_text 语义转测试断言） ----------


@pytest.mark.parametrize("mode", ["closed"])
def test_template_constant_required_placeholders(mode):
    """模板含该 mode 全部必需占位符（closed 为 {content}）。"""
    text = get_prompt(mode)
    for placeholder in REQUIRED_PLACEHOLDERS[mode]:
        assert placeholder in text


@pytest.mark.parametrize("mode", ["closed"])
def test_template_constant_no_illegal_fields(mode):
    """占位符白名单：format 解析出的字段只允许 content（字面花括号须双写
    转义——parse 对转义段返回 None，不受白名单约束）；format 语法合法
    （花括号配对，parse 失败即红）且 strip 后非空。"""
    text = get_prompt(mode)
    assert text.strip() == text and text  # 落库即 strip 形态的语义延续
    fields = [field for _, field, _, _ in string.Formatter().parse(text)]
    for field in fields:
        assert field is None or field in ALLOWED_FIELDS, field


# ---------- GET /api/v1/prompts 只读契约视图 ----------


def test_contract_endpoint_readonly_structure(client):
    """GET 契约：逐项 {mode, template_text, allowed_fields,
    required_placeholders}，与白名单导出一致，正文与本地常量逐字一致，
    无版本字段（git 即版本）。"""
    resp = client.get("/api/v1/prompts")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["mode"] for r in rows] == ["closed"]
    for row in rows:
        assert set(row.keys()) == {"mode", "template_text",
                                   "allowed_fields", "required_placeholders"}
        assert row["template_text"] == get_prompt(row["mode"])
        assert row["allowed_fields"] == ["content"]
        assert row["required_placeholders"] == ["{content}"]


def test_contract_endpoint_mode_filter(client):
    """mode 精确过滤沿用既有语义：命中返回单项，未知 mode 返回空列表。"""
    closed = client.get("/api/v1/prompts", params={"mode": "closed"})
    assert closed.status_code == 200
    assert [r["mode"] for r in closed.json()] == ["closed"]
    empty = client.get("/api/v1/prompts", params={"mode": "hybrid"})
    assert empty.status_code == 200 and empty.json() == []
