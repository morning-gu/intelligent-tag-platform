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
   :memory: 每连接独立、工作线程看不到主线程数据）；
6. **报告组装 + 历史扫描（Task 5）**——assemble_report 的格值与
   `evaluate_closed` 直算同数（服务层只做样本构造）、先验基线钉死
   （tag_universe 按 gt 全影片均值降序）、universe 过滤（交集外条目
   不计分只计 no_gt）、部分运行 banner / 失败率警示、report 三件套
   落盘、history 磁盘扫描 + interrupted 推断 + 读侧容错（Review
   Focus 4/5）。
"""

from __future__ import annotations

import json
import threading
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
    MovieResult,
    PRESET_SEED,
    PRESET_SIZE,
    RunActiveError,
    RunBusyError,
    assemble_report,
    load_gt,
    load_movie_content,
    movie_entries,
    render_report_md,
    scan_datasets,
    select_movies,
    write_report,
)
from tagplatform.eval.closed_ranking import ClosedSample, evaluate_closed
from tagplatform.capabilities.llm import MockLLMClient


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


class _CountingSessionFactory:
    """会话计数工厂（池饿死回归探针）：包装真实工厂，按线程计进出。

    逐线程归属是判据的正确口径（终审 Important）：池饿死事故的本质是
    「**本线程**的 produce（含 LLM 调用）持有**自己的**连接」；跨线程的
    毫秒级 assemble 会话重叠是合法并发，按全局计数会把正确实现也判红
    （评审者实测：逐线程恒 0、全局口径恒 4 假阳性）。锁内维护
    open_by_thread 与全局开放会话峰值（峰值合法性 ≤ concurrency）。
    """

    def __init__(self, inner):
        self._inner = inner
        self._lock = threading.Lock()
        self.open_by_thread: dict[int, int] = {}
        self.peak_open = 0

    def __call__(self):
        return _CountingSession(self._inner(), self)

    def _enter(self) -> None:
        tid = threading.get_ident()
        with self._lock:
            n = self.open_by_thread.get(tid, 0) + 1
            self.open_by_thread[tid] = n
            self.peak_open = max(self.peak_open,
                                 sum(self.open_by_thread.values()))

    def _exit(self) -> None:
        tid = threading.get_ident()
        with self._lock:
            n = self.open_by_thread.get(tid, 0) - 1
            if n > 0:
                self.open_by_thread[tid] = n
            else:
                self.open_by_thread.pop(tid, None)

    def held_by_current_thread(self) -> int:
        with self._lock:
            return self.open_by_thread.get(threading.get_ident(), 0)


class _CountingSession:
    """计数会话：属性透传内层（assemble_spec 内服务级 DB 读取可用）。"""

    def __init__(self, inner, factory):
        self._inner = inner
        self._factory = factory

    def __enter__(self):
        self._factory._enter()
        return self._inner

    def __exit__(self, *exc):
        self._factory._exit()
        return self._inner.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _SessionProbeLLM(MockLLMClient):
    """LLM 桩：complete() 时记**本线程**持有的会话数（violation 计数，
    不中断跑——跨线程合法重叠不计，见 _CountingSessionFactory 口径注）。"""

    def __init__(self, factory):
        super().__init__()
        self._factory = factory
        self.violations = 0

    def complete(self, messages, run_id):
        if self._factory.held_by_current_thread() > 0:
            self.violations += 1
        return super().complete(messages, run_id)


@pytest.fixture
def engine(tmp_path):
    """评测服务用文件 DB（四层重构 Task 3 起 prompt 本地化：create_all
    不再播种 prompt_template——after_create 钩子随本地化退役）。"""
    from tagplatform.capabilities.storage import create_engine_for
    from tagplatform.capabilities.storage.models import Base

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
    from tagplatform.capabilities.storage import make_session_factory
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

    def _make(llm=MOCK_LLM, dataset_root=None,
              session_factory=None) -> ClosedEvalService:
        service = ClosedEvalService(
            session_factory or make_session_factory(engine), llm, MOCK_EMBEDDER,
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


def test_llm_calls_hold_zero_db_sessions(eval_setup, engine):
    """池饿死事故回归（2026-11-21 run-081903，并发 16 > 池 5+10）：
    LLM 调用线程自身不得持有 DB 会话——produce 含全部 LLM 调用（分钟级），
    会话横跨它就会在高并发下占死 QueuePool、拖垮全部平台 API。
    判据逐线程归属（跨线程毫秒级 assemble 重叠是合法并发）；另断言全局
    开放会话峰值不超过并发上限（合法并发的上界）。"""
    from tagplatform.capabilities.storage import make_session_factory

    factory = _CountingSessionFactory(make_session_factory(engine))
    llm = _SessionProbeLLM(factory)
    service = eval_setup(llm=llm, session_factory=factory)
    run_id = service.start("emotion", {"mode": "all"}, wait=True)
    assert service.get(run_id).status == "succeeded"
    assert service.get(run_id).failed == 0
    assert llm.violations == 0
    assert factory.peak_open <= DEFAULT_CONCURRENCY


def test_finished_at_on_terminal_state_and_history(svc):
    """终态后：get() 快照与 history() 行都带 finished_at（ISO 可解析）。

    meta.json 终态写盘本就带 finished_at；本用例钉的是注册表 RunState 与
    history() 摘要行的透出（运行中/老 run 无此字段 → None，宁缺勿谎）。
    """
    from datetime import datetime

    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    state = svc.get(run_id)
    assert state.finished_at is not None
    datetime.fromisoformat(state.finished_at)  # ISO 形态
    row = next(r for r in svc.history() if r["run_id"] == run_id)
    assert row["finished_at"] is not None


def test_delete_terminal_run_removes_disk_and_registry(svc):
    """删除终态 run：磁盘 run 目录、注册表、历史列表三处一并清除。"""
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    _drain_active(svc)  # worker 完全收尾（active 位释放）
    assert svc.delete(run_id) is True
    assert svc.get(run_id) is None
    assert not _run_dir(svc, run_id).exists()
    assert all(r["run_id"] != run_id for r in svc.history())


def test_delete_active_run_refused(eval_setup):
    """运行中不可删（active 位守卫——worker 在跑或在收尾落盘均算活跃）。"""
    gate = _RecordingGateLLM()
    gate.hold()
    service = eval_setup(llm=gate)
    run_id = service.start("emotion", {"mode": "all"})
    try:
        with pytest.raises(RunActiveError):
            service.delete(run_id)
        assert _run_dir(service, run_id).exists()
    finally:
        gate.release()
    _await_terminal(service, run_id)
    _drain_active(service)


def test_delete_unknown_returns_false(svc):
    assert svc.delete("run-nope") is False


def test_delete_interrupted_disk_residual(svc):
    """进程死亡残留（磁盘 status=running、注册表无）= interrupted，可删。"""
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    with svc._lock:
        svc._runs.pop(run_id)
    assert svc.delete(run_id) is True
    assert not _run_dir(svc, run_id).exists()


def test_delete_path_traversal_returns_false(svc):
    """run_id 路径越界形态（../）按不存在处理，绝不对输出根外动手。"""
    assert svc.delete("../outside") is False


def test_delete_dot_and_nested_rejected(svc):
    """「解析后等于根」与嵌套段形态按不存在处理（终审 Critical）：
    delete(".") 曾把整个 eval_output_root 连同全部 run 目录 rmtree 清空。"""
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    _drain_active(svc)
    assert svc.delete(".") is False
    assert svc.delete("a/b") is False
    # 爆炸半径核验：输出根与已完成 run 均完好（守卫放行即全灭）
    root_dir = Path(svc.settings.eval_output_root)
    assert root_dir.exists()
    assert _run_dir(svc, run_id).exists()


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


# ---------------------------------------------------------------- 报告组装（Task 5）

_GT2 = {
    "m1": {"t1": 0.9, "t2": 0.5, "t3": 0.1},
    "m2": {"t1": 0.2, "t2": 0.8, "t3": 0.6},
    "m3": {"t5": 0.7},  # gt∩universe 为空 → invalid 样本
}
_UNIVERSE = ["t1", "t2", "t3"]


def _movie(tt, produced, *, ok=True, error=None):
    """手构 MovieResult（报告组装单测不需要服务/DB/LLM）。"""
    return MovieResult(tt=tt, title=f"title-{tt}", ok=ok, error=error,
                       produced=produced, elapsed_ms=1)


def _prow(tag_id, sf):
    """produced 行（五键契约；sf 可乱序——assemble 需防御性再排）。"""
    return {"tag_id": tag_id, "tag_name": f"名-{tag_id}", "sf": sf,
            "confidence": 0.5, "evidence_count": 1}


def _meta(**extra):
    base = {"status": "succeeded", "done": 3, "total": 3, "failed": 0}
    base.update(extra)
    return base


def test_report_grid_matches_metrics_module():
    # 同一批 MovieResult 喂 assemble_report，与手工构造 ClosedSample 喂
    # evaluate_closed：格值必须同数（服务层只做样本构造，指标只属于指标模块）
    results = [
        _movie("m1", [_prow("t1", 0.9), _prow("t2", 0.5)]),
        _movie("m2", [_prow("t2", 0.8), _prow("t1", 0.2), _prow("t3", 0.55)]),  # 乱序
        _movie("m3", [_prow("t5", 0.8)]),  # t5 交集外 → no_gt；gt∩universe 空 → invalid
    ]
    report = assemble_report(results, _GT2, _UNIVERSE, (0.5,), (5,),
                             meta=_meta())
    samples = [
        ClosedSample(ranked=("t1", "t2"), relevance=_GT2["m1"],
                     system_scores={"t1": 0.9, "t2": 0.5}, label="m1"),
        ClosedSample(ranked=("t2", "t3", "t1"), relevance=_GT2["m2"],
                     system_scores={"t2": 0.8, "t1": 0.2, "t3": 0.55}, label="m2"),
        ClosedSample(ranked=(), relevance={}, system_scores={}, label="m3"),
    ]
    expected = evaluate_closed(samples, thetas=(0.5,), ks=(5,))
    cell = next(g for g in report["grid"]
                if g["theta"] == 0.5 and g["k"] == 5)
    exp = expected.grid[0]
    assert cell["precision"] == pytest.approx(exp.precision)
    assert cell["recall"] == pytest.approx(exp.recall)
    assert cell["ndcg"] == pytest.approx(exp.ndcg)
    assert cell["precision_random"] == pytest.approx(exp.precision_random)
    assert cell["recall_random"] == pytest.approx(exp.recall_random)
    # 聚合侧同口径：coverage/Spearman 与 ClosedReport 一致；核算含 invalid/no_gt
    assert report["coverage"]["median"] == pytest.approx(expected.coverage_median)
    assert report["spearman"]["macro"] == pytest.approx(expected.spearman_macro)
    assert report["sample_accounting"] == {"total": 3, "ok": 3, "failed": 0,
                                           "invalid": 1, "no_gt": 1}


def test_prior_baseline_present():
    # 先验序钉死：tag_universe 按 gt 全影片均值降序（并列 tag_id 字典序）。
    # 均值（gt 全表）：t1=0.55、t2=0.65、t3=0.35 → 先验序 (t2, t1, t3)
    results = [
        _movie("m1", [_prow("t1", 0.9)]),
        _movie("m2", [_prow("t3", 0.3)]),
    ]
    report = assemble_report(results, _GT2, _UNIVERSE, (0.5,), (5,),
                             meta=_meta(done=2, total=2))
    prior = report["baselines"]["prior"]
    assert prior  # 非空：先验样本与系统同机制算出的完整网格
    prior_samples = [
        ClosedSample(ranked=("t2", "t1", "t3"), relevance=_GT2[m],
                     system_scores={"t2": 0.65, "t1": 0.55, "t3": 0.35},
                     label=m)
        for m in ("m1", "m2")
    ]
    expected = evaluate_closed(prior_samples, thetas=(0.5,), ks=(5,))
    cell = next(g for g in prior if g["theta"] == 0.5 and g["k"] == 5)
    exp = expected.grid[0]
    assert cell["precision"] == pytest.approx(exp.precision)
    assert cell["recall"] == pytest.approx(exp.recall)
    assert cell["ndcg"] == pytest.approx(exp.ndcg)


def test_universe_filtering_no_gt_counted():  # spec §5 保护原则
    # 产出行含 tag_universe 外的标签（且 sf 更高）：该行不进 ranked
    # （P 值与"从未产出过它"完全相同——白给 precision/冤枉 recall 都不要），
    # 只计入 sample_accounting.no_gt
    with_outside = [_movie("m1", [_prow("tX", 0.95), _prow("t1", 0.9)])]
    without = [_movie("m1", [_prow("t1", 0.9)])]
    meta = _meta(done=1, total=1)
    a = assemble_report(with_outside, _GT2, _UNIVERSE, (0.5,), (5,), meta=meta)
    b = assemble_report(without, _GT2, _UNIVERSE, (0.5,), (5,), meta=meta)
    assert a["sample_accounting"]["no_gt"] == 1
    assert b["sample_accounting"]["no_gt"] == 0
    assert a["grid"] == b["grid"]  # tX 不占 K 位、不进任何指标
    assert a["coverage"] == b["coverage"]
    assert a["spearman"] == b["spearman"]


def test_partial_run_banner():  # Review Focus 5
    results = [_movie("m1", [_prow("t1", 0.9)])]
    cancelled = assemble_report(results, _GT2, _UNIVERSE, (0.5,), (5,),
                                meta=_meta(status="cancelled", done=1, total=2))
    assert cancelled["banner"] is not None and "部分运行" in cancelled["banner"]
    # 样本核算响亮：成功 1 / 总 2（不允许悄悄缩水）
    acc = cancelled["sample_accounting"]
    assert acc["ok"] < acc["total"] and (acc["total"], acc["ok"]) == (2, 1)
    # succeeded 但 done<total（取消/中止后仍有未跑余量）同样不可横比
    shrank = assemble_report(results, _GT2, _UNIVERSE, (0.5,), (5,),
                             meta=_meta(done=1, total=2))
    assert shrank["banner"] is not None
    # 全量成功 → 无 banner
    full = assemble_report([_movie("m1", [_prow("t1", 0.9)]),
                            _movie("m2", [_prow("t2", 0.8)])],
                           _GT2, _UNIVERSE, (0.5,), (5,),
                           meta=_meta(done=2, total=2))
    assert full["banner"] is None


def test_failure_rate_warning():
    gt10 = {f"m{i}": dict(_GT2["m1"]) for i in range(10)}
    results = ([_movie(f"m{i}", [_prow("t1", 0.9)]) for i in range(8)]
               + [_movie("m8", [], ok=False, error="RuntimeError: 注入甲"),
                  _movie("m9", [], ok=False, error="RuntimeError: 注入乙")])
    report = assemble_report(results, gt10, _UNIVERSE, (0.5,), (5,),
                             meta=_meta(done=10, total=10, failed=2))
    assert report["warning"] is not None  # 2/10 = 20% > 10%
    assert report["failed_movies"] == [
        {"tt": "m8", "error": "RuntimeError: 注入甲"},
        {"tt": "m9", "error": "RuntimeError: 注入乙"}]
    clean = assemble_report(results[:8], gt10, _UNIVERSE, (0.5,), (5,),
                            meta=_meta(done=8, total=8))
    assert clean["warning"] is None and clean["failed_movies"] == []


def test_report_md_and_csv_written(tmp_path):
    results = [_movie("m1", [_prow("t1", 0.9), _prow("t2", 0.5)]),
               _movie("m2", [_prow("t2", 0.8), _prow("t3", 0.55)])]
    meta = {"status": "succeeded", "done": 2, "total": 2, "failed": 0,
            "llm_model": "mock-1", "embedding_model": "mock",
            "prompt": {"mode": "closed", "version": 1},
            "library": {"version": 7, "n_dirs": 1, "n_tags": 4},
            "dataset": {"id": "emotion", "gt_sha8": "abcd1234"}}
    report = assemble_report(results, _GT2, _UNIVERSE, (0.5,), (5,), meta=meta)
    assert report["calibre"] == {"llm_model": "mock-1",
                                 "embedding_model": "mock",
                                 "prompt_version": 1, "library_version": 7,
                                 "dataset_id": "emotion", "gt_sha8": "abcd1234"}
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    write_report(run_dir, report)
    assert all((run_dir / name).exists()
               for name in ("report.json", "report.md", "metrics.csv"))
    md = (run_dir / "report.md").read_text(encoding="utf-8")
    assert md == render_report_md(report)
    assert "| θ |" in md and "银标准" in md and "mock-1" in md
    lines = (run_dir / "metrics.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "theta,k,metric,macro,ci_low,ci_high,random_baseline"
    assert len(lines) - 1 == 3  # 格数（θ1×K1）× 指标数（P/R/nDCG）
    assert {line.split(",")[2] for line in lines[1:]} == {
        "precision", "recall", "ndcg"}


# ---------------------------------------------------------------- 历史扫描（Task 5）

def test_history_scans_disk_and_infers_interrupted(svc):  # Review Focus 4
    out = Path(svc.settings.eval_output_root)
    assert svc.history() == []  # 输出根不存在 → []
    # run-old：终态 + 报告齐全（统一取格约定的 θ0.2/θ0.8 档 + θ0.4 行在）
    old = out / "run-old"
    old.mkdir(parents=True)
    (old / "meta.json").write_text(json.dumps({
        "run_id": "run-old", "status": "succeeded", "done": 2, "total": 2,
        "failed": 0, "started_at": "2026-11-19T10:00:00+00:00",
        "dataset": {"id": "emotion"}}, ensure_ascii=False), encoding="utf-8")
    (old / "report.json").write_text(json.dumps({
        "grid": [
            {"theta": 0.2, "k": 10, "n_valid": 2, "precision": 0.55,
             "recall": 0.65, "ndcg": 0.75, "precision_random": 0.2,
             "recall_random": 0.4},
            {"theta": 0.8, "k": 20, "n_valid": 2, "precision": 0.3,
             "recall": 0.45, "ndcg": 0.5, "precision_random": 0.1,
             "recall_random": 0.5},
            {"theta": 0.4, "k": 10, "n_valid": 2, "precision": 0.5,
             "recall": 0.6, "ndcg": 0.7, "precision_random": 0.2,
             "recall_random": 0.4},
            {"theta": 0.4, "k": 20, "n_valid": 2, "precision": 0.4,
             "recall": 0.5, "ndcg": 0.6, "precision_random": 0.2,
             "recall_random": 0.9},
        ],
        "coverage": {"min": 0.1, "median": 0.8, "max": 0.9,
                     "values": [0.8]},
    }, ensure_ascii=False), encoding="utf-8")
    # run-new：meta 残留 running 且不在进程内注册表 → interrupted
    new = out / "run-new"
    new.mkdir()
    (new / "meta.json").write_text(json.dumps({
        "run_id": "run-new", "status": "running", "done": 1, "total": 3,
        "failed": 0, "started_at": "2026-11-20T10:00:00+00:00",
        "dataset": {"id": "emotion"}}, ensure_ascii=False), encoding="utf-8")
    # run-wrongtheta：报告只有 θ=0.6 档（custom-θ run）→ 统一取格回退
    # 最近有值格：p10/ndcg10 取 0.6 格、r20 无 K=20 行 → None（覆盖率照读）
    wrong = out / "run-wrongtheta"
    wrong.mkdir()
    (wrong / "meta.json").write_text(json.dumps({
        "run_id": "run-wrongtheta", "status": "succeeded", "done": 1,
        "total": 1, "failed": 0, "started_at": "2026-11-18T10:00:00+00:00",
        "dataset": {"id": "emotion"}}, ensure_ascii=False), encoding="utf-8")
    (wrong / "report.json").write_text(json.dumps({
        "grid": [{"theta": 0.6, "k": 10, "precision": 0.9, "recall": 0.9,
                  "ndcg": 0.9}],
        "coverage": {"median": 0.5}}, ensure_ascii=False), encoding="utf-8")
    # run-trunc：report.json 截断（meta→raw→report 写序的非原子窗口）
    # → 按"该 run 无报告"处理，不抛异常
    trunc = out / "run-trunc"
    trunc.mkdir()
    (trunc / "meta.json").write_text(json.dumps({
        "run_id": "run-trunc", "status": "succeeded", "done": 1, "total": 1,
        "failed": 0, "started_at": "2026-11-17T10:00:00+00:00",
        "dataset": {"id": "emotion"}}, ensure_ascii=False), encoding="utf-8")
    (trunc / "report.json").write_text('{"grid": [ 截断', encoding="utf-8")

    rows = svc.history()
    # started_at 降序；状态推断与摘要容错一次钉死
    assert [r["run_id"] for r in rows] == ["run-new", "run-old",
                                           "run-wrongtheta", "run-trunc"]
    assert rows[0]["status"] == "interrupted"
    assert rows[0]["ndcg10"] is None and rows[0]["r20"] is None
    assert rows[1]["status"] == "succeeded"
    # 统一取格：p10/ndcg10 @θ0.2、r20 @θ0.8（都精确存在）
    assert (rows[1]["ndcg10"], rows[1]["p10"], rows[1]["r20"],
            rows[1]["coverage_median"]) == (0.75, 0.55, 0.45, 0.8)
    assert (rows[2]["ndcg10"], rows[2]["p10"], rows[2]["r20"]) == (
        0.9, 0.9, None)  # custom-θ 回退：K=10 落 0.6 格、无 K=20 行
    assert rows[2]["coverage_median"] == 0.5  # 覆盖率与 θ 无关，照常读出
    assert rows[3]["ndcg10"] is None and rows[3]["coverage_median"] is None
    assert all(r["dataset_id"] == "emotion" for r in rows)


def test_history_summary_theta_row_empty_falls_back_or_none(svc):  # Review Focus 2
    """统一取格的「有值格」判定：目标 θ 行的格值为 null（该 θ 无有效影片）
    → 不算有值，回退最近有值档；整档无值 → None（宁缺勿假）。"""
    out = Path(svc.settings.eval_output_root)
    # run-emptyrow：θ0.2 行 K=10 格全 null、θ0.8 行 K=20 格 null → 回退
    # θ0.4 有值格；K=20 只有 θ0.4 行有值 → r20 也回退 0.4
    d = out / "run-emptyrow"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "run_id": "run-emptyrow", "status": "succeeded", "done": 2, "total": 2,
        "failed": 0, "started_at": "2026-11-19T10:00:00+00:00",
        "dataset": {"id": "emotion"}}, ensure_ascii=False), encoding="utf-8")
    (d / "report.json").write_text(json.dumps({
        "grid": [
            {"theta": 0.2, "k": 10, "n_valid": 0, "precision": None,
             "recall": None, "ndcg": None},
            {"theta": 0.8, "k": 20, "n_valid": 0, "precision": None,
             "recall": None, "ndcg": None},
            {"theta": 0.4, "k": 10, "n_valid": 2, "precision": 0.3,
             "recall": 0.4, "ndcg": 0.4},
            {"theta": 0.4, "k": 20, "n_valid": 2, "precision": 0.2,
             "recall": 0.25, "ndcg": 0.3},
        ]}, ensure_ascii=False), encoding="utf-8")
    # run-allnull：四行全 null → 全 None（不取无值格的「θ」也不取值）
    d2 = out / "run-allnull"
    d2.mkdir(parents=True)
    (d2 / "meta.json").write_text(json.dumps({
        "run_id": "run-allnull", "status": "succeeded", "done": 2, "total": 2,
        "failed": 0, "started_at": "2026-11-18T10:00:00+00:00",
        "dataset": {"id": "emotion"}}, ensure_ascii=False), encoding="utf-8")
    (d2 / "report.json").write_text(json.dumps({
        "grid": [
            {"theta": 0.2, "k": 10, "n_valid": 0, "precision": None,
             "recall": None, "ndcg": None},
            {"theta": 0.4, "k": 10, "n_valid": 0, "precision": None,
             "recall": None, "ndcg": None},
            {"theta": 0.8, "k": 20, "n_valid": 0, "precision": None,
             "recall": None, "ndcg": None},
            {"theta": 0.4, "k": 20, "n_valid": 0, "precision": None,
             "recall": None, "ndcg": None},
        ]}, ensure_ascii=False), encoding="utf-8")

    rows = svc.history()
    by_id = {r["run_id"]: r for r in rows}
    empty = by_id["run-emptyrow"]
    assert (empty["ndcg10"], empty["p10"], empty["r20"]) == (
        0.4, 0.3, 0.25)  # θ0.2/0.8 行无值 → 回退 θ0.4 有值格
    allnull = by_id["run-allnull"]
    assert (allnull["ndcg10"], allnull["p10"], allnull["r20"]) == (
        None, None, None)  # 全 null → None（宁缺勿假）


def test_service_end_to_end_writes_report(svc, root):
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    d = _run_dir(svc, run_id)
    report = json.loads((d / "report.json").read_text(encoding="utf-8"))
    # 前端契约键齐全（Task 7 消费）
    assert {"banner", "sample_accounting", "grid", "baselines", "coverage",
            "spearman", "failed_movies", "warning", "calibre", "note"} \
        <= set(report)
    assert report["banner"] is None and report["warning"] is None
    assert report["sample_accounting"]["total"] == 2
    assert report["sample_accounting"]["ok"] == 2
    assert report["sample_accounting"]["no_gt"] == 0  # 库标签全在 gt 内
    assert report["baselines"]["prior"]  # 先验基线随 run 落盘
    assert report["calibre"]["llm_model"] == "mock-1"
    assert report["calibre"]["dataset_id"] == "emotion"
    assert len(report["calibre"]["gt_sha8"]) == 8
    assert "银标准" in report["note"]
    assert (d / "report.md").exists() and (d / "metrics.csv").exists()
    # 历史扫描随即能看到该 run（含 θ0.2/θ0.8、K=10/20 摘要档）
    mine = next(r for r in svc.history() if r["run_id"] == run_id)
    assert mine["status"] == "succeeded" and mine["ndcg10"] is not None


# ---------------------------------------------------------------- 数据集根快照（Task 2）


class _RecordingGateLLM(MockLLMClient):
    """记录型 + 闸门 LLM：complete 留存完整 messages（断言懒加载来源），
    hold 后阻塞调用（钉运行中切根的时序）；10s 未放行自恢复防挂死。"""

    def __init__(self) -> None:
        super().__init__()
        self._gate = threading.Event()
        self._gate.set()
        self.prompts: list[str] = []

    def hold(self) -> None:
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    def complete(self, messages, run_id):
        self._gate.wait(10)
        self.prompts.append(json.dumps(messages, ensure_ascii=False))
        return super().complete(messages, run_id)


def _mark_scenes(root: Path, marker: str) -> None:
    """把根内所有 content.json 的场景描述打上标记（断言懒加载来源）。"""
    for movie_dir in sorted((root / "data").iterdir()):
        path = movie_dir / "content.json"
        text_ = path.read_text(encoding="utf-8")
        path.write_text(text_.replace("开场对话。", f"开场对话。{marker}"),
                        encoding="utf-8")


def test_run_with_override_uses_override_root(tmp_path, eval_setup):
    """带 override 起跑：worker 懒加载必须来自 override 根（起跑快照）。

    现行缺陷（Task 2 要修）：start 用 effective_root 选片/校验，
    _run_movie 却从 settings.eval_dataset_root 加载 content——两根不一致
    时选片与产出张冠李戴。
    """
    root_a, root_b = tmp_path / "ds-a", tmp_path / "ds-b"
    root_a.mkdir()
    root_b.mkdir()
    _write_dataset(root_a)
    _write_dataset(root_b)
    _mark_scenes(root_a, "MARKER_A")
    _mark_scenes(root_b, "MARKER_B")
    llm = _RecordingGateLLM()
    svc = eval_setup(llm=llm, dataset_root=str(root_a))
    svc.dataset_root_override = str(root_b)
    svc.start("emotion", {"mode": "all"}, wait=True)
    blob = "".join(llm.prompts)
    assert "MARKER_B" in blob
    assert "MARKER_A" not in blob


def test_root_switch_mid_run_uses_snapshot(tmp_path, eval_setup):
    """运行中切根：在跑 run 的余量片继续按起跑根加载（spec 隔离红线守卫）。

    concurrency=1 + 6 片：hold 首调用期间切 override，后续 5 片在切根后
    才懒加载——必须仍来自起跑根（快照），不得读运行时 effective_root。
    """
    root_a, root_b = tmp_path / "ds-a2", tmp_path / "ds-b2"
    root_a.mkdir()
    root_b.mkdir()
    _write_dataset(root_a, six=True)
    _write_dataset(root_b, six=True)
    _mark_scenes(root_a, "MARKER_A")
    _mark_scenes(root_b, "MARKER_B")
    llm = _RecordingGateLLM()
    svc = eval_setup(llm=llm, dataset_root=str(root_a))
    llm.hold()
    svc.start("emotion", {"mode": "all"}, concurrency=1)  # 不等：首片阻塞在闸上
    svc.dataset_root_override = str(root_b)
    llm.release()
    _drain_active(svc)
    blob = "".join(llm.prompts)
    assert "MARKER_A" in blob
    assert "MARKER_B" not in blob
