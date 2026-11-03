"""资产生成测试（数据集影片 → 上游契约格式的资产 JSON）。

覆盖：
- 关键帧按 story 的 shot 区间等距抽取；
- 字幕解析（SRT）、按场景截取与等距抽样、无字幕时为空；
- 文件名 → 数据集影片的匹配（IMDb id / 精确 / 包含 / 模糊 / 不命中）；
- 模板组装：characters、character_ids、events、emotion、dialogues（含 speaker
  映射）、audio_events 恒空；
- 端到端（假客户端 + 临时数据集 + 临时字幕 zip）：调用顺序、tokens、
  进度回调、断点续跑、失败兜底；
- infer=False 只填数据集事实（零调用）；
- 产物能被平台的上游适配器直接吃下。
"""

import json
import zipfile
from pathlib import Path

import pytest

from tagplatform.adapters.vision import to_content_json
from tagplatform.assets import (
    ASSET_BUILDER_VERSION,
    AssetBuilder,
    CastIndex,
    assemble_asset,
    build_index,
    lines_for_scene,
    match,
    normalize,
    parse_filename,
    parse_scene_result,
    parse_srt,
    parse_summary_result,
    pick_keyframes,
    plan_scenes,
    source_tag,
)
from tagplatform.assets.subtitles import SubtitleLine
from tagplatform.llm import LLMResponse
from tagplatform.schemas.upstream import parse_upstream

LONG_SUMMARY = ("这是整片的中文剧情描述，交代了主要人物、核心冲突与结局走向，"
                "篇幅足够长，可以通过长度闸门；它同时用于验证摘要解析、重试与"
                "兜底逻辑，因此必须超过最小长度阈值，避免被当成残片丢弃。")

MOVIE_ID = "tt0000001"


# ---------------------------------------------------------------- 工具

def _touch_frames(root: Path, movie_id: str, shots: range) -> None:
    d = root / "frames" / "frames" / movie_id
    d.mkdir(parents=True, exist_ok=True)
    for shot in shots:
        for img in range(2):
            (d / f"shot_{shot:04d}_img_{img}.jpg").write_bytes(b"fakejpg")


def _write_srt(root: Path, movie_id: str) -> None:
    """写一个覆盖两个场景的小字幕包（0-10s / 10-20s）。"""
    entries = []
    for i, (start, end, text) in enumerate([
        (1, 2, "Get in the car."),
        (3, 4, "Where are we going?"),
        (11, 12, "Don't ask questions."),
        (15, 16, "We're here."),
    ], start=1):
        entries.append(f"{i}\n00:00:{start:02d},000 --> 00:00:{end:02d},500\n{text}\n")
    with zipfile.ZipFile(root / "subtitle1K.v1.zip", "w") as z:
        z.writestr(f"subtitle/{movie_id}.srt", "\n".join(entries))


def _make_dataset(root: Path, movie_id: str = MOVIE_ID) -> None:
    (root / "meta_data" / "meta").mkdir(parents=True, exist_ok=True)
    (root / "annotation_data" / "annotation").mkdir(parents=True, exist_ok=True)
    (root / "meta_data" / "meta" / f"{movie_id}.json").write_text(json.dumps({
        "title": "Test Movie (1999)", "genres": ["Drama"], "language": "en",
        "synopsis": "An English synopsis about a test movie.",
        "imdb_rating": 8.7,
        "cast": [{"name": "Actor A", "character": "Don Vito"},
                 {"name": "Actor B", "character": "Bonasera"}],
    }, ensure_ascii=False), encoding="utf-8")
    (root / "annotation_data" / "annotation" / f"{movie_id}.json").write_text(
        json.dumps({"story": [
            {"shot": [1, 2], "duration": [0, 10], "description": "Scene one desc."},
            {"shot": [3, 4], "duration": [10, 20], "description": "Scene two desc."},
        ]}, ensure_ascii=False), encoding="utf-8")
    _touch_frames(root, movie_id, range(0, 6))
    _write_srt(root, movie_id)


class FakeClient:
    """按 run_id 返回预置 JSON 的假客户端（零网络）。"""

    def __init__(self, fail_run_ids: set[str] | None = None,
                 bad_summary: bool = False):
        self.calls: list[str] = []
        self.fail = fail_run_ids or set()
        self.bad_summary = bad_summary

    def complete(self, messages, run_id):
        self.calls.append(run_id)
        if run_id in self.fail:
            return LLMResponse(content="我觉得这场戏很精彩。", usage={}, raw={}, ok=True)
        if run_id.startswith("summary_"):
            content = "[1945]" if self.bad_summary else json.dumps(
                {"summary": LONG_SUMMARY}, ensure_ascii=False)
            return LLMResponse(content=content,
                               usage={"prompt_tokens": 100, "completion_tokens": 50},
                               raw={}, ok=True)
        return LLMResponse(
            content=json.dumps({
                "description": f"{run_id} 的中文剧情描述",
                "atmosphere": ["紧张压迫", "压抑沉重", "乱写的词"],
                "events": [{"description": "两人在车里争执",
                            "participants": [
                                {"name": "Don Vito", "emotions": ["愤怒", "不存在"]},
                                {"name": "Bonasera", "emotions": ["恐惧"]},
                                {"name": "路人", "emotions": ["愤怒"]},
                            ]}],
                "virtual_time": "1999年冬",
                "location": "车内",
                "dialogues": [
                    {"line": 1, "speaker": "Don Vito"},
                    {"line": 2, "speaker": ""},
                ],
            }, ensure_ascii=False),
            usage={"prompt_tokens": 200, "completion_tokens": 80},
            raw={}, ok=True)


# ---------------------------------------------------------------- 字幕

def test_parse_srt_basic():
    raw = ("1\n00:00:01,000 --> 00:00:02,500\n<i>Get in</i> the car.\n\n"
           "2\n00:01:03,250 --> 00:01:04,000\n- Don't ask.\n")
    lines = parse_srt(raw)
    assert len(lines) == 2
    assert lines[0].start_ms == 1000 and lines[0].end_ms == 2500
    assert lines[0].text == "Get in the car."
    assert lines[1].start_ms == 63250 and lines[1].text == "Don't ask."


def test_parse_srt_skips_bad_blocks():
    assert parse_srt("not a subtitle") == []
    assert parse_srt("1\n00:00:01,000 --> \nno end\n") == []


def test_clean_text_strips_sdh_marks():
    from tagplatform.assets.subtitles import clean_text
    assert clean_text("[door slams]") == "[door slams]"      # 纯音效行保留
    assert clean_text("Oh, God. - [Thud]") == "Oh, God."
    assert clean_text("- Yes. - No.") == "Yes. No."
    assert clean_text("<i>Run!</i>") == "Run!"


def test_lines_for_scene_caps_and_samples():
    lines = [SubtitleLine(i * 1000, i * 1000 + 500, f"line {i}") for i in range(20)]
    picked = lines_for_scene(lines, 0, 20_000, limit=5)
    assert len(picked) == 5
    assert [p.start_ms for p in picked] == sorted(p.start_ms for p in picked)
    # 窗口落在两行之间的空档 → 无交集
    assert lines_for_scene(lines, 2600, 2900, limit=5) == []
    assert lines_for_scene([], 0, 10, limit=5) == []


# ---------------------------------------------------------------- 文件名匹配

def test_normalize_and_parse_filename():
    assert normalize("The Godfather") == "godfather"
    assert normalize("Star Wars: Episode V") == "star wars episode v"
    assert parse_filename("The.Godfather.1972.1080p.BluRay.x264.mkv")[0] == \
        "The Godfather"
    assert parse_filename("tt0068646.mp4")[1] == "tt0068646"


def test_build_index_and_match(tmp_path):
    root = tmp_path / "mn"
    _make_dataset(root)
    index = build_index(root)
    assert len(index) == 1 and index[0].movie_id == MOVIE_ID
    assert index[0].year == "1999" and index[0].rating == 8.7

    hit = match("Test.Movie.1999.720p.mkv", index)
    assert hit is not None and hit.movie.movie_id == MOVIE_ID
    assert hit.how in ("exact", "contains")

    by_id = match("tt0000001.mp4", index)
    assert by_id is not None and by_id.how == "imdb_id"

    fuzzy = match("Test Movi 1999.mkv", index)
    assert fuzzy is not None and fuzzy.movie.movie_id == MOVIE_ID
    assert fuzzy.how in ("contains", "fuzzy")

    assert match("完全无关的片名.mp4", index) is None


# ---------------------------------------------------------------- 模板组装

def test_assemble_asset_template_shape():
    cast = CastIndex(["Don Vito", "Bonasera"])
    scene = {
        "scene_id": "s1", "start_ms": 0, "end_ms": 1000,
        "description": "场景描述", "virtual_time": "1999年", "location": "车内",
        "character_ids": ["character_001"],
        "events": [{"event_id": "event_001", "description": "争执",
                    "participant_ids": [{"character_id": "character_001",
                                         "emotion": [{"name": "愤怒",
                                                      "code": "ANGER"}]}]}],
        "emotion": [{"name": "紧张压迫", "code": "TENSE_OPPRESSIVE"}],
        "dialogues": [{"dialogue_id": "dialogue_001",
                       "speaker_id": "character_001",
                       "speaker_name": "Don Vito", "text": "Hi",
                       "start_ms": 0, "end_ms": 100}],
        "audio_events": [],
    }
    asset = assemble_asset("tt1", "片名", "en", 1000, "整片描述", cast, [scene])
    assert asset["asset"]["asset_id"] == "movie_tt1"
    # 角色只有模板里的四个键（演员写在 description 里）
    assert asset["content"]["characters"][0] == {
        "character_id": "character_001", "name": "Don Vito",
        "aliases": [], "description": None}
    body = asset["content"]["scenes"][0]
    assert set(body) == {"scene_id", "start_ms", "end_ms", "description",
                         "virtual_time", "location", "character_ids", "events",
                         "emotion", "dialogues", "audio_events"}
    assert body["emotion"] == [{"name": "紧张压迫", "code": "TENSE_OPPRESSIVE"}]
    assert body["audio_events"] == []


def test_parse_scene_result_filters_and_defaults():
    out = parse_scene_result(json.dumps({
        "description": "d", "atmosphere": ["紧张", " ", ""],
        "dialogues": [{"line": 1, "speaker": "乙"}, {"line": "x"}],
        "events": [{"description": "事件",
                    "participants": [{"name": "乙",
                                      "emotions": ["愤怒", " ", "狂喜"]}]}],
    }, ensure_ascii=False))
    assert out["atmosphere"] == ["紧张"]
    assert out["dialogues"] == [{"line": 1, "speaker": "乙"}]
    assert out["events"][0]["participants"] == [
        {"name": "乙", "emotions": ["愤怒", "狂喜"]}]


# ---------------------------------------------------------------- 端到端

def test_cast_index_writes_actor_into_description():
    """模板没有演员字段：演员写进 description，不另开字段。"""
    cast = CastIndex([("Don Vito", "Marlon Brando")])
    assert cast.characters[0] == {
        "character_id": "character_001", "name": "Don Vito", "aliases": [],
        "description": "由 Marlon Brando 饰演"}
    assert cast.cid("don vito") == "character_001"
    assert CastIndex(["无名角色"]).characters[0]["description"] is None


def test_build_movie_end_to_end(tmp_path):
    root = tmp_path / "mn"
    _make_dataset(root)
    client = FakeClient()
    progress: list[tuple[int, int]] = []
    builder = AssetBuilder(root, tmp_path / "out", client, k=2, workers=1,
                           progress_cb=lambda d, t: progress.append((d, t)))
    stats = builder.build_movie(MOVIE_ID)

    assert stats.scenes == 2 and not stats.failed_scenes
    assert stats.scenes_with_dialogues == 2
    assert client.calls[:2] == [f"{MOVIE_ID}_story_000", f"{MOVIE_ID}_story_001"]
    assert client.calls[2] == f"summary_{MOVIE_ID}"

    data = json.loads(Path(stats.output).read_text(encoding="utf-8"))
    assert data["asset"]["title"] == "Test Movie"
    assert data["content"]["description"] == LONG_SUMMARY
    scenes = data["content"]["scenes"]
    assert [s["scene_id"] for s in scenes] == ["scene_001", "scene_002"]
    first = scenes[0]
    # 氛围只留词表内的词，并带上大写码
    assert first["emotion"] == [{"name": "紧张压迫", "code": "TENSE_OPPRESSIVE"},
                                {"name": "压抑沉重", "code": "OPPRESSIVE_HEAVY"}]
    assert first["virtual_time"] == "1999年冬" and first["location"] == "车内"
    assert first["character_ids"] == ["character_001", "character_002"]
    # 事件参与人带情绪；词表外的角色/情绪被丢掉
    event = first["events"][0]
    assert event["event_id"] == "event_001"
    assert event["participant_ids"] == [
        {"character_id": "character_001",
         "emotion": [{"name": "愤怒", "code": "ANGER"}]},
        {"character_id": "character_002",
         "emotion": [{"name": "恐惧", "code": "FEAR"}]},
    ]
    # 台词：时间来自字幕，说话人来自模型；台词行没有情绪字段
    assert len(first["dialogues"]) == 2
    assert first["dialogues"][0] == {
        "dialogue_id": "dialogue_001",
        "speaker_id": "character_001", "speaker_name": "Don Vito",
        "text": "Get in the car.", "start_ms": 1000, "end_ms": 2500,
    }
    assert first["dialogues"][1]["speaker_id"] is None

    # 产物必须能被平台适配器吃下
    content = to_content_json(parse_upstream(data))
    assert content.content.scenes[0].emotions == ["紧张压迫", "压抑沉重"]
    assert content.content.scenes[0].dialogues[0].speaker_id == "character_001"
    assert len(content.content.characters) == 2
    # 人物情绪从事件参与人回推到场景级（标签渲染仍拿得到）
    assert [(e.character_id, e.emotion)
            for e in content.content.scenes[0].character_emotions] == [
        ("character_001", "愤怒"), ("character_002", "恐惧")]
    # 角色表带演员（写进 description，模板没有 actor 字段）
    assert content.content.characters[0].description == "由 Actor A 饰演"


def test_build_movie_template_keys(tmp_path):
    """默认口径：输出字段集与契约示例逐字一致，没有契约外的键。"""
    root = tmp_path / "mn"
    _make_dataset(root)
    builder = AssetBuilder(root, tmp_path / "out", FakeClient(), k=2, workers=1)
    data = json.loads(Path(builder.build_movie(MOVIE_ID).output)
                      .read_text(encoding="utf-8"))
    assert set(data) == {"schema_version", "asset", "content"}
    assert set(data["content"]) == {"description", "characters", "scenes"}
    assert set(data["asset"]) == {"asset_id", "asset_type", "title",
                                  "duration_ms", "video_uri", "language"}
    assert set(data["content"]["characters"][0]) == {
        "character_id", "name", "aliases", "description"}
    scene = data["content"]["scenes"][0]
    assert set(scene) == {"scene_id", "start_ms", "end_ms", "description",
                          "virtual_time", "location", "character_ids",
                          "events", "emotion", "dialogues", "audio_events"}
    assert set(scene["events"][0]) == {"event_id", "description",
                                       "participant_ids"}
    assert set(scene["events"][0]["participant_ids"][0]) == {"character_id",
                                                             "emotion"}
    assert set(scene["emotion"][0]) == {"name", "code"}
    assert set(scene["dialogues"][0]) == {"dialogue_id", "speaker_id",
                                          "speaker_name", "text", "start_ms",
                                          "end_ms"}
    # 人物情绪挂在事件参与人上，没有场景级 character_emotions
    assert "character_emotions" not in scene
    assert "emotion" not in scene["dialogues"][0]


def test_infer_off_fills_only_dataset_facts(tmp_path):
    """infer=False：一次调用都不发，只填数据集确有的字段。"""
    root = tmp_path / "mn"
    _make_dataset(root)
    client = FakeClient()
    builder = AssetBuilder(root, tmp_path / "out", client, k=2, workers=1,
                           infer=False)
    stats = builder.build_movie(MOVIE_ID)

    assert client.calls == []
    assert stats.prompt_tokens == 0 and stats.completion_tokens == 0
    assert not stats.failed_scenes
    data = json.loads(Path(stats.output).read_text(encoding="utf-8"))
    # 整片/场景描述用数据集英文原文
    assert data["content"]["description"] == "An English synopsis about a test movie."
    scene = data["content"]["scenes"][0]
    assert scene["description"] == "Scene one desc."
    # 数据集没有的字段一律留空
    assert scene["emotion"] == [] and scene["events"] == []
    assert scene["virtual_time"] is None and scene["location"] is None
    # 台词文本/时间来自数据集，说话人与情绪不知道就留空
    assert [d["text"] for d in scene["dialogues"]] == ["Get in the car.",
                                                       "Where are we going?"]
    assert all(d["speaker_id"] is None and "emotion" not in d
               for d in scene["dialogues"])
    # 角色名与演员来自 meta.cast，别名数据集没有 → 空
    assert data["content"]["characters"][0] == {
        "character_id": "character_001", "name": "Don Vito",
        "aliases": [], "description": "由 Actor A 饰演"}


def test_source_tag_two_modes():
    """推断 / 纯事实两种口径的 source 标识不同（重传不会错认缓存）。"""
    assert source_tag(True) == f"asset_builder:{ASSET_BUILDER_VERSION}-infer"
    assert source_tag(False) == f"asset_builder:{ASSET_BUILDER_VERSION}-facts"

def test_build_movie_without_subtitles(tmp_path):
    """没有字幕的片：dialogues 为空，其余照常。"""
    root = tmp_path / "mn"
    _make_dataset(root)
    (root / "subtitle1K.v1.zip").unlink()
    builder = AssetBuilder(root, tmp_path / "out", FakeClient(), k=2, workers=1)
    stats = builder.build_movie(MOVIE_ID)
    data = json.loads(Path(stats.output).read_text(encoding="utf-8"))
    assert data["content"]["scenes"][0]["dialogues"] == []
    assert stats.scenes_with_dialogues == 0


def test_build_movie_fallback_on_bad_output(tmp_path):
    root = tmp_path / "mn"
    _make_dataset(root)
    client = FakeClient(fail_run_ids={f"{MOVIE_ID}_story_000",
                                      f"{MOVIE_ID}_story_000#retry"})
    builder = AssetBuilder(root, tmp_path / "out", client, k=2, workers=1)
    stats = builder.build_movie(MOVIE_ID)
    assert f"{MOVIE_ID}_story_000" in stats.failed_scenes
    data = json.loads(Path(stats.output).read_text(encoding="utf-8"))
    assert data["content"]["scenes"][0]["description"] == "Scene one desc."


def test_build_movie_skips_existing(tmp_path):
    root = tmp_path / "mn"
    _make_dataset(root)
    client = FakeClient()
    builder = AssetBuilder(root, tmp_path / "out", client, k=2, workers=1)
    builder.build_movie(MOVIE_ID)
    calls = len(client.calls)
    builder.build_movie(MOVIE_ID)
    assert len(client.calls) == calls


def test_summary_fragment_falls_back_to_scenes(tmp_path):
    root = tmp_path / "mn"
    _make_dataset(root)
    builder = AssetBuilder(root, tmp_path / "out", FakeClient(bad_summary=True),
                           k=2, workers=1)
    stats = builder.build_movie(MOVIE_ID)
    data = json.loads(Path(stats.output).read_text(encoding="utf-8"))
    assert data["content"]["description"].startswith(f"{MOVIE_ID}_story_000")
    assert "__summary_fallback__" in stats.failed_scenes


def test_parse_summary_result_forms():
    assert parse_summary_result(json.dumps({"summary": LONG_SUMMARY},
                                           ensure_ascii=False)) == LONG_SUMMARY
    assert parse_summary_result(LONG_SUMMARY) == LONG_SUMMARY
    assert parse_summary_result("[1945]") == ""


def test_pick_keyframes_evenly(tmp_path):
    _touch_frames(tmp_path, "tt1", range(0, 10))
    picked = pick_keyframes(tmp_path / "frames" / "frames" / "tt1", [2, 5], k=3)
    shots = [int(p.name.split("_")[1]) for p in picked]
    assert len(picked) == 3 and shots == sorted(shots)
    assert all(2 <= s <= 5 for s in shots)
    assert pick_keyframes(tmp_path / "nope", [1, 2], k=3) == []


def test_plan_scenes_attaches_dialogues(tmp_path):
    root = tmp_path / "mn"
    _make_dataset(root)
    annotation = json.loads(
        (root / "annotation_data" / "annotation" / f"{MOVIE_ID}.json")
        .read_text(encoding="utf-8"))
    subs = parse_srt(zipfile.ZipFile(root / "subtitle1K.v1.zip")
                     .read(f"subtitle/{MOVIE_ID}.srt").decode())
    plans = plan_scenes(root, MOVIE_ID, annotation, k=2, subtitles=subs,
                        dialogue_limit=5)
    assert [(p.start_ms, p.end_ms) for p in plans] == [(0, 10_000), (10_000, 20_000)]
    assert [len(p.dialogues) for p in plans] == [2, 2]
    assert plans[0].dialogues[0].text == "Get in the car."


@pytest.mark.parametrize("text", ["", "没有 JSON", '{"description": ""}'])
def test_parse_scene_result_rejects_bad(text):
    assert parse_scene_result(text) is None
