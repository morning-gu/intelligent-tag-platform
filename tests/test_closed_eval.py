"""closed_eval 单测：数据集层 + 干跑运行器服务层的口径钉死。

夹具 `_write_dataset` 在 tmp 内搭一套迷你 MovieTags 数据集（绝不扫 /cache/*）：

1. **畸形 gt 不崩扫描**（Review Focus 1）——bad.csv 首行两列 → 扫描直接
   跳过该文件，合法的 emotion.csv 照常出现（宁缺勿崩）；
2. **usable = gt 有分 ∩ content.json 存在**——tt0000003 只有 gt 分没有
   content.json，不进可用列表；
3. **speak_id 改名**——远端 content.json 的台词字段是 `speak_id`（平台
   上游契约是 `speaker_id`），加载时统一改名，说话人不能退化成「未知」；
4. **选片确定性**——preset30 同 seed 同结果（输入顺序不同也算相等）、
   不足 30 全取、custom 越权 id 显式报错；
5. **干跑运行器（Task 4 服务层）**——起跑校验序（数据集→选片→库→
   库∩gt 交集→busy）、每片 3 次尝试、完成序 5 连败中止、取消保留已完成
   片、终态 meta/raw_results + 增量 progress.jsonl 落盘、DB 零写入红线
   （Review Focus 3）。测试一律文件 DB（SQLite 文件库跨线程可见，
   :memory: 每连接独立、工作线程看不到主线程数据）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from conftest import MOCK_EMBEDDER, MOCK_LLM
from tagplatform.closed_eval import (
    ABORT_CONSECUTIVE,
    DEFAULT_CONCURRENCY,
    RETRY_PER_MOVIE,
    ClosedEvalService,
    EvalValidationError,
    PRESET_SEED,
    PRESET_SIZE,
    RunBusyError,
    load_gt,
    load_movie_content,
    movie_entries,
    scan_datasets,
    select_movies,
)
from tagplatform.llm import MockLLMClient


# ---------------------------------------------------------------- 夹具

_UPSTREAM_TEMPLATE = """{{
  "schema_version": "1.0",
  "asset": {{"asset_id": "{tt}", "asset_type": "movie", "title": "{title}"}},
  "content": {{
    "description": "一部测试影片的整体剧情描述。",
    "characters": [{{"character_id": "character_001", "name": "主角"}}],
    "scenes": [
      {{
        "scene_id": "scene_001", "start_ms": 0, "end_ms": 60000,
        "description": "开场对话。",
        "dialogues": [
          {{"dialogue_id": "d1", "speak_id": "character_001", "text": "你好。",
             "start_ms": 1000, "end_ms": 2000}}
        ]
      }}
    ]
  }}
}}
"""


def _write_dataset(root: Path, *, six: bool = False) -> None:
    """内置夹具：迷你 MovieTags 数据集（gt 两份 + 影片内容 JSON）。

    - gt/emotion.csv：三列首行 `movieId,tagId,relevance`，默认 3 影片 ×
      4 标签（t1/t2/t7/t9，与测试库 {t1, t9} 交集恰 2）；six=True 时
      gt 与内容各扩到 6 片（tt0000001-tt0000006）；
    - gt/bad.csv：首行两列——畸形文件，扫描必须跳过而不是崩；
    - data/tt0000001、tt0000002/content.json：上游形态、台词用 `speak_id`；
      默认 tt0000003 只有 gt 分、没有 content.json（→ 不进 usable），
      six=True 时补齐到 6 片（中止/取消用例需要从未启动的余量片）。
    """
    gt = root / "gt"
    gt.mkdir(parents=True)
    gt_ids = ["tt0000001", "tt0000002", "tt0000003"]
    if six:
        gt_ids += ["tt0000004", "tt0000005", "tt0000006"]
    rows = ["movieId,tagId,relevance"]
    for tt in gt_ids:
        for tag, rel in (("t1", "0.9"), ("t2", "0.4"), ("t7", "0.1"),
                         ("t9", "0.8")):
            rows.append(f"{tt},{tag},{rel}")
    (gt / "emotion.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (gt / "bad.csv").write_text("movieId,tagId\ntt0000001,t1\n",
                                encoding="utf-8")
    entries = [("tt0000001", "A Movie"), ("tt0000002", "B Movie")]
    if six:
        entries += [("tt0000003", "C Movie"), ("tt0000004", "D Movie"),
                    ("tt0000005", "E Movie"), ("tt0000006", "F Movie")]
    data = root / "data"
    for tt, title in entries:
        movie_dir = data / tt
        movie_dir.mkdir(parents=True)
        (movie_dir / "content.json").write_text(
            _UPSTREAM_TEMPLATE.format(tt=tt, title=title), encoding="utf-8")


@pytest.fixture
def root(tmp_path):
    """临时数据集根（str）：每测试独立一套迷你 MovieTags。"""
    _write_dataset(tmp_path)
    return str(tmp_path)


# ---------------------------------------------------------------- 扫描

def test_scan_datasets_skips_invalid(root):  # Review Focus 1
    infos = scan_datasets(root, library_tag_ids={"t1", "t9"})
    assert [i.id for i in infos] == ["emotion"]
    assert (infos[0].n_movies, infos[0].n_tags, infos[0].library_overlap) == (3, 4, 2)


def test_scan_datasets_usable_excludes_missing_content(root):
    assert scan_datasets(root, set())[0].usable_movies == ["tt0000001", "tt0000002"]


# ---------------------------------------------------------------- gt 加载

def test_load_gt_unknown_dataset_raises(root):
    with pytest.raises(ValueError):
        load_gt(root, "nope")


def test_load_gt_shape(root):
    # 返回形状 {movieId: {tagId: relevance}}，relevance 是 float
    gt = load_gt(root, "emotion")
    assert set(gt) == {"tt0000001", "tt0000002", "tt0000003"}
    assert gt["tt0000001"] == {"t1": 0.9, "t2": 0.4, "t7": 0.1, "t9": 0.8}
    assert all(isinstance(v, float) for tags in gt.values() for v in tags.values())


def test_gt_cache_rereads_after_mtime_change(root):
    # 进程内缓存按 (path, mtime) 失效：文件变化后复扫必须看到新行数
    assert scan_datasets(root, set())[0].n_movies == 3
    with (Path(root) / "gt" / "emotion.csv").open("a", encoding="utf-8") as f:
        f.write("tt0000009,t1,0.5\n")
    assert scan_datasets(root, set())[0].n_movies == 4


# ---------------------------------------------------------------- 影片加载

def test_load_movie_content_renames_speak_id(root):
    content = load_movie_content(root, "tt0000001")
    assert content.content.scenes[0].dialogues[0].speaker == "character_001"


def test_movie_entries_titles(root):
    entries = movie_entries(root, "emotion")
    assert entries == [{"tt": "tt0000001", "title": "A Movie"},
                       {"tt": "tt0000002", "title": "B Movie"}]


# ---------------------------------------------------------------- 选片

def test_select_movies_preset_deterministic(root):
    a = select_movies(["x1", "x2", "x3"], "preset30")
    b = select_movies(["x3", "x1", "x2"], "preset30")
    assert a == b and len(a) == 3          # 不足 30 → 全取
    assert select_movies(["x1", "x2"], "all") == ["x1", "x2"]
    with pytest.raises(ValueError, match="tt9999"):
        select_movies(["x1"], "custom", ["x1", "tt9999"])


def test_select_movies_sorted_dedup_and_defaults():
    # 契约：返回排序去重列表；常量钉死（试点 30 部 seed=20261120）
    assert select_movies(["b", "a", "b"], "all") == ["a", "b"]
    assert select_movies(["b", "a", "b"], "custom", ["b", "a"]) == ["a", "b"]
    assert PRESET_SIZE == 30 and PRESET_SEED == 20261120


# ---------------------------------------------------------------- 干跑运行器（Task 4 服务层）

# 评测迷你标签库：gt 标签 t1/t2/t7/t9 全部入库（交集非空），挂 content
# 级目录（挂载点恒解析为影片级，resolve_mount 不丢行）
_EVAL_DIRS = [
    {"dir_id": "dir_eval", "parent_id": None, "level": 1, "dir_name": "评测标签",
     "dir_type": "dir_static", "applicable_scope": "content",
     "description": "闭环评测单测用迷你目录", "aliases": []},
]
_EVAL_TAGS = [
    {"tag_id": tag_id, "dir_id": "dir_eval", "tag_name": name,
     "tag_type": "val_fixed", "description": "评测夹具标签", "status": "active"}
    for tag_id, name in (("t1", "悬疑"), ("t2", "动作"),
                         ("t7", "喜剧"), ("t9", "爱情"))
]
# 零交集库（Review Focus 2 夹具）：标签 id 全在 gt 之外
_DISJOINT_TAGS = [
    {"tag_id": "x1", "dir_id": "dir_eval", "tag_name": "零交集甲",
     "tag_type": "val_fixed", "description": "评测夹具标签", "status": "active"},
    {"tag_id": "x2", "dir_id": "dir_eval", "tag_name": "零交集乙",
     "tag_type": "val_fixed", "description": "评测夹具标签", "status": "active"},
]


class _FailMovieLLM(MockLLMClient):
    """失败注入：仅指定影片恒抛错（producer 收到的 spec.run_id 以 :tt 结尾）。

    记录每次抛错的 run_id，供断言重试次数（1 + RETRY_PER_MOVIE 次尝试）。
    """

    def __init__(self, tt: str):
        super().__init__()
        self._suffix = f":{tt}"
        self.failed_calls: list[str] = []

    def complete(self, messages, run_id):
        if run_id.endswith(self._suffix):
            self.failed_calls.append(run_id)
            raise RuntimeError(f"注入失败：{run_id}")
        return super().complete(messages, run_id)


class _FailAllLLM(MockLLMClient):
    """全部调用恒抛错（中止用例：完成序连续 ABORT_CONSECUTIVE 失败）。"""

    def complete(self, messages, run_id):
        raise RuntimeError(f"注入失败：{run_id}")


class _SlowLLM(MockLLMClient):
    """慢 LLM 测试缝隙：每次调用睡 0.1s（起跑 busy/取消用例的时间窗）。"""

    def complete(self, messages, run_id):
        time.sleep(0.1)
        return super().complete(messages, run_id)


@pytest.fixture
def engine(tmp_path):
    """评测服务用文件 DB（建表即播种 closed v1 激活模板——after_create 钩子）。"""
    from tagplatform.db import create_engine_for
    from tagplatform.db.models import Base

    eng = create_engine_for(str(tmp_path / "closed-eval.db"))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def root6(tmp_path):
    """6 片数据集根（中止/取消用例：完成序连败后须有从未启动的余量片）。"""
    _write_dataset(tmp_path / "ds6", six=True)
    return str(tmp_path / "ds6")


@pytest.fixture
def eval_setup(tmp_path, root, engine):
    """服务工厂：文件 DB + 评测标签库（∩ gt 非空）+ 临时 eval 输出根。

    失败/慢速注入经 llm 参数换客户端（包装 conftest 的 MOCK_LLM）；6 片
    用例经 dataset_root 换数据集根。teardown 统一等 worker 收尾再解绑库
    引擎——不让 daemon 线程碰已 dispose 的引擎（输出保整洁）。
    """
    from tagplatform.db import make_session_factory
    from tagplatform.settings import Settings
    from tests.library_fixture import (
        bind_engine,
        library_root,
        unbind_engine,
        write_library,
    )

    bind_engine(engine)
    write_library(library_root(), dirs=_EVAL_DIRS, tags=_EVAL_TAGS)
    made: list[ClosedEvalService] = []

    def _make(llm=MOCK_LLM, dataset_root=None) -> ClosedEvalService:
        service = ClosedEvalService(
            make_session_factory(engine), llm, MOCK_EMBEDDER,
            Settings(eval_dataset_root=dataset_root or root,
                     eval_output_root=str(tmp_path / "eval-out")))
        made.append(service)
        return service

    yield _make
    for service in made:
        _drain_active(service)
    unbind_engine(engine)


@pytest.fixture
def svc(eval_setup):
    """默认服务：MOCK_LLM + root（2 片）数据集 + 临时 eval 输出根。"""
    return eval_setup()


def _run_dir(service, run_id) -> Path:
    return Path(service.settings.eval_output_root) / run_id


def _raw_rows(service, run_id) -> list[dict]:
    return json.loads((_run_dir(service, run_id) / "raw_results.json")
                      .read_text(encoding="utf-8"))


def _read_meta(service, run_id) -> dict:
    return json.loads((_run_dir(service, run_id) / "meta.json")
                      .read_text(encoding="utf-8"))


def _await_terminal(service, run_id, timeout=15.0):
    """轮询 get() 至终态（取消后 worker 还要收尾在途片，需给它时间）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service.get(run_id)
        if state is not None and state.status != "running":
            return state
        time.sleep(0.01)
    pytest.fail(f"run {run_id} 未在 {timeout}s 内到达终态")


def _drain_active(service, timeout=15.0):
    """teardown 用：等活跃 run 完全收尾（worker 退出、busy 位释放）。"""
    deadline = time.monotonic() + timeout
    while service.active_run_id() is not None and time.monotonic() < deadline:
        time.sleep(0.01)


def test_runner_constants():
    # 契约钉死：默认并发 / 每片重试（含首发共 3 次尝试）/ 完成序连败中止阈值
    assert DEFAULT_CONCURRENCY == 4
    assert RETRY_PER_MOVIE == 2
    assert ABORT_CONSECUTIVE == 5


def test_run_completes_and_writes_artifacts(svc, root):
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    assert svc.get(run_id).status == "succeeded"
    d = Path(svc.settings.eval_output_root) / run_id
    assert (d / "meta.json").exists() and (d / "raw_results.json").exists()
    rows = json.loads((d / "raw_results.json").read_text(encoding="utf-8"))
    assert len(rows) == 2 and all(r["ok"] for r in rows)
    # raw 行契约：gt 向量并入（含无内容片的满表向量），produced 行五键齐全
    assert {r["tt"] for r in rows} == {"tt0000001", "tt0000002"}
    for r in rows:
        assert r["gt"] == {"t1": 0.9, "t2": 0.4, "t7": 0.1, "t9": 0.8}
        for p in r["produced"]:
            assert set(p) == {"tag_id", "tag_name", "sf", "confidence",
                              "evidence_count"}
    # 增量进度：每片一行（锁内追加）
    lines = (d / "progress.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    entries = [json.loads(line) for line in lines]
    assert all(e["ok"] and {"tt", "produced", "elapsed_ms"} <= set(e)
               for e in entries)
    # meta 口径快照（dataset/library/prompt/模型/选片/终态计数）
    meta = _read_meta(svc, run_id)
    assert meta["dataset"]["id"] == "emotion"
    assert meta["dataset"]["n_rows"] == 12  # 3 片 × 4 标签（gt 全表，含无内容片）
    assert len(meta["dataset"]["gt_sha8"]) == 8
    assert meta["prompt"] == {"mode": "closed", "version": 1}
    assert meta["library"]["n_dirs"] == 1 and meta["library"]["n_tags"] == 4
    assert meta["llm_model"] == "mock-1" and meta["embedding_model"] == "mock"
    assert meta["movies"] == {"mode": "all", "ids": ["tt0000001", "tt0000002"]}
    assert (meta["status"], meta["done"], meta["total"],
            meta["failed"]) == ("succeeded", 2, 2, 0)


def test_db_zero_writes(engine, svc, root):  # Review Focus 3
    svc.start("emotion", {"mode": "all"}, wait=True)
    with engine.connect() as con:
        for table in ("media_asset", "pre_asset_tag", "tag_generation_task",
                      "asset_build_task", "tag_quality_evaluation"):
            assert con.execute(text(f"select count(*) from {table}")).scalar() == 0


def test_intersection_empty_rejected(svc, root):  # Review Focus 2
    from tests.library_fixture import library_root, write_library

    write_library(library_root(), dirs=_EVAL_DIRS, tags=_DISJOINT_TAGS)
    with pytest.raises(EvalValidationError):
        svc.start("emotion", {"mode": "all"}, wait=True)
    assert svc.active_run_id() is None  # 校验失败不占 busy 位


def test_single_movie_failure_records_and_continues(eval_setup, root):
    failer = _FailMovieLLM("tt0000002")
    service = eval_setup(failer)
    run_id = service.start("emotion", {"mode": "all"}, wait=True)
    state = service.get(run_id)
    assert state.status == "succeeded" and state.failed == 1
    rows = _raw_rows(service, run_id)
    bad = next(r for r in rows if r["tt"] == "tt0000002")
    assert bad["ok"] is False and bad["error"]
    good = next(r for r in rows if r["tt"] == "tt0000001")
    assert good["ok"] is True
    # 重试语义钉死：失败片共 1 + RETRY_PER_MOVIE 次尝试（每次首发调用即抛）
    assert (sum(1 for r in failer.failed_calls
                if r.endswith(":tt0000002")) == 1 + RETRY_PER_MOVIE)
    # 失败片的进度行带 error 键（增量落盘里失败也可见）
    entries = [json.loads(line) for line in
               (_run_dir(service, run_id) / "progress.jsonl")
               .read_text(encoding="utf-8").splitlines()]
    assert "error" in next(e for e in entries if e["tt"] == "tt0000002")
    assert "error" not in next(e for e in entries if e["tt"] == "tt0000001")


def test_six_movies_full_run_default_concurrency(eval_setup, root6):
    # 默认并发 4 × 6 片：滑窗补位提交两波（131 部全量评测的实际路径）
    service = eval_setup(dataset_root=root6)
    run_id = service.start("emotion", {"mode": "all"}, wait=True)
    state = service.get(run_id)
    assert state.status == "succeeded"
    assert state.done == state.total == 6 and state.failed == 0
    assert len(_raw_rows(service, run_id)) == 6


def test_consecutive_failures_abort(eval_setup, root6):
    service = eval_setup(_FailAllLLM(), dataset_root=root6)
    run_id = service.start("emotion", {"mode": "all"}, concurrency=1, wait=True)
    state = service.get(run_id)
    assert state.status == "aborted"
    assert state.done < state.total
    # 滑动提交：完成序 5 连败即停，第 6 部从未进池（并发 1 下确定）
    assert state.done == ABORT_CONSECUTIVE
    assert state.failed == ABORT_CONSECUTIVE
    assert len(_raw_rows(service, run_id)) == ABORT_CONSECUTIVE
    meta = _read_meta(service, run_id)
    assert meta["status"] == "aborted" and meta["done"] < meta["total"]


def test_busy_run_rejects(eval_setup, root):
    service = eval_setup(_SlowLLM())
    r1 = service.start("emotion", {"mode": "all"})  # wait=False
    try:
        assert service.active_run_id() == r1
        with pytest.raises(RunBusyError):
            service.start("emotion", {"mode": "all"})
    finally:
        assert service.cancel(r1) is True
    _await_terminal(service, r1)  # 在途片跑完再退出（teardown 不碰活线程）


def test_cancel_keeps_completed_results(eval_setup, root6):
    service = eval_setup(_SlowLLM(), dataset_root=root6)
    run_id = service.start("emotion", {"mode": "all"}, concurrency=1)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and service.get(run_id).done < 1:
        time.sleep(0.01)
    assert service.get(run_id).done >= 1
    assert service.cancel(run_id) is True
    state = _await_terminal(service, run_id)
    assert state.status == "cancelled"
    rows = _raw_rows(service, run_id)
    assert len(rows) < state.total
    assert rows and rows[0]["ok"] is True and rows[0]["gt"]  # 已完成片保留
    meta = _read_meta(service, run_id)
    assert meta["status"] == "cancelled" and meta["done"] < meta["total"]
