"""MovieTags 影片 JSON 的读取和平台内容转换。"""
import json
from pathlib import Path

from tagplatform.content_io import load_content
from tagplatform.schemas import ContentJSON


def read_movie_data(movie_dir: Path) -> dict:
    return json.loads((movie_dir / "content.json").read_text(encoding="utf-8"))


def _rename_speak_id(data: dict) -> None:
    """把台词的 `speak_id` 键改名为平台契约的 `speaker_id`（就地改）。

    /cache/MovieTags 的 content.json 台词字段是 `speak_id`（spec §4 记录的
    已知差异），平台上游 schema 是 `speaker_id`；不改名会被上游模型的
    extra="ignore" 静默吞掉，说话人退化成「未知说话人」。浅遍历
    content.scenes[*].dialogues[*]，键存在才改。
    """
    content = data.get("content")
    if not isinstance(content, dict):
        return
    for scene in content.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        for dialogue in scene.get("dialogues") or []:
            if isinstance(dialogue, dict) and "speak_id" in dialogue:
                dialogue["speaker_id"] = dialogue.pop("speak_id")


def parse_movie_content(data: dict) -> ContentJSON:
    _rename_speak_id(data)
    return load_content(json.dumps(data, ensure_ascii=False))
