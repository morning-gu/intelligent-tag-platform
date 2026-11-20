"""闭环评测 admin API 路由契约（Task 6 基础 + Task 2 单片明细）：13 端点
+ 错误映射 + app 装配。

路由契约钉死（plan Task 6，全部端点 require_admin）：
- GET /datasets → DatasetInfoOut（usable_movies 输出为**数量**；畸形 gt
  文件在扫描层就被跳过，API 列表只见合法数据集）；
- GET /datasets/{id}/movies → [{tt, title}]（未知 dataset 404）；
- POST /runs：错误映射集中在 router 层——未知 dataset 404（先以
  scan_datasets 结果校验 dataset_id）、服务层 EvalValidationError → 400、
  RunBusyError → 409（detail 含活跃 run_id）；pydantic 形状校验 422 自动；
- GET /runs → history 摘要行；GET /runs/{id} → RunState 形（404 未知；
  进程内注册表没有时按磁盘历史兜底——/runs 列表里有的 id，详情必须能
  打开）；POST cancel → {ok}（未知/已终态 → ok=false，幂等意图）；
- GET report → report.json（无报告/截断 → 404，与 history 读侧容错同
  口径）；GET files/{name} → FileResponse 白名单五件套（report.json 走
  专用端点不在白名单、白名单外 404）；
- GET runs/{id}/movies → 单片摘要包层 {theta_used, k_p, k_r, rows}（行
  序失败置顶、P 升序）；GET runs/{id}/movies/{tt} → 单片明细三表；读侧
  容错：raw_results.json 缺失/截断/非 list → 404「无单片明细」，meta
  档位缺失/损坏 → DEFAULT 档（0.4/10/20），未知 tt → 404。

夹具：`_write_dataset`（来自 test_closed_eval.py）往
client.app.state.settings.eval_dataset_root 写迷你 MovieTags（绝不扫
/cache/*）；激活 closed 模板由 Base.metadata.create_all 的 after_create
钩子自动播种（Task 3 发现，无需手插）；评测库经 write_library 换成 gt
标签全集（write_library 镜像进所有已绑引擎含 app 引擎；tag_library
fixture 每测试开头重置回默认库，换库只在当前测试内生效）。

busy / cancel / 运行中无报告的确定性时间窗：`_GateLLM` 以 Event 闸住
LLM 调用（经文档化测试缝隙 `app.state.closed_eval._llm` 换入）——首发
run 阻塞在 complete()，第二发起跑必撞 busy，时序由测试显式控制，不靠
sleep 赌机器快慢。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tagplatform.llm import MockLLMClient
from test_closed_eval import _EVAL_DIRS, _EVAL_TAGS, _write_dataset

# 评测 API 端点前缀（本文件所有用例共用）
_BASE = "/api/v1/eval/closed"


class _GateLLM(MockLLMClient):
    """LLM 调用闸门：hold() 后所有 complete 阻塞至 release()。

    busy 409 / cancel / 运行中无报告 404 的时间窗由此钉死（首发 run 的
    complete 阻塞在闸上）；10s 未放行则抛错——测试忘 release 也不会挂死
    套件（run 以失败终态收尾，worker 退出不滞留）。
    """

    def __init__(self) -> None:
        super().__init__()
        self._gate = threading.Event()
        self._gate.set()  # 初始放行

    def hold(self) -> None:
        self._gate.clear()

    def release(self) -> None:
        self._gate.set()

    def complete(self, messages, run_id):
        if not self._gate.wait(timeout=10):
            raise RuntimeError("闸门超时未放行（测试未 release）")
        return super().complete(messages, run_id)


def _seed_dataset_and_library(client: TestClient) -> None:
    """往 app 的评测数据集根写迷你 MovieTags + 把库换成 gt 标签全集。

    - 数据集根来自 client fixture 的隔离设置（tmp_path/eval-ds），测试
      间零残留；
    - write_library 镜像进所有已绑引擎（含 app 引擎）：库标签 = gt 标签
      全集（t1/t2/t7/t9）→ 库∩gt 非空，run 能起跑；
    - 激活 closed 模板由建表钩子自动播种（见模块 docstring），无需手插。
    """
    from tests.library_fixture import library_root, write_library

    _write_dataset(Path(client.app.state.settings.eval_dataset_root))
    write_library(library_root(), dirs=_EVAL_DIRS, tags=_EVAL_TAGS)


def _start_run(client: TestClient, dataset: str = "emotion",
               movies: dict | None = None, **extra) -> dict:
    """起跑（默认 all 模式）→ 响应 body（断言 200 由调用方按需做）。"""
    payload = {"dataset": dataset,
               "movies": movies or {"mode": "all"}, **extra}
    return client.post(f"{_BASE}/runs", json=payload)


def _await_terminal(client: TestClient, run_id: str,
                    timeout: float = 15.0) -> dict:
    """轮询 GET /runs/{run_id} 至终态（mock llm 毫秒级；闸门用例由
    release 后收敛）。超时 fail 不静默。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"{_BASE}/runs/{run_id}").json()
        if body.get("status") != "running":
            return body
        time.sleep(0.01)
    pytest.fail(f"run {run_id} 未在 {timeout}s 内到达终态")


def _await_report(client: TestClient, run_id: str,
                  timeout: float = 15.0):
    """轮询报告端点至 200。

    终态落盘存在文档化的非原子窗口（meta→raw→report 写序，注册表先翻
    终态、报告后落盘；读侧 404「无报告」是既定容错语义）——测试轮询到
    报告出现再断言内容，不赌毫秒级窗口。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"{_BASE}/runs/{run_id}/report")
        if r.status_code == 200:
            return r
        time.sleep(0.01)
    pytest.fail(f"run {run_id} 的报告未在 {timeout}s 内落盘")


def _await_file(client: TestClient, run_id: str, name: str,
                timeout: float = 5.0):
    """轮询产物文件端点至 200。

    write_report 写序 report.json → report.md → metrics.csv（closed_eval
    :1209 起）：等 report.json 200 后紧接断言 md/csv 仍可能撞上毫秒级
    窗口（负载下的实测 flake，2026-11-21）——同 _await_report 口径轮询。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"{_BASE}/runs/{run_id}/files/{name}")
        if r.status_code == 200:
            return r
        time.sleep(0.01)
    pytest.fail(f"run {run_id} 的产物 {name} 未在 {timeout}s 内落盘")


# ---------------------------------------------------------------- 数据集列表


def test_datasets_endpoint_shape(client):
    """GET /datasets：合法数据集列表 + 字段契约（usable_movies 是数量）。"""
    _seed_dataset_and_library(client)
    r = client.get(f"{_BASE}/datasets")
    assert r.status_code == 200
    rows = r.json()
    # 畸形 bad.csv 被扫描层跳过：列表只有 emotion
    assert len(rows) == 1 and rows[0]["id"] == "emotion"
    assert rows[0]["n_movies"] == 3 and rows[0]["n_tags"] == 4
    assert rows[0]["library_overlap"] == 4  # gt {t1,t2,t7,t9} ∩ 评测库全集
    # usable_movies 输出为数量：tt0000003 只有 gt 分没有 content.json → 2
    assert rows[0]["usable_movies"] == 2
    assert rows[0]["gt_path"].endswith("emotion.csv")


def test_dataset_movies_endpoint(client):
    """GET /datasets/{id}/movies：usable 影片 [{tt, title}]；未知 id 404。"""
    _seed_dataset_and_library(client)
    r = client.get(f"{_BASE}/datasets/emotion/movies")
    assert r.status_code == 200
    assert r.json() == [{"tt": "tt0000001", "title": "A Movie"},
                        {"tt": "tt0000002", "title": "B Movie"}]
    assert client.get(f"{_BASE}/datasets/nope/movies").status_code == 404


# ---------------------------------------------------------------- 起跑与错误映射


def test_unknown_dataset_404(client):
    """未知 dataset：POST /runs 与 GET movies 均 404（先校验后透传；
    detail 来自路由层守卫，区别于路由不存在的裸 Not Found）。"""
    _seed_dataset_and_library(client)
    r = _start_run(client, dataset="nope")
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]
    r2 = client.get(f"{_BASE}/datasets/nope/movies")
    assert r2.status_code == 404 and "不存在" in r2.json()["detail"]


def test_start_validation_400(client):
    """服务层 EvalValidationError → 400：custom 越权 id / concurrency<1。"""
    _seed_dataset_and_library(client)
    r = _start_run(client, movies={"mode": "custom", "ids": ["tt9999"]})
    assert r.status_code == 400
    r2 = _start_run(client, concurrency=0)
    assert r2.status_code == 400
    # 校验失败不占 busy 位：紧随其后的合法起跑照常受理
    r3 = _start_run(client)
    assert r3.status_code == 200
    assert _await_terminal(client, r3.json()["run_id"])["status"] == "succeeded"


def test_start_bad_shape_422(client):
    """pydantic 形状校验 422 自动：movies.mode 词表外 / 缺 dataset。"""
    _seed_dataset_and_library(client)
    r = _start_run(client, movies={"mode": "banana"})
    assert r.status_code == 422
    r2 = client.post(f"{_BASE}/runs", json={"movies": {"mode": "all"}})
    assert r2.status_code == 422


def test_start_busy_409(client):
    """双发起跑：闸门 LLM 闸住首发 run → 第二发 409，detail 含活跃 run_id。"""
    _seed_dataset_and_library(client)
    gate = _GateLLM()
    gate.hold()
    service = client.app.state.closed_eval
    original_llm = service._llm
    service._llm = gate  # 文档化测试缝隙：换慢 LLM 创造 busy 时间窗
    try:
        r1 = _start_run(client)
        assert r1.status_code == 200
        run_id = r1.json()["run_id"]
        r2 = _start_run(client)
        assert r2.status_code == 409
        assert run_id in r2.json()["detail"]
    finally:
        gate.release()
        service._llm = original_llm
    # 放行后首发 run 正常收敛（终态 succeeded，busy 位随 worker 释放）
    assert _await_terminal(client, run_id)["status"] == "succeeded"


# ---------------------------------------------------------------- 端到端


def test_run_end_to_end(client):
    """起跑 → 轮询终态 → 报告 / 文件 / 历史一条龙（mock llm 毫秒级）。"""
    _seed_dataset_and_library(client)
    r = _start_run(client)
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    body = _await_terminal(client, run_id)
    assert body["status"] == "succeeded"
    assert body["done"] == body["total"] == 2 and body["failed"] == 0
    # RunState 形字段齐全（含终态时刻）
    assert {"run_id", "status", "done", "total", "failed", "started_at",
            "finished_at", "error"} <= set(body)
    assert body["finished_at"] is not None  # 终态时刻（ISO 串）

    rep = _await_report(client, run_id)
    assert "grid" in rep.json()
    assert rep.json()["banner"] is None  # 全量成功无部分运行 banner

    # 文件白名单内：report.md 可下载（md/csv 在 json 后落盘，轮询到出现）
    assert _await_file(client, run_id, "report.md").status_code == 200

    # 历史列表含本 run（摘要档随报告落盘：θ=0.4 的 K=10 在默认档内）
    rows = client.get(f"{_BASE}/runs").json()
    mine = next(row for row in rows if row["run_id"] == run_id)
    assert mine["status"] == "succeeded" and mine["dataset_id"] == "emotion"
    assert mine["ndcg10"] is not None
    assert mine["finished_at"] is not None  # 历史行也透出终态时刻


def test_files_whitelist_404(client):
    """files 白名单：五件套 200；report.json 走专用端点不在白名单、
    白名单外任意名 → 404。"""
    _seed_dataset_and_library(client)
    run_id = _start_run(client).json()["run_id"]
    _await_terminal(client, run_id)
    # 先等报告落盘（终态写序 meta→raw→report 的窄窗口），再验白名单全量稳定
    # （md/csv 在 json 后毫秒级落盘，逐文件轮询——同 _await_report 口径）
    _await_report(client, run_id)
    base = f"{_BASE}/runs/{run_id}/files"
    for name in ("report.md", "metrics.csv", "raw_results.json",
                 "meta.json", "progress.jsonl"):
        assert _await_file(client, run_id, name).status_code == 200, name
    assert client.get(f"{base}/whatever.txt").status_code == 404
    assert client.get(f"{base}/report.json").status_code == 404


# ---------------------------------------------------------------- 删除


def test_delete_run_endpoint(client):
    """DELETE 终态 run：200 {ok} → 详情/列表 404；重复删除 → 404。"""
    _seed_dataset_and_library(client)
    run_id = _start_run(client).json()["run_id"]
    _await_terminal(client, run_id)
    _await_report(client, run_id)
    # 报告落盘后 active 位可能还有毫秒级释放窗口：短轮询到 200
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        r = client.delete(f"{_BASE}/runs/{run_id}")
        if r.status_code == 200:
            break
        time.sleep(0.01)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get(f"{_BASE}/runs/{run_id}").status_code == 404
    # 重复删除 → 404（资源消失语义，与 GET 未知 run 同口径）
    assert client.delete(f"{_BASE}/runs/{run_id}").status_code == 404
    # 历史列表不再含该 run
    assert all(row["run_id"] != run_id
               for row in client.get(f"{_BASE}/runs").json())


def test_delete_active_run_409(client):
    """运行中删除 → 409 带原因（active 位守卫）。"""
    _seed_dataset_and_library(client)
    gate = _GateLLM()
    gate.hold()
    service = client.app.state.closed_eval
    original_llm = service._llm
    service._llm = gate
    try:
        run_id = _start_run(client).json()["run_id"]
        r = client.delete(f"{_BASE}/runs/{run_id}")
        assert r.status_code == 409
        assert "不可删除" in r.json()["detail"]
    finally:
        gate.release()
        service._llm = original_llm
    _await_terminal(client, run_id)


# ---------------------------------------------------------------- 取消


def test_cancel_endpoint(client):
    """cancel：受理 {ok: true} → 终态 cancelled；重复取消 {ok: false}；
    运行中无报告 → 404，终态后报告落盘（部分运行 banner 响亮）。"""
    _seed_dataset_and_library(client)
    gate = _GateLLM()
    gate.hold()
    service = client.app.state.closed_eval
    original_llm = service._llm
    service._llm = gate
    try:
        run_id = _start_run(client).json()["run_id"]
        # 在途运行、报告未落盘 → 404「无报告」（与 history 读侧容错同口径）
        assert client.get(f"{_BASE}/runs/{run_id}/report").status_code == 404
        r = client.post(f"{_BASE}/runs/{run_id}/cancel")
        assert r.status_code == 200 and r.json() == {"ok": True}
    finally:
        gate.release()
        service._llm = original_llm
    body = _await_terminal(client, run_id)
    assert body["status"] == "cancelled"
    # 幂等：已终态再取消 → ok=false（不报错、不影响已落盘结果）
    assert client.post(f"{_BASE}/runs/{run_id}/cancel").json() == {"ok": False}
    # 取消 run 的报告随终态落盘：部分运行 banner 必在
    rep = _await_report(client, run_id)
    assert rep.json()["banner"] is not None


# ---------------------------------------------------------------- run 详情与未知


def test_unknown_run_404_and_empty_listing(client):
    """未知 run_id：详情/报告/文件 404；无 run 时历史列表为空。"""
    _seed_dataset_and_library(client)
    assert client.get(f"{_BASE}/runs/run-unknown").status_code == 404
    assert client.get(f"{_BASE}/runs/run-unknown/report").status_code == 404
    assert client.get(
        f"{_BASE}/runs/run-unknown/files/report.md").status_code == 404
    assert client.get(f"{_BASE}/runs").json() == []


def test_get_run_disk_only_fallback(client):
    """注册表没有的 run 按磁盘历史兜底（上一进程残留）：meta.status=running
    且不在注册表 → interrupted（与 /runs 列表口径一致）。"""
    _seed_dataset_and_library(client)
    out = Path(client.app.state.settings.eval_output_root)
    d = out / "run-diskonly"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "run_id": "run-diskonly", "status": "running", "done": 1,
        "total": 3, "failed": 0, "started_at": "2026-11-19T10:00:00+00:00",
        "dataset": {"id": "emotion"}}, ensure_ascii=False), encoding="utf-8")
    body = client.get(f"{_BASE}/runs/run-diskonly").json()
    assert body["status"] == "interrupted"
    assert body["done"] == 1 and body["total"] == 3
    assert body["run_id"] == "run-diskonly"
    assert body["finished_at"] is None  # 老 run meta 无该键 → None（兼容）


# ---------------------------------------------------------------- admin 门槛


# ---------------------------------------------------------------- 数据集根路径


def test_dataset_root_default_and_switch(client, tmp_path):
    """GET 默认态 → POST 切有效根（读路径跟随）→ 无效根 400/422，失败不变更。

    进程内 override（spec 2026-11-20-eval-dataset-root-design）：重启回落
    .env 默认由 is_default 表达；400 语义 = 目录不存在/非目录/扫描零有效
    数据集，原因写进 detail。
    """
    _seed_dataset_and_library(client)
    # 默认态：root = settings 值、is_default=True
    r = client.get(f"{_BASE}/dataset-root")
    assert r.status_code == 200
    body = r.json()
    assert body["root"] == str(client.app.state.settings.eval_dataset_root)
    assert body["is_default"] is True

    # 第二个有效根（同构迷你数据集）→ 200；GET 反映且 is_default=False；
    # GET /datasets 的 gt_path 指向新根（读路径走 effective_root）
    root_b = tmp_path / "ds-b"
    root_b.mkdir()
    _write_dataset(root_b)
    r = client.post(f"{_BASE}/dataset-root", json={"root": str(root_b)})
    assert r.status_code == 200
    assert r.json() == {"root": str(root_b), "is_default": False}
    body = client.get(f"{_BASE}/dataset-root").json()
    assert body["root"] == str(root_b)
    assert body["is_default"] is False
    rows = client.get(f"{_BASE}/datasets").json()
    assert len(rows) == 1 and rows[0]["id"] == "emotion"
    assert rows[0]["gt_path"].startswith(str(root_b))

    # 二次 POST 相同根 → 200 幂等
    assert client.post(f"{_BASE}/dataset-root",
                       json={"root": str(root_b)}).status_code == 200

    # 空字符串 → 422（形状校验）
    assert client.post(f"{_BASE}/dataset-root",
                       json={"root": ""}).status_code == 422
    # 不存在路径 → 400 带原因
    r = client.post(f"{_BASE}/dataset-root",
                    json={"root": str(tmp_path / "nope")})
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"]
    # 文件（非目录）→ 400
    a_file = tmp_path / "plain.txt"
    a_file.write_text("x", encoding="utf-8")
    assert client.post(f"{_BASE}/dataset-root",
                       json={"root": str(a_file)}).status_code == 400
    # 目录存在但扫描零有效数据集 → 400 带原因
    empty_dir = tmp_path / "empty-dir"
    empty_dir.mkdir()
    r = client.post(f"{_BASE}/dataset-root", json={"root": str(empty_dir)})
    assert r.status_code == 400
    assert "数据集" in r.json()["detail"]
    # 失败的 POST 不变更生效值
    assert client.get(f"{_BASE}/dataset-root").json()["root"] == str(root_b)


def test_admin_only_all_endpoints(client):
    """全部 13 端点仅 admin：建 reviewer → 换登录态 → 逐一 403。"""
    _seed_dataset_and_library(client)
    assert client.post("/api/v1/users", json={
        "username": "bob", "password": "pw-bob-1", "role": "reviewer"
    }).status_code == 200
    client.post("/api/v1/auth/logout")
    client.post("/api/v1/auth/login",
                json={"username": "bob", "password": "pw-bob-1"})

    run_id = "run-anything"
    checks = [
        lambda: client.get(f"{_BASE}/datasets"),
        lambda: client.get(f"{_BASE}/datasets/emotion/movies"),
        lambda: client.post(f"{_BASE}/runs",
                            json={"dataset": "emotion",
                                  "movies": {"mode": "all"}}),
        lambda: client.get(f"{_BASE}/runs"),
        lambda: client.get(f"{_BASE}/runs/{run_id}"),
        lambda: client.post(f"{_BASE}/runs/{run_id}/cancel"),
        lambda: client.delete(f"{_BASE}/runs/{run_id}"),
        lambda: client.get(f"{_BASE}/runs/{run_id}/report"),
        lambda: client.get(f"{_BASE}/runs/{run_id}/files/report.md"),
        lambda: client.get(f"{_BASE}/runs/{run_id}/movies"),
        lambda: client.get(f"{_BASE}/runs/{run_id}/movies/{run_id}"),
        lambda: client.get(f"{_BASE}/dataset-root"),
        lambda: client.post(f"{_BASE}/dataset-root",
                            json={"root": "/cache/MovieTags"}),
    ]
    for check in checks:
        assert check().status_code == 403


# ---------------------------------------------------------------- 单片明细两端点（badcase 视图）

# 手写 run 目录的默认 id（本节用例共用）
_MOVIES_RUN = "run-movies"


def _movies_row(tt, gt, produced, *, ok=True, error=None, elapsed_ms=1200):
    """手造一条 raw_results.json 行（形状 = closed_eval 终态落盘行）。"""
    return {"tt": tt, "title": f"片名 {tt}", "ok": ok, "error": error,
            "produced": produced, "gt": dict(gt), "elapsed_ms": elapsed_ms}


def _movies_prod(tag_id, sf, *, tag_name=None):
    """手造一条 produced 条目（confidence/evidence_count 给常规值）。"""
    return {"tag_id": tag_id, "tag_name": tag_name or tag_id, "sf": sf,
            "confidence": 0.9, "evidence_count": 2}


def _movies_rows():
    """默认行数据：一部 hit（A）+ 一部失败（B），指标手算值钉死：

    - A（tt0000001）：produced 故意乱序（t2 在 t1 前）——明细按 (-sf, tag)
      防御重排后 t1 在前；zz 不在本批 gt 并集 → 空间外；t9 gt=0.1 < θ →
      产出不命中；t7 ≥θ 未产出 → 漏检（库映射 t7→喜剧 回填名称）。
      p@10 = 2/10、r@20 = 2/3、spearman = 0.4（四键秩 [4,3,1,2]×[4,2,3,1]）。
    - B（tt0000002）：ok=False + error → 摘要置顶、指标全 None
      （n_relevant 仍按 gt∩库 ≥θ 计 1）。
    """
    a = _movies_row(
        "tt0000001", {"t1": 0.9, "t2": 0.8, "t7": 0.85, "t9": 0.1},
        [_movies_prod("t2", 0.80, tag_name="动作"),
         _movies_prod("t1", 0.90, tag_name="悬疑"),
         _movies_prod("zz", 0.99, tag_name="噪声"),
         _movies_prod("t9", 0.75, tag_name="爱情")])
    b = _movies_row("tt0000002", {"t7": 0.9}, [], ok=False, error="boom",
                    elapsed_ms=300)
    return [a, b]


def _write_movies_run(client, *, run_id=_MOVIES_RUN, rows=None,
                      write_raw=True, write_meta=True, grid=True):
    """手写一个终态 run 目录（磁盘夹具模式同 test_get_run_disk_only_fallback）：
    raw_results.json + meta.json；grid=False 时 meta 不带 thetas/ks 键
    （老 run 形态）。"""
    out = Path(client.app.state.settings.eval_output_root)
    d = out / run_id
    d.mkdir(parents=True)
    if write_raw:
        (d / "raw_results.json").write_text(
            json.dumps(rows if rows is not None else _movies_rows(),
                       ensure_ascii=False), encoding="utf-8")
    if write_meta:
        meta = {"run_id": run_id, "status": "succeeded", "done": 2,
                "total": 2, "failed": 1, "dataset": {"id": "emotion"},
                "started_at": "2026-11-21T10:00:00+00:00"}
        if grid:
            meta["thetas"] = [0.2, 0.4, 0.6]
            meta["ks"] = [5, 10, 20]
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False),
                                     encoding="utf-8")
    return d


def test_run_movies_endpoint(client):
    """GET /runs/{id}/movies：包层三键来自 meta thetas/ks（0.4/10/20）；
    行序失败置顶（输入序 A,B → 输出 B,A）；摘要行十键齐全 + 指标手算值。"""
    _seed_dataset_and_library(client)
    _write_movies_run(client)
    r = client.get(f"{_BASE}/runs/{_MOVIES_RUN}/movies")
    assert r.status_code == 200
    body = r.json()
    assert (body["theta_used"], body["k_p"], body["k_r"]) == (0.4, 10, 20)
    assert [row["tt"] for row in body["rows"]] == ["tt0000002", "tt0000001"]
    fail, hit = body["rows"]
    assert fail["ok"] is False and fail["error"] == "boom"
    assert fail["p_at_k"] is None and fail["r_at_k"] is None
    assert fail["spearman"] is None and fail["n_relevant"] == 1
    assert hit["ok"] is True
    assert hit["p_at_k"] == pytest.approx(0.2)  # 2 命中 / k=10
    assert hit["r_at_k"] == pytest.approx(2 / 3)  # 2 / 3 个相关
    assert hit["spearman"] == pytest.approx(0.4)
    assert (hit["n_produced"], hit["n_relevant"]) == (4, 3)
    # 摘要行十键契约（不多不少）
    assert {"tt", "title", "ok", "error", "spearman", "p_at_k", "r_at_k",
            "elapsed_ms", "n_produced", "n_relevant"} == set(hit)


def test_run_movie_detail_endpoint(client):
    """GET /runs/{id}/movies/{tt}：三表齐全 + hit 标记 + misses 名称回填；
    防御重排（produced 原始序 t2 在前 → t1 在前）；失败片三表全空；
    未知 tt → 404。"""
    _seed_dataset_and_library(client)
    _write_movies_run(client)
    r = client.get(f"{_BASE}/runs/{_MOVIES_RUN}/movies/tt0000001")
    assert r.status_code == 200
    body = r.json()
    assert body["tt"] == "tt0000001" and body["ok"] is True
    assert (body["theta_used"], body["k_p"], body["k_r"]) == (0.4, 10, 20)
    assert [p["tag_id"] for p in body["produced"]] == ["t1", "t2", "t9"]
    assert [p["hit"] for p in body["produced"]] == [True, True, False]
    assert body["produced"][0]["gt_score"] == 0.9
    assert body["produced"][0]["tag_name"] == "悬疑"
    assert body["produced"][2]["gt_score"] == 0.1  # 产出但 <θ → 不命中
    assert set(body["produced"][0]) == {"tag_id", "tag_name", "sf",
                                        "confidence", "evidence_count",
                                        "gt_score", "hit"}
    assert body["out_of_space"] == [
        {"tag_id": "zz", "tag_name": "噪声", "sf": 0.99}]
    assert body["misses"] == [
        {"tag_id": "t7", "tag_name": "喜剧", "gt_score": 0.85}]
    # 失败片：三表全空、error 原样、档位键仍在
    r2 = client.get(f"{_BASE}/runs/{_MOVIES_RUN}/movies/tt0000002")
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["ok"] is False and d2["error"] == "boom"
    assert (d2["produced"] == [] and d2["out_of_space"] == []
            and d2["misses"] == [])
    assert (d2["theta_used"], d2["k_p"], d2["k_r"]) == (0.4, 10, 20)
    # 未知 tt → 404
    r3 = client.get(f"{_BASE}/runs/{_MOVIES_RUN}/movies/tt9999999")
    assert r3.status_code == 404
    assert "tt9999999" in r3.json()["detail"]


def test_movies_404_when_raw_results_missing_or_truncated(client):
    """读侧容错：raw_results.json 缺失 / 截断 / 非 list → 404「无单片明细」
    （运行中 run 与 meta→raw 写序非原子窗口，不 500）；摘要与明细端点
    同口径。"""
    _seed_dataset_and_library(client)
    d = _write_movies_run(client, write_raw=False)
    r = client.get(f"{_BASE}/runs/{_MOVIES_RUN}/movies")
    assert r.status_code == 404
    assert "无单片明细" in r.json()["detail"]
    # 截断 JSON → 同 404（两端点都断言）
    (d / "raw_results.json").write_text("{截断", encoding="utf-8")
    r2 = client.get(f"{_BASE}/runs/{_MOVIES_RUN}/movies")
    assert r2.status_code == 404 and "无单片明细" in r2.json()["detail"]
    r2d = client.get(f"{_BASE}/runs/{_MOVIES_RUN}/movies/tt0000001")
    assert r2d.status_code == 404 and "无单片明细" in r2d.json()["detail"]
    # 合法 JSON 但非 list → 同 404
    (d / "raw_results.json").write_text('{"tt": "x"}', encoding="utf-8")
    r3 = client.get(f"{_BASE}/runs/{_MOVIES_RUN}/movies")
    assert r3.status_code == 404 and "无单片明细" in r3.json()["detail"]


def test_movies_404_when_rows_not_dict_shaped(client):
    """行级形状守卫（终审 Important）：raw_results.json 是合法 JSON list
    但行不是 dict（['abc'] → AttributeError）或行缺 tt 键（[{'nott': 1}]
    → KeyError）→ 同 404「无单片明细」，不 500（读侧容错承诺延伸到行级，
    两端点同口径）。"""
    _seed_dataset_and_library(client)
    d = _write_movies_run(client, rows=["abc"])
    r = client.get(f"{_BASE}/runs/{_MOVIES_RUN}/movies")
    assert r.status_code == 404 and "无单片明细" in r.json()["detail"]
    r2 = client.get(f"{_BASE}/runs/{_MOVIES_RUN}/movies/tt0000001")
    assert r2.status_code == 404 and "无单片明细" in r2.json()["detail"]
    # 行是 dict 但缺 tt 键 → 同 404
    (d / "raw_results.json").write_text('[{"nott": 1}]', encoding="utf-8")
    r3 = client.get(f"{_BASE}/runs/{_MOVIES_RUN}/movies")
    assert r3.status_code == 404 and "无单片明细" in r3.json()["detail"]


def test_movies_default_grid_when_meta_missing(client):
    """meta.json 缺失 / 有 meta 但无 thetas·ks 键（老 run）→ DEFAULT 档
    照常出数：0.4/10/20（DEFAULT_THETAS→0.4、DEFAULT_KS→k_p 10/k_r 20）。"""
    _seed_dataset_and_library(client)
    _write_movies_run(client, write_meta=False)
    r = client.get(f"{_BASE}/runs/{_MOVIES_RUN}/movies")
    assert r.status_code == 200
    body = r.json()
    assert (body["theta_used"], body["k_p"], body["k_r"]) == (0.4, 10, 20)
    assert len(body["rows"]) == 2  # 指标照算（rows 有数）
    # meta 存在但缺 thetas/ks 键 → 同回落
    _write_movies_run(client, run_id="run-movies-oldmeta", grid=False)
    body2 = client.get(f"{_BASE}/runs/run-movies-oldmeta/movies").json()
    assert (body2["theta_used"], body2["k_p"], body2["k_r"]) == (0.4, 10, 20)
    # meta 只有 ks 无 thetas → 两键独立回落：thetas 走 DEFAULT（0.4），
    # ks 照用（[5] 唯一元素即 P/R 两目标的最近档 → k_p=k_r=5）（终审 Minor）
    out = Path(client.app.state.settings.eval_output_root)
    d3 = out / "run-movies-ks-only"
    d3.mkdir()
    (d3 / "raw_results.json").write_text(
        json.dumps(_movies_rows(), ensure_ascii=False), encoding="utf-8")
    (d3 / "meta.json").write_text(
        json.dumps({"run_id": "run-movies-ks-only", "ks": [5]},
                   ensure_ascii=False), encoding="utf-8")
    body3 = client.get(f"{_BASE}/runs/run-movies-ks-only/movies").json()
    assert body3["theta_used"] == 0.4  # thetas 缺 → DEFAULT 档
    assert body3["k_p"] == 5 and body3["k_r"] == 5  # ks=[5] 照用


def test_movies_non_admin_401(client):
    """非 admin 门槛（两端点各一）：登录 reviewer → 403（require_admin 对
    非 admin 角色记 403——沿用现有端点断言模式）；未登录 → 401（内层
    get_current_user 先行短路）。"""
    _seed_dataset_and_library(client)
    _write_movies_run(client)
    assert client.post("/api/v1/users", json={
        "username": "bob", "password": "pw-bob-1", "role": "reviewer"
    }).status_code == 200
    client.post("/api/v1/auth/logout")
    client.post("/api/v1/auth/login",
                json={"username": "bob", "password": "pw-bob-1"})
    assert client.get(f"{_BASE}/runs/{_MOVIES_RUN}/movies").status_code == 403
    assert client.get(
        f"{_BASE}/runs/{_MOVIES_RUN}/movies/tt0000001").status_code == 403
    # 未登录 → 401
    client.post("/api/v1/auth/logout")
    assert client.get(f"{_BASE}/runs/{_MOVIES_RUN}/movies").status_code == 401
    assert client.get(
        f"{_BASE}/runs/{_MOVIES_RUN}/movies/tt0000001").status_code == 401


def test_movies_run_dir_guard(client):
    """run_id 路径守卫（同 files 端点守卫模式）：越界形态 run_id → 404。
    `..%2F` 解码后含 / 路由段即不匹配；反斜杠/绝对路径形态能进端点、被
    `_run_dir` 守卫拒——这两种断言 detail 含「不存在」（守卫 404，非
    路由裸 Not Found）。"""
    _seed_dataset_and_library(client)
    assert client.get(f"{_BASE}/runs/..%2Fescape/movies").status_code == 404
    r = client.get(f"{_BASE}/runs/..%5Cescape/movies")
    assert r.status_code == 404 and "不存在" in r.json()["detail"]
    r2 = client.get(f"{_BASE}/runs/..%5Cescape/movies/tt0000001")
    assert r2.status_code == 404 and "不存在" in r2.json()["detail"]
    r3 = client.get(f"{_BASE}/runs/C:%5CWindows%5Ctemp/movies")
    assert r3.status_code == 404 and "不存在" in r3.json()["detail"]
