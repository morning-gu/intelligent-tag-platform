"""闭环（closed）生成的候选标签文本块：渲染与拼接。

producer（生成）与 confidence（Ccons 重评）共用本模块，保证两侧拼出的
prompt 结构同构：候选标签块作为 `## 候选标签` 节插入在渲染后正文的
`## 影视内容` 节之前。块内不使用 JSON，逐行给出目录头与每个候选标签的
tag_id、标签名称、标签描述、标签别名（全角冒号分隔字段），便于 LLM 逐
条阅读。
"""

from __future__ import annotations

# 块首节头。producer 与 confidence 拼接后均以此节开始候选区。
_CANDIDATE_HEADER = "## 候选标签"
# 插入锚点：候选块插在此节标题之前（模板渲染后的正文区首节）。
_CONTENT_HEADER = "## 影视内容"


def _one_line(value: str) -> str:
    """压平为单行：换行/连续空白折叠成单个空格。"""
    return " ".join(value.split())


def render_candidate_block(
    dir_name: str,
    dir_description: str,
    tags: list[tuple[str, str, str, tuple[str, ...]]],
) -> str:
    """渲染候选标签块（含 `## 候选标签` 节头，末尾不带换行）。

    tags 每项为 (tag_id, tag_name, description, aliases)。目录描述与
    标签描述/别名均单行化；目录描述为空则省略该行；标签无别名则省略
    别名行。
    """
    lines = [_CANDIDATE_HEADER, f"目录：{_one_line(dir_name)}"]
    dir_description = _one_line(dir_description)
    if dir_description:
        lines.append(f"目录描述：{dir_description}")
    for tag_id, tag_name, description, aliases in tags:
        lines.append("")
        lines.append(f"- tag_id：{tag_id} 标签名称：{_one_line(tag_name)}")
        lines.append(f"  标签描述：{_one_line(description)}")
        aliases = tuple(_one_line(alias) for alias in aliases if _one_line(alias))
        if aliases:
            lines.append(f"  标签别名：{'、'.join(aliases)}")
    return "\n".join(lines)


def splice_candidate_block(prompt_text: str, block: str) -> str:
    """把候选块插入 prompt_text 的 `## 影视内容` 节之前。

    模板渲染后 `## 影视内容` 之前是导语（以空行结尾），插入结果形如
    「导语\\n\\n候选块\\n\\n## 影视内容 …」。锚点缺失说明模板结构已
    变，抛 ValueError 而非静默追加。
    """
    idx = prompt_text.find(_CONTENT_HEADER)
    if idx < 0:
        raise ValueError(
            f"closed prompt 模板缺少 {_CONTENT_HEADER!r} 节，无法插入候选标签块")
    return prompt_text[:idx] + block + "\n\n" + prompt_text[idx:]
