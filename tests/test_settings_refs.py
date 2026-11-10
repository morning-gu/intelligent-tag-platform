"""Settings 引用守护：源码里读的 `settings.<x>` 必须真的在 Settings 上存在。

事故背景（2026-11-10）：`settings.open_library_verify_max_tokens` 在合 main 时
被整块覆盖掉（字段定义丢了、调用点还在），直到跑生成任务、构造复核客户端才
`AttributeError`，任务直接失败。这类"只在运行时、只在某条分支上"的漏字段用
测试拦，比等人肉发现便宜得多。

用 tokenize 扫源码而不是正则：注释与字符串里的 `settings.xxx`（例如
governance 里退役字段的说明）不该误报。
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

from tagplatform.settings import Settings

SRC = Path(__file__).resolve().parents[1] / "src"

# pydantic BaseModel 自带的属性访问，不是配置字段
_BUILTIN_ATTRS = {
    "model_copy", "model_dump", "model_dump_json", "model_validate",
    "model_fields", "model_fields_set", "model_construct", "model_post_init",
}


def _referenced_settings_attrs() -> dict[str, set[str]]:
    """扫 src 下所有 .py，收集 `settings.NAME` 里的 NAME（跳过字符串/注释）。"""
    found: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        try:
            tokens = list(
                tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline)
            )
        except (tokenize.TokenError, UnicodeDecodeError, OSError):
            continue
        for prev, cur, nxt in zip(tokens, tokens[1:], tokens[2:]):
            if (
                prev.type == tokenize.NAME
                and prev.string == "settings"
                and cur.type == tokenize.OP
                and cur.string == "."
                and nxt.type == tokenize.NAME
            ):
                rel = str(path.relative_to(SRC))
                found.setdefault(nxt.string, set()).add(rel)
    return found


def test_settings_references_exist():
    defined = set(Settings.model_fields)
    refs = _referenced_settings_attrs()
    assert refs, "没扫到任何 settings.<x> 引用，扫描逻辑本身可能坏了"
    missing = {
        name: files
        for name, files in refs.items()
        if name not in defined and name not in _BUILTIN_ATTRS
    }
    assert not missing, "源码引用了 Settings 没有的字段：" + "; ".join(
        f"settings.{name} ← {', '.join(sorted(files))}"
        for name, files in sorted(missing.items())
    )


def test_verify_max_tokens_default():
    """复核输出上限的默认值：一次复核烧过 27k tokens，这个兜底不能丢。"""
    assert Settings().open_library_verify_max_tokens == 4096


def test_verify_client_carries_max_tokens(monkeypatch):
    """构造复核客户端时确实把上限传下去了（mock 模式下不走这里，故用假客户端）。"""
    recorded: dict[str, object] = {}

    class FakeOpenAIClient:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

    import tagplatform.llm as llm_pkg

    monkeypatch.setattr(llm_pkg, "OpenAIClient", FakeOpenAIClient)

    from tagplatform.generation.mtg.pipeline import _make_verify_client

    settings = Settings()
    client = _make_verify_client(settings, object())  # 非 MockLLMClient → 走真实构造分支

    assert isinstance(client, FakeOpenAIClient)
    assert recorded["max_tokens"] == settings.open_library_verify_max_tokens
    assert recorded["temperature"] == 0.0
    assert recorded["model"] == settings.mtg_judge_model
