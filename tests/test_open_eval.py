"""open_eval 单测：OpenEvalService 干跑运行器的口径钉死（Task 4）。

夹具镜像 tests/test_closed_eval.py（迷你 MovieTags 数据集 + 文件 DB + 迷你
评测标签库），runner 契约测试不跑真管线：monkeypatch
`open_service.load_assembly_context` 把 producer 换成 FakeOpenProducer
（canned ProducedTag + 镜像真管线的审计落盘契约），管线行为已有
test_generation_open.py 覆盖；评审层同样用替身（_ReviewOkStub /
_ReviewFailStub）替换 `svc._review_client`——make_review_client 的
选择逻辑已有 test_eval_open_review.py 覆盖。

钉死口径：

1. 跑完写盘六件 + audit/（meta.json / progress.jsonl / raw_results.json /
   report.json / report.md / metrics.csv / audit/<tt>.json）——Task 5
   接线后 _write_artifacts 末尾组装报告三件套（组装口径归
   test_eval_open_report.py）；
2. FakeOpenProducer 收到 run 级 settings 副本（modalities / audit 目录
   生效）与 session=None（producer 不持会话，池饿死纪律）；
3. 评审失败契约（spec §6）：产出完整保留、行 review=None、review_error
   落 raw、不计入 produce 失败（failed=0）；
4. 双熔断各自触发：produce 连败 ≥ABORT_CONSECUTIVE 与评审连败
   ≥ABORT_REVIEW_CONSECUTIVE 互相独立（ok=True 且 review_error 才计
   评审连败；produce 失败片不计评审连败）；
5. 跨服务互斥两个方向（closed 活跃 → open RunBusyError，反之亦然）；
6. modalities 校验：video 拒绝（v1 文本模态，关键帧为后续扩展）、
   缺 script 拒绝（台词随 script 通道渲染，缺了评审材料为空）、
   未知模态拒绝、空集拒绝；
7. 无 gt 依赖：库标签与历史 gt 无交集也照常起跑（开放评测没有排序
   真值任务，θ/集合准确率口径已移除）；
8. delete 路径守卫（`.`/嵌套段/越界按不存在，绝不 rmtree 输出根）；
9. review calibre（model / same_model / prompt_version）进 meta；
10. history 五摘要键（pass_rate / new_pass / hit_pass / new_ratio /
    three_high）由 open_report._read_open_summary 读 report.json；
    报告组装的专测在 test_eval_open_report.py。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from conftest import MOCK_EMBEDDER, MOCK_LLM
from tagplatform.capabilities.llm import MockLLMClient
from tagplatform.capabilities.prompts.open_review import REVIEW_PROMPT_VERSION
from tagplatform.services.eval import open_service
from tagplatform.services.eval.open_service import (
    ABORT_CONSECUTIVE,
    ABORT_REVIEW_CONSECUTIVE,
    DEFAULT_CONCURRENCY,
    RETRY_PER_MOVIE,
    EvalValidationError,
    OpenEvalService,
    RunBusyError,
)
from tagplatform.services.eval.service import (
    ClosedEvalService,
    RunBusyError as ClosedRunBusyError,
)
from tagplatform.services.generation.base import Evidence, ProducedTag
from tagplatform.settings import Settings
from tests.library_fixture import (
    bind_engine,
    library_root,
    unbind_engine,
    write_library,
)


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
        "description": "两名人物在开场场景中发生激烈争执。",
        "dialogues": [
          {{"dialogue_id": "d1", "speak_id": "character_001", "text": "你好。",
             "start_ms": 1000, "end_ms": 2000}}
        ]
      }},
      {{"scene_id": "scene_002", "start_ms": 60000, "end_ms": 90000,
        "description": "两人再次相遇并解释冲突起因。"}}
    ]
  }}
}}
"""

def _write_dataset(root: Path, *, six: bool = False) -> None:
    """内置夹具：迷你 MovieTags 数据集（gt 两份 + 影片内容 JSON）。

    形制与 tests/test_closed_eval.py 同款（gt/emotion.csv 三片 × 4 标签 +
    畸形 bad.csv + data/<tt>/content.json；six=True 扩到 6 片）。场景描述
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
    """临时数据集根（str）：每测试独立一套迷你 MovieTags（2 片可用）。"""
    _write_dataset(tmp_path)
    return str(tmp_path)


@pytest.fixture
def root6(tmp_path):
    """6 片数据集根（熔断用例：完成序连败后须有从未启动的余量片）。"""
    _write_dataset(tmp_path / "ds6", six=True)
    return str(tmp_path / "ds6")


@pytest.fixture
def engine(tmp_path):
    """评测服务用文件 DB（跨线程可见；:memory: 工作线程看不到主线程数据）。"""
    from tagplatform.capabilities.storage import create_engine_for
    from tagplatform.capabilities.storage.models import Base

    eng = create_engine_for(str(tmp_path / "open-eval.db"))
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


# 评测迷你标签库：gt 标签 t1/t2/t7/t9 全部入库（交集非空），挂 content
# 级目录（与 test_closed_eval.py 同款）
_EVAL_DIRS = [
    {"dir_id": "dir_eval", "parent_id": None, "level": 1, "dir_name": "评测标签",
     "dir_type": "dir_static", "applicable_scope": "content",
     "description": "开放评测单测用迷你目录", "aliases": []},
]
_EVAL_TAGS = [
    {"tag_id": tag_id, "dir_id": "dir_eval", "tag_name": name,
     "tag_type": "val_fixed", "description": "评测夹具标签", "status": "active"}
    for tag_id, name in (("t1", "悬疑"), ("t2", "动作"),
                         ("t7", "喜剧"), ("t9", "爱情"))
]
# 零交集库夹具：标签 id 全在 gt 之外（open 不拒绝，closed 同场景显式报错）
_DISJOINT_TAGS = [
    {"tag_id": "x1", "dir_id": "dir_eval", "tag_name": "零交集甲",
     "tag_type": "val_fixed", "description": "评测夹具标签", "status": "active"},
    {"tag_id": "x2", "dir_id": "dir_eval", "tag_name": "零交集乙",
     "tag_type": "val_fixed", "description": "评测夹具标签", "status": "active"},
]


# ---------------------------------------------------------------- 替身

def _hit(tag_id: str, name: str = "命中标签") -> ProducedTag:
    """库内命中行（origin=catalog，tag_id 非空）——干跑产出的 canned 形。"""
    return ProducedTag(
        origin="catalog", tag_id=tag_id, tag_name=name, candidate_type=None,
        candidate_description=None, relevance_score=0.9, confidence=0.8,
        evidence=[Evidence(scene_id="scene_001", start_ms=0, end_ms=60000,
                           description="开场证据")], level="film")


def _new_word(name: str, desc: str = "测试新词通用释义") -> ProducedTag:
    """新词行（origin=generated，tag_id=None）。"""
    return ProducedTag(
        origin="generated", tag_id=None, tag_name=name,
        candidate_type="val_fixed", candidate_description=desc,
        relevance_score=0.7, confidence=0.6,
        evidence=[Evidence(scene_id="scene_001", start_ms=0, end_ms=60000,
                           description="开场证据")], level="film", dir_id="dir_eval")


class FakeOpenProducer:
    """OpenTagProducer 替身：canned 产出 + 调用记录 + 审计落盘契约镜像。

    - 记录每次 produce 的 (task_id, asset_id, run_id, session, settings)，
      供断言 run 级 settings 副本与 session=None；
    - `gate` 非空时 produce 阻塞其上（busy/取消用例的时间窗）；
    - `fail_all` / `fail_tts` 注入 produce 失败（熔断用例，每次尝试都抛）；
    - 成功路径镜像真管线的审计契约：settings.mtg_report_dir/<task_id>.json
      ——runner 的职责是把 audit 目录重定向进 run 目录，替身落一份 stub
      JSON 让「四件写盘」断言可见（审计内容归管线测试管）。
    """

    mode = "open"
    builds_prompt = True
    prompt_version = "mtg-fake-v1"

    # 缺省 canned：tt0000001 = 两命中（t1 交集内带 gt 分 + tX 交集外）+ 一
    # 新词，tt0000002 = 一命中 + 一新词；其余影片（6 片数据集/兜底）各一
    # 命中 + 一新词——评审失败/评审熔断用例够用
    DEFAULT_CANNED = {
        "tt0000001": [_hit("t1", "悬疑"), _hit("tX", "交集外词"),
                      _new_word("新词甲")],
        "tt0000002": [_hit("t9", "爱情"), _new_word("新词乙")],
        "*": [_hit("t1"), _new_word("兜底新词")],
    }

    def __init__(self, canned: dict | None = None, *,
                 gate: threading.Event | None = None,
                 fail_all: bool = False,
                 fail_tts: set[str] | None = None) -> None:
        self.canned = canned if canned is not None else self.DEFAULT_CANNED
        self.calls: list[dict] = []
        self._gate = gate
        self._fail_all = fail_all
        self._fail_tts = set(fail_tts or ())

    def produce(self, spec, session, llm, embedder, judge_client=None,
                settings=None) -> list[ProducedTag]:
        self.calls.append({"task_id": spec.task_id, "asset_id": spec.asset_id,
                           "run_id": spec.run_id, "session": session,
                           "settings": settings})
        if self._gate is not None:
            self._gate.wait(10)
        if self._fail_all or spec.asset_id in self._fail_tts:
            raise RuntimeError(f"注入失败：{spec.asset_id}")
        tags = self.canned.get(spec.asset_id, self.canned.get("*", []))
        self._write_audit(settings, spec.task_id)
        return list(tags)

    @staticmethod
    def _write_audit(settings, task_id: str) -> None:
        """镜像 pipeline._write_report 契约：mtg_report_dir/<task_id>.json。"""
        target = Path(settings.mtg_report_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{task_id}.json").write_text(
            json.dumps({"task_id": task_id, "stub": True}, ensure_ascii=False),
            encoding="utf-8")


class _ReviewOkStub:
    model = "mock-1"

    """评审替身：回三维分 4/3/3，并引用两处夹具场景。

    返回纯文本（open_review._unwrap 对 str 按成功处理）；多出的 id（超出
    实际行数）由 open_review 按批内 index 出界忽略。`delay` 供取消用例
    拉开片完成间隔。
    """

    def __init__(self, total: int = 48, delay: float = 0.0) -> None:
        self.calls: list[str] = []
        self._total = total
        self._delay = delay

    def complete(self, messages, run_id) -> str:
        if self._delay:
            time.sleep(self._delay)
        self.calls.append(run_id)
        labels = json.loads(messages[-1]["content"].rsplit("## 待评估标签\n", 1)[1])
        reviews = [{"id": label["id"],
                    "pass": True if label["desc"] else None,
                    "reason": "场景检索有独立价值" if label["desc"] else "缺少释义，暂无法决定",
                    "relevance": {"score": 4, "reason": "场景有据"},
                    "semantics": {"score": 3, "reason": "名称清晰"},
                    "value": {
                        "presentation": {"score": 3, "reason": "有内容看点"},
                        "search": {"score": 3, "reason": "支持检索"},
                        "recommendation": {"score": 3, "reason": "推荐可用"},
                    },
                    "evidence_scene_ids": ["scene_001", "scene_002"]} for label in labels[:self._total]]
        return json.dumps({"reviews": reviews}, ensure_ascii=False)


class _ReviewFailStub:
    """评审替身：恒抛错（评审失败契约 / 评审熔断用例）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, messages, run_id) -> str:
        self.calls.append(run_id)
        raise RuntimeError("评审端点宕机")


class _GateLLM(MockLLMClient):
    """闸门 LLM（互斥用例的 closed 侧）：hold 后阻塞调用，保持 run 活跃；
    10s 未放行自恢复防挂死，放行后走 mock 正常完成。"""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()

    def complete(self, messages, run_id):
        self.gate.wait(10)
        return super().complete(messages, run_id)


# ---------------------------------------------------------------- 服务夹具

@pytest.fixture
def open_setup(tmp_path, root, engine, monkeypatch):
    """开放评测服务工厂：文件 DB + 迷你评测标签库 + 临时输出根 + fake 装配。

    monkeypatch `open_service.load_assembly_context`：保留真实装配的库加载
    与策略解析（「库就绪」校验路径照真走），仅把 producer 换成本测试的
    FakeOpenProducer。注意同一测试内多次 _make 共享最近注入的 fake（用例
    至多一个跑起来的 open 服务）。teardown 统一等 worker 收尾再解绑库
    引擎——不让 daemon 线程碰已 dispose 的引擎。
    """
    bind_engine(engine)
    write_library(library_root(), dirs=_EVAL_DIRS, tags=_EVAL_TAGS)
    made: list[tuple[OpenEvalService, FakeOpenProducer]] = []
    real_load = open_service.load_assembly_context

    def _make(llm=MOCK_LLM, producer: FakeOpenProducer | None = None,
              dataset_root: str | None = None,
              settings_kwargs: dict | None = None,
              dataset_root_owner=None) -> tuple[OpenEvalService, FakeOpenProducer]:
        fake = producer if producer is not None else FakeOpenProducer()

        def _fake_load(mode, policy_json="", session=None):
            ctx = real_load(mode, policy_json, session)
            return replace(ctx, producer=fake)

        monkeypatch.setattr(open_service, "load_assembly_context", _fake_load)
        settings = Settings(
            eval_dataset_root=dataset_root or root,
            open_eval_output_root=str(tmp_path / "open-out"),
            eval_output_root=str(tmp_path / "closed-out"),
            **(settings_kwargs or {}))
        svc = OpenEvalService(llm, MOCK_EMBEDDER, settings,
                              dataset_root_owner=dataset_root_owner)
        made.append((svc, fake))
        return svc, fake

    yield _make
    for svc, _fake in made:
        _drain_active(svc)
    unbind_engine(engine)


def _closed_service(root: str, output_root: str,
                    llm=MOCK_LLM) -> ClosedEvalService:
    """闭环服务（互斥/owner 用例）：同根、临时输出根、可注入慢速 LLM。"""
    return ClosedEvalService(llm, MOCK_EMBEDDER,
                             Settings(eval_dataset_root=root,
                                      eval_output_root=output_root))


# ---------------------------------------------------------------- 断言助手

def _run_dir(service, run_id) -> Path:
    return Path(service.settings.open_eval_output_root) / run_id


def _raw_rows(service, run_id) -> list[dict]:
    return json.loads((_run_dir(service, run_id) / "raw_results.json")
                      .read_text(encoding="utf-8"))


def _read_meta(service, run_id) -> dict:
    return json.loads((_run_dir(service, run_id) / "meta.json")
                      .read_text(encoding="utf-8"))


def _progress_entries(service, run_id) -> list[dict]:
    lines = (_run_dir(service, run_id) / "progress.jsonl") \
        .read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


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


# ---------------------------------------------------------------- 契约常量

def test_runner_constants():
    # 契约钉死：默认并发 / 每片重试 / produce 熔断阈值（沿用闭环语义）/ 评审独立熔断
    assert DEFAULT_CONCURRENCY == 4
    assert RETRY_PER_MOVIE == 2
    assert ABORT_CONSECUTIVE == 5
    assert ABORT_REVIEW_CONSECUTIVE == 5


# ---------------------------------------------------------------- 起跑校验

def test_start_validation_order(open_setup):
    """校验序前段：数据集存在 → 选片非空 → concurrency（失败不占 busy 位）。"""
    svc, _ = open_setup()
    with pytest.raises(EvalValidationError, match="数据集不存在"):
        svc.start("nope", {"mode": "all"})
    with pytest.raises(EvalValidationError, match="选片"):
        svc.start("emotion", {"mode": "custom", "ids": []})
    with pytest.raises(EvalValidationError, match="concurrency"):
        svc.start("emotion", {"mode": "all"}, concurrency=0)
    assert svc.active_run_id() is None


def test_modalities_validation(open_setup):  # Review Focus 5
    """modalities：video 拒绝（专属文案）/ 缺 script 拒绝 / 未知拒绝 / 空拒绝。"""
    svc, _ = open_setup()
    with pytest.raises(EvalValidationError, match="关键帧"):
        svc.start("emotion", {"mode": "all"}, modalities=("video",))
    with pytest.raises(EvalValidationError, match="关键帧"):
        svc.start("emotion", {"mode": "all"}, modalities=("video", "script"))
    with pytest.raises(EvalValidationError, match="script"):
        svc.start("emotion", {"mode": "all"}, modalities=("dialogue",))
    with pytest.raises(EvalValidationError, match="为空"):
        svc.start("emotion", {"mode": "all"}, modalities=())
    with pytest.raises(EvalValidationError, match="未知模态"):
        svc.start("emotion", {"mode": "all"}, modalities=("script", "foo"))
    assert svc.active_run_id() is None  # 校验失败不占 busy 位


# ---------------------------------------------------------------- 干跑主流程

def test_run_completes_writes_all_artifacts(open_setup):
    """跑完写盘六件 + audit/：meta / progress / raw / report.json /
    report.md / metrics.csv / audit/<tt>.json（Task 5 接线报告三件套）。"""
    svc, fake = open_setup()
    stub = _ReviewOkStub()
    svc._review_client = stub
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    assert svc.get(run_id).status == "succeeded"
    d = _run_dir(svc, run_id)
    assert (d / "meta.json").exists()
    assert (d / "progress.jsonl").exists()
    assert (d / "raw_results.json").exists()
    assert (d / "audit" / "tt0000001.json").exists()
    assert (d / "audit" / "tt0000002.json").exists()
    # 评审逐批原始响应随片落盘（成功片也留，供诊断端点复盘）
    assert (d / "review" / "tt0000001.json").exists()
    # Task 5 接线：报告三件套落盘
    assert (d / "report.json").exists()
    assert (d / "report.md").exists()
    assert (d / "metrics.csv").exists()
    report = json.loads((d / "report.json").read_text(encoding="utf-8"))
    assert report["sample_accounting"] == {
        "total": 2, "ok": 2, "failed": 0, "review_failed": 0}
    # 全量成功、无失败核算超 10%，但夹具未配独立评审端点 → 仅同模警示
    assert any("共享判断偏差" in line for line in report["banner"])
    # 替身审计（{"task_id","stub":True}）无漏斗键 → judge 空聚合
    assert report["judge"]["funnel"] is None
    assert report["judge"]["scores"]["relevance"]["n"] == 0
    assert report["calibre"]["review_same_model"] is True
    # 汇总：5 条产出（3+2），新词 2 条；替身评审全过线 → 通过率 1.0
    assert report["summary"]["total_tags"] == 5
    assert report["summary"]["new_words"] == 2
    assert report["summary"]["new_ratio"] == pytest.approx(2 / 5)
    assert report["summary"]["pass_rate"] == pytest.approx(1.0)
    assert report["summary"]["metrics"]["relevance"]["average"] == pytest.approx(4.0)

    rows = _raw_rows(svc, run_id)
    assert len(rows) == 2 and all(r["ok"] for r in rows)
    row1 = next(r for r in rows if r["tt"] == "tt0000001")
    # raw 行契约（Task 5 组装 open_report 的输入形状）
    assert set(row1) == {"tt", "title", "ok", "error", "hits", "new_words",
                         "review_error", "elapsed_ms"}
    assert row1["title"] == "A Movie" and row1["error"] is None
    assert row1["review_error"] is None
    # 产出二分：命中行 / 新词行（judge_reference 替身审计无判决 → None）
    assert len(row1["hits"]) == 2 and len(row1["new_words"]) == 1
    h1 = next(h for h in row1["hits"] if h["tag_id"] == "t1")
    assert h1["desc"] == "评测夹具标签"
    assert h1["scene_ids"] == ["scene_001"]
    assert h1["judge_reference"] is None
    assert h1["review"]["pass"] is True
    assert h1["review"]["reason"] == "场景检索有独立价值"
    assert h1["review"]["evidence_scene_ids"] == ["scene_001", "scene_002"]
    assert h1["review"]["relevance"] == {"score": 4, "reason": "场景有据"}
    hx = next(h for h in row1["hits"] if h["tag_id"] == "tX")
    assert hx["review"]["pass"] is None
    assert hx["review"]["semantics"]["score"] is None
    nw = row1["new_words"][0]
    assert set(nw) == {"name", "desc", "level", "scene_ids", "judge_reference", "review",
                       "dir_id", "dir_name", "target_type"}
    assert (nw["dir_id"], nw["dir_name"], nw["target_type"]) == ("dir_eval", "评测标签", "content")
    assert nw["name"] == "新词甲" and nw["review"]["pass"] is True

    # 增量进度：每片一行，review="ok"
    entries = _progress_entries(svc, run_id)
    assert len(entries) == 2
    assert all({"tt", "ok", "review", "hits", "new_words", "elapsed_ms"}
               <= set(e) for e in entries)
    assert all(e["review"] == "ok" and e["ok"] for e in entries)

    # 评审按片发起：run_id 带 :tt 后缀（LLM 调用标签口径）
    assert len(stub.calls) == 2
    assert sorted(c.rsplit(":t", 1)[0] for c in stub.calls) == [
        f"{run_id}:tt0000001", f"{run_id}:tt0000002"]  # 批次范围后缀

    # meta 口径快照
    meta = _read_meta(svc, run_id)
    assert meta["dataset"]["id"] == "emotion"
    assert meta["prompt"] == {"mode": "open", "version": "mtg-fake-v1"}
    assert meta["llm_model"] == "mock-1" and meta["embedding_model"] == "mock"
    assert meta["movies"] == {"mode": "all", "ids": ["tt0000001", "tt0000002"]}
    assert "thetas" not in meta  # 闭环遗留口径已移除
    assert meta["modalities"] == ["script", "dialogue"]
    assert meta["concurrency"] == DEFAULT_CONCURRENCY
    assert (meta["status"], meta["done"], meta["total"],
            meta["failed"]) == ("succeeded", 2, 2, 0)
    assert meta.get("warning") is None  # 有交集 → 无 warning


def test_producer_receives_run_settings_copy_and_no_session(open_setup):
    """produce 收到 run 级 settings 副本（modalities / audit 目录生效）与 session=None。"""
    svc, fake = open_setup()
    svc._review_client = _ReviewOkStub()
    modalities_before = svc.settings.mtg_modalities
    report_dir_before = svc.settings.mtg_report_dir
    run_id = svc.start("emotion", {"mode": "all"}, modalities=("script",),
                       wait=True)
    assert {c["asset_id"] for c in fake.calls} == {"tt0000001", "tt0000002"}
    call = next(c for c in fake.calls if c["asset_id"] == "tt0000001")
    assert call["session"] is None  # producer 不持会话（池饿死纪律）
    assert call["settings"] is not svc.settings  # run 级副本而非原对象
    assert call["settings"].mtg_modalities == "script"
    assert call["settings"].mtg_report_dir == str(
        Path(svc.settings.open_eval_output_root) / run_id / "audit")
    assert call["task_id"] == "tt0000001"  # 审计文件名 = task_id
    assert call["run_id"] == f"{run_id}:tt0000001"
    # 原服务 settings 不被 run 副本污染（起跑前后原值不变，不写死默认——
    # conftest 会经环境变量改 mtg_report_dir）
    assert svc.settings.mtg_modalities == modalities_before
    assert svc.settings.mtg_report_dir == report_dir_before


# ---------------------------------------------------------------- 评审失败契约

def test_review_failure_preserves_output(open_setup):
    """评审失败（spec §6）：产出完整保留、行 review=None、review_error 落 raw、
    片仍计 ok（不进 produce 失败），progress 行 review="failed"。"""
    svc, fake = open_setup()
    stub = _ReviewFailStub()
    svc._review_client = stub
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    state = svc.get(run_id)
    assert state.status == "succeeded" and state.failed == 0
    rows = _raw_rows(svc, run_id)
    assert all(r["ok"] for r in rows)
    row1 = next(r for r in rows if r["tt"] == "tt0000001")
    assert row1["review_error"] and "评审" in row1["review_error"]
    assert len(row1["hits"]) == 2 and len(row1["new_words"]) == 1  # 产出保留
    assert all(h["review"] is None for h in row1["hits"])
    assert all(n["review"] is None for n in row1["new_words"])
    entries = _progress_entries(svc, run_id)
    assert all(e["review"] == "failed" and e["ok"] for e in entries)
    # 评审每片一次（产出行 3 条 < 切分阈值，单片单批）
    assert len(stub.calls) == 2
    assert sorted(c.rsplit(":t", 1)[0] for c in stub.calls) == [
        f"{run_id}:tt0000001", f"{run_id}:tt0000002"]  # 批次范围后缀
    # Task 5 接线：报告侧按评审失败契约核算（两类片分开，不进 produce 失败）
    report = json.loads((_run_dir(svc, run_id) / "report.json")
                        .read_text(encoding="utf-8"))
    assert report["sample_accounting"] == {
        "total": 2, "ok": 2, "failed": 0, "review_failed": 2}
    assert report["summary"]["pass_rate"] is None  # 评审失败排除出分母（None≠0）
    assert report["summary"]["total_tags"] == 5  # 产出不作废
    assert [m["tt"] for m in report["review_failed_movies"]] == [
        "tt0000001", "tt0000002"]
    assert all(m["hit_pass"] is None and m["new_pass"] is None
               for m in report["movies"])
    # 失败现场：原始响应与失败原因按片落盘，报告之外还能还原具体形态
    trace = json.loads((_run_dir(svc, run_id) / "review" / "tt0000001.json")
                       .read_text(encoding="utf-8"))
    assert "评审端点宕机" in trace["error"]
    assert trace["batches"][0]["error"].startswith("评审调用失败")
    assert trace["errors"]


# ---------------------------------------------------------------- 双熔断

def test_produce_fuse_aborts(open_setup, root6):
    """produce 熔断：完成序连续 5 片失败 → aborted，余量片从未进池（并发 1）。"""
    svc, fake = open_setup(producer=FakeOpenProducer(fail_all=True),
                           dataset_root=root6)
    run_id = svc.start("emotion", {"mode": "all"}, concurrency=1, wait=True)
    state = svc.get(run_id)
    assert state.status == "aborted"
    assert state.done == ABORT_CONSECUTIVE
    assert state.failed == ABORT_CONSECUTIVE
    assert len(_raw_rows(svc, run_id)) == ABORT_CONSECUTIVE
    # 每失败片 1 + RETRY_PER_MOVIE 次尝试
    attempts: dict[str, int] = {}
    for call in fake.calls:
        attempts[call["asset_id"]] = attempts.get(call["asset_id"], 0) + 1
    assert set(attempts.values()) == {1 + RETRY_PER_MOVIE}
    meta = _read_meta(svc, run_id)
    assert meta["status"] == "aborted" and meta["done"] < meta["total"]


def test_review_fuse_aborts(open_setup, root6):
    """评审熔断（独立于 produce 熔断）：连续 5 片评审失败 → aborted、
    failed=0（评审失败不算 produce 失败）、meta.error 注明评审端点熔断。"""
    svc, fake = open_setup(dataset_root=root6)
    svc._review_client = _ReviewFailStub()
    run_id = svc.start("emotion", {"mode": "all"}, concurrency=1, wait=True)
    state = svc.get(run_id)
    assert state.status == "aborted"
    assert state.done == ABORT_REVIEW_CONSECUTIVE
    assert state.failed == 0  # 评审失败片照常 ok
    rows = _raw_rows(svc, run_id)
    assert len(rows) == ABORT_REVIEW_CONSECUTIVE
    assert all(r["ok"] and r["review_error"] for r in rows)
    assert all(r["hits"] and r["hits"][0]["review"] is None for r in rows)
    meta = _read_meta(svc, run_id)
    assert meta["status"] == "aborted"
    assert "评审端点熔断" in (meta.get("error") or "")


# ---------------------------------------------------------------- 跨服务互斥

def test_cross_service_mutex_both_directions(open_setup, root, tmp_path):  # Review Focus 2
    """closed 活跃 → open 起跑 RunBusyError；open 活跃 → closed 起跑 RunBusyError。

    互挂探针 = Task 6 app 装配同款：双方各挂对端的
    active_run_id_lockless（无锁读，防锁序死锁）。"""
    closed_llm = _GateLLM()
    closed_svc = _closed_service(root, str(tmp_path / "closed-out"),
                                 llm=closed_llm)
    svc, fake = open_setup()
    closed_svc.busy_probe = svc.active_run_id_lockless
    svc.busy_probe = closed_svc.active_run_id_lockless

    # 方向一：closed 活跃 → open 起跑拒绝（闸门 hold 住 closed run）
    closed_llm.gate.clear()
    closed_run = closed_svc.start("emotion", {"mode": "all"})
    try:
        with pytest.raises(RunBusyError, match="另一评测进行中"):
            svc.start("emotion", {"mode": "all"})
        assert svc.active_run_id() is None  # 校验失败不占 busy 位
    finally:
        closed_llm.gate.set()
    _await_terminal(closed_svc, closed_run)
    _drain_active(closed_svc)

    # 方向二：open 活跃 → closed 起跑拒绝（闸门假 producer 保持 open 活跃）
    open_gate = threading.Event()
    svc2, _ = open_setup(producer=FakeOpenProducer(gate=open_gate))
    svc2.busy_probe = closed_svc.active_run_id_lockless
    closed_svc.busy_probe = svc2.active_run_id_lockless
    open_run = svc2.start("emotion", {"mode": "all"})
    try:
        with pytest.raises(ClosedRunBusyError, match="另一评测进行中"):
            closed_svc.start("emotion", {"mode": "all"})
    finally:
        open_gate.set()
    _await_terminal(svc2, open_run)
    _drain_active(svc2)
    _drain_active(closed_svc)


def test_own_busy_rejects(open_setup):
    """open 自有 busy 位：同服务重复起跑拒绝（与对端探针无关）。"""
    gate = threading.Event()
    svc, _ = open_setup(producer=FakeOpenProducer(gate=gate))
    run_id = svc.start("emotion", {"mode": "all"})
    try:
        assert svc.active_run_id() == run_id
        with pytest.raises(RunBusyError, match="已有评测 run 进行中"):
            svc.start("emotion", {"mode": "all"})
    finally:
        gate.set()
    _await_terminal(svc, run_id)
    _drain_active(svc)


def test_effective_root_uses_owner_override(open_setup, root):
    """effective_root：owner（ClosedEvalService）优先——共享闭环页 override。"""
    closed_svc = _closed_service(root, str(root) + "-out")
    closed_svc.dataset_root_override = "/cache/Other"
    svc, _ = open_setup(dataset_root_owner=closed_svc)
    assert svc.effective_root == "/cache/Other"
    svc2, _ = open_setup()
    assert svc2.effective_root == root  # 无 owner → settings.eval_dataset_root


# ---------------------------------------------------------------- 零交集 / calibre

def test_library_without_gt_overlap_proceeds(open_setup):
    """开放评测无 gt 依赖：库标签与历史 gt 零交集也照常跑完（无 warning）。"""
    svc, fake = open_setup()
    write_library(library_root(), dirs=_EVAL_DIRS, tags=_DISJOINT_TAGS)
    svc._review_client = _ReviewOkStub()
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    state = svc.get(run_id)
    assert state.status == "succeeded"
    meta = _read_meta(svc, run_id)
    assert meta.get("warning") is None
    report = json.loads((_run_dir(svc, run_id) / "report.json")
                        .read_text(encoding="utf-8"))
    assert report["sample_accounting"] == {
        "total": 2, "ok": 2, "failed": 0, "review_failed": 0}
    assert report["summary"]["hit_pass"] == None


def test_review_calibre_in_meta(open_setup):
    """meta.review：评审模型 + same_model + 评审 prompt 版本。"""
    svc, _ = open_setup()
    svc._review_client = _ReviewOkStub()
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    meta = _read_meta(svc, run_id)
    assert meta["review"]["model"] == "mock-1"
    assert meta["review"]["same_model"] is True
    assert meta["review"]["prompt_version"] == REVIEW_PROMPT_VERSION
    assert meta["review"]["batch_mode"] == "movie"
    assert meta["review"]["max_concurrent_requests"] == 4

    svc2, _ = open_setup(settings_kwargs={
        "use_mock_llm": True, "open_eval_review_model": "review-x"})
    run_id2 = svc2.start("emotion", {"mode": "all"}, wait=True)
    meta2 = _read_meta(svc2, run_id2)
    assert meta2["review"]["model"] == "mock-1"
    assert meta2["review"]["same_model"] is True
    assert meta2["review"]["prompt_version"] == REVIEW_PROMPT_VERSION


# ---------------------------------------------------------------- 生命周期

def test_delete_path_guards(open_setup):
    """delete 路径守卫：`.`/嵌套段/越界按不存在，绝不 rmtree 输出根。"""
    svc, _ = open_setup()
    svc._review_client = _ReviewOkStub()
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    _drain_active(svc)
    assert svc.delete(".") is False
    assert svc.delete("a/b") is False
    assert svc.delete("../outside") is False
    assert svc.delete("run-nope") is False
    # 爆炸半径核验：输出根与已完成 run 均完好
    out = Path(svc.settings.open_eval_output_root)
    assert out.exists() and _run_dir(svc, run_id).exists()
    assert svc.delete(run_id) is True
    assert not _run_dir(svc, run_id).exists()
    assert svc.get(run_id) is None


def test_get_and_cancel_unknown(open_setup):
    svc, _ = open_setup()
    assert svc.get("run-nope") is None
    assert svc.cancel("run-nope") is False


def test_cancel_keeps_completed_results(open_setup, root6):
    """取消：在途片跑完、结果保留、status=cancelled、done<total。"""
    svc, fake = open_setup(dataset_root=root6)
    svc._review_client = _ReviewOkStub(delay=0.1)  # 每片评审 0.1s，拉开完成间隔
    run_id = svc.start("emotion", {"mode": "all"}, concurrency=1)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and svc.get(run_id).done < 1:
        time.sleep(0.01)
    assert svc.cancel(run_id) is True
    state = _await_terminal(svc, run_id)
    _drain_active(svc)
    assert state.status == "cancelled"
    rows = _raw_rows(svc, run_id)
    assert 1 <= len(rows) < state.total
    assert all(r["ok"] for r in rows)
    meta = _read_meta(svc, run_id)
    assert meta["status"] == "cancelled" and meta["done"] < meta["total"]
    child = svc.rereview(run_id)["run_id"]
    child_meta = _read_meta(svc, child)
    assert {key: child_meta[key] for key in ("status", "done", "total", "failed")} == {
        key: meta[key] for key in ("status", "done", "total", "failed")}
    assert len(_raw_rows(svc, child)) == len(rows)


# ---------------------------------------------------------------- 历史扫描

def test_live_report_published(open_setup):
    """运行落 live.json，整片评审完成后统一回填。"""
    svc, _ = open_setup()
    svc._review_client = _ReviewOkStub()
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    _drain_active(svc)
    d = _run_dir(svc, run_id)
    assert (d / "live.json").exists()
    live = json.loads((d / "live.json").read_text(encoding="utf-8"))
    assert live["summary"]["total_tags"] == 5


def test_review_produced_publishes_whole_batch(open_setup):
    """_review_produced 带 result/publish：整批完成后一起回填。"""
    svc, _ = open_setup()
    svc._review_client = _ReviewOkStub()
    content = open_service.load_movie_content(svc.effective_root, "tt0000001")
    snapshot = [{"tag_id": None, "name": "新词甲", "desc": "通用释义",
                 "level": "film", "scenes": [], "judge_reference": None}]
    result = open_service.OpenMovieResult(
        tt="tt0000001", title="x", ok=True, error=None, hits=[],
        new_words=[], review_error=None, elapsed_ms=0)
    calls = []
    svc._review_produced("tt0000001", snapshot, content, "run-x",
                         result=result, publish=lambda: calls.append(1))
    assert len(calls) == 2  # 产出待评、整片已评
    assert result.new_words[0]["review"]["pass"] is True


def test_rereview_reuses_snapshot_without_produce(open_setup):
    """rereview 复用产出快照重评审：不重跑生成，换评审替身后报告更新。"""
    svc, fake = open_setup()
    svc._review_client = _ReviewOkStub()
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    _drain_active(svc)
    d = _run_dir(svc, run_id)
    snapshot = json.loads((d / "produced" / "tt0000001.json").read_text(encoding="utf-8"))
    assert snapshot["labels"][0]["target_type"] == "content"
    calls_before = len(fake.calls)
    # 换评审替身为失败 → rereview 后评审失败但产出保留，produce 不重跑
    before = (d / "report.json").read_bytes()
    # 重评读取原材料快照。当前根目录切换也不影响。
    svc.settings.eval_dataset_root = str(d / "missing-dataset")
    svc._review_client = _ReviewFailStub()
    svc._review_client.model = "new-review-model"
    out = svc.rereview(run_id)
    assert out["reviewed"] == 2 and out["review_failed"] == 2
    assert len(fake.calls) == calls_before  # 没重跑生成
    assert (d / "report.json").read_bytes() == before
    child = _run_dir(svc, out["run_id"])
    report = json.loads((child / "report.json").read_text(encoding="utf-8"))
    assert report["source_run_id"] == run_id
    assert report["calibre"]["review_model"] == "new-review-model"
    assert report["sample_accounting"]["review_failed"] == 2
    assert report["summary"]["total_tags"] == 5  # 产出不作废
    assert report["summary"]["pass_rate"] is None  # 评审失败排除出分母
    assert report["movies"][0]["hits"][0]["target_type"] == "content"


def test_rereview_preserves_zero_output_and_original_produce_failure(open_setup):
    svc, _ = open_setup(producer=FakeOpenProducer(canned={}, fail_tts={"tt0000002"}))
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    out = svc.rereview(run_id)
    assert out["reviewed"] == 0
    assert _raw_rows(svc, out["run_id"]) == _raw_rows(svc, run_id)
    meta = _read_meta(svc, out["run_id"])
    assert (meta["done"], meta["failed"]) == (2, 1)


def test_rereview_rejects_active_run(open_setup):
    svc, _ = open_setup()
    svc._review_client = _ReviewOkStub()
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    # 终态后注册表已清；手动塞回活跃态模拟在跑
    from tagplatform.services.eval.open_service import RunActiveError
    svc._active_run_id = run_id
    with pytest.raises(RunActiveError):
        svc.rereview(run_id)


def test_history_running_progress_uses_registry(open_setup, root6):
    """回归：运行中 history 行进度取注册表活值，而非 meta.json 起跑值。

    meta.json 只在起跑/终态写盘（运行中 done 恒 0），前端进度条轮询
    history 列表——取 meta 会一路 0% 到终态才跳 100%。运行中行的
    done/total/failed 须与注册表一致；终态后回落 meta 值。
    """
    svc, _ = open_setup(dataset_root=root6)
    svc._review_client = _ReviewOkStub(delay=0.3)  # 拉开片完成间隔，稳住运行中窗口
    run_id = svc.start("emotion", {"mode": "all"}, concurrency=1)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and svc.get(run_id).done < 1:
        time.sleep(0.01)
    state = svc.get(run_id)
    assert state.status == "running" and state.done >= 1
    assert _read_meta(svc, run_id)["done"] == 0  # 运行中 meta 不推进
    row = next(r for r in svc.history() if r["run_id"] == run_id)
    assert row["status"] == "running"
    assert 1 <= row["done"] < row["total"]
    assert row["total"] == state.total
    assert svc.cancel(run_id) is True
    _await_terminal(svc, run_id)
    _drain_active(svc)
    meta = _read_meta(svc, run_id)
    row = next(r for r in svc.history() if r["run_id"] == run_id)
    assert (row["done"], row["total"], row["failed"]) == (
        meta["done"], meta["total"], meta["failed"])


def test_history_summary_from_report(open_setup):
    """history 五摘要键由 _read_open_summary 读 report.json（Task 5 接线）；
    磁盘 running 残留不在注册表 → interrupted、无报告 → 五键 None。"""
    svc, _ = open_setup()
    svc._review_client = _ReviewOkStub()
    run_id = svc.start("emotion", {"mode": "all"}, wait=True)
    _drain_active(svc)
    out = Path(svc.settings.open_eval_output_root)
    # run-ghost：meta 残留 running 且不在进程内注册表 → interrupted
    ghost = out / "run-ghost"
    ghost.mkdir()
    (ghost / "meta.json").write_text(json.dumps({
        "run_id": "run-ghost", "status": "running", "done": 1, "total": 3,
        "failed": 0, "started_at": "2026-11-21T10:00:00+00:00",
        "dataset": {"id": "emotion"}}, ensure_ascii=False), encoding="utf-8")

    rows = svc.history()
    by_id = {r["run_id"]: r for r in rows}
    mine = by_id[run_id]
    assert mine["status"] == "succeeded"
    assert mine["finished_at"] is not None
    assert mine["dataset_id"] == "emotion"
    # 夹具全 pass：通过率均 1.0；新词占比微口径 2/5；三维 4/3/3 非全满分
    assert mine["pass_rate"] == pytest.approx(1.0)
    assert mine["new_pass"] == pytest.approx(1.0)
    assert mine["hit_pass"] == pytest.approx(1.0)
    assert mine["new_ratio"] == pytest.approx(2 / 5)
    assert mine["three_high"] == pytest.approx(0.0)
    assert by_id["run-ghost"]["status"] == "interrupted"
    five = ("pass_rate", "new_pass", "hit_pass", "new_ratio", "three_high")
    assert all(by_id["run-ghost"][k] is None for k in five)
    # started_at 降序
    started = [r["started_at"] or "" for r in rows]
    assert started == sorted(started, reverse=True)


def test_history_empty_when_root_missing(tmp_path):
    """输出根不存在 → []（宁缺勿崩）。"""
    svc = OpenEvalService(MOCK_LLM, MOCK_EMBEDDER, Settings(
        open_eval_output_root=str(tmp_path / "no-such-root")))
    assert svc.history() == []


# ---------------------------------------------------------------- DB 零写入

def test_db_zero_writes(open_setup, engine):
    """干跑全程 DB 零写入（session=None 传 producer + 评测只写输出根）。"""
    svc, fake = open_setup()
    svc._review_client = _ReviewOkStub()
    svc.start("emotion", {"mode": "all"}, wait=True)
    with engine.connect() as con:
        for table in ("media_asset", "pre_asset_tag", "tag_generation_task",
                      "asset_build_task", "tag_quality_evaluation"):
            assert con.execute(
                text(f"select count(*) from {table}")).scalar() == 0
