"""数据集评测的选片、并发执行、进度和历史生命周期。"""
from __future__ import annotations

import json
import logging
import os
import random
import secrets
import shutil
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Generic, TypeVar

logger = logging.getLogger(__name__)

PRESET_SIZE = 30
PRESET_SEED = 20261120


def select_movies(usable: list[str], mode: str,
                  custom: list[str] | None = None, *,
                  size: int = PRESET_SIZE,
                  seed: int = PRESET_SEED) -> list[str]:
    """按模式从 usable 选片，返回排序去重列表。

    - `preset30`：`sorted` 后 `random.Random(seed).sample(..., min(size, len))`
      ——不足 size 全取；固定 seed 保证换机器重跑抽到同一批片。sample 的
      输出顺序随输入顺序漂移，末尾再排序让结果与输入顺序无关；
    - `all`：全部（排序去重）；
    - `custom`：只接受 usable 子集，越权 id → ValueError 列出未知项
      （前端只发可见列表，越权 = 调用方 bug，早失败不静默缩圈）。
    """
    pool = sorted(set(usable))
    if mode == "preset30":
        return sorted(random.Random(seed).sample(pool, min(size, len(pool))))
    if mode == "all":
        return pool
    if mode == "custom":
        chosen = set(custom or [])
        unknown = sorted(chosen - set(pool))
        if unknown:
            raise ValueError(f"custom 选片含 usable 之外的影片 id：{unknown}")
        return sorted(chosen)
    raise ValueError(f"未知选片模式：{mode!r}（支持 preset30 / all / custom）")


# 单进程默认并发：与主模型网关的批处理并发（4）对齐，131 部全量评测不压垮网关
DEFAULT_CONCURRENCY = 4
# 每片重试次数（含首发共 1+2=3 次尝试）：单片偶发失败（超时/解析抖动）不断跑
RETRY_PER_MOVIE = 2
# 完成序连续失败达该数 → 中止余量：系统性故障保险丝（网关整体不可用时
# 别把整批烧完），单片失败远达不到该阈值
ABORT_CONSECUTIVE = 5


class RunBusyError(RuntimeError):
    """已有评测 run 进行中（单进程同时只允许一个 run，重复起跑显式拒绝）。"""


class RunActiveError(RuntimeError):
    """run 仍活跃（worker 在跑或在终态落盘收尾），不可删除——先取消/等收尾。"""


class EvalValidationError(ValueError):
    """评测起跑校验失败：数据集不存在 / 选片越权或为空 / 库未就绪 / 库∩gt 零交集。"""


@dataclass
class MovieResult:
    """单片生成或评估的结果。"""

    tt: str
    title: str
    ok: bool
    error: str | None
    produced: list[dict]
    elapsed_ms: int


@dataclass
class RunState:
    """一次 run 的进度快照（`get` 返回副本，读方无锁安全）。

    finished_at：终态时刻（_worker 落终态时盖章，运行中 None）；磁盘侧
    meta.json 同名键由 _write_artifacts 终态写盘（老 run 无此键 → None）。
    """

    run_id: str
    status: str
    done: int
    total: int
    failed: int
    started_at: str
    error: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class EvalJob:
    run_id: str
    movies: list[str]
    concurrency: int
    started_at: str
    meta: dict
    dataset_root: str


def now_iso() -> str:
    """UTC ISO 时间戳（含微秒：历史按 started_at 排序需可分辨同秒 run）。"""
    return datetime.now(timezone.utc).isoformat()


def dump_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


_Job = TypeVar("_Job", bound=EvalJob)
_Result = TypeVar("_Result", bound=MovieResult)


class EvalRunService(ABC, Generic[_Job, _Result]):
    """进程内跟踪活跃运行，磁盘保留进度和历史。"""

    def __init__(self, output_root: str, dataset_root: str) -> None:
        self.output_root = Path(output_root)
        self.default_dataset_root = dataset_root
        self._lock = threading.Lock()
        self._runs: dict[str, RunState] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._active_run_id: str | None = None
        self.dataset_root_override: str | None = None

    @property
    def effective_root(self) -> str:
        return (self.dataset_root_override if self.dataset_root_override is not None
                else self.default_dataset_root)

    def run_directory(self, run_id: str) -> Path | None:
        path = Path(run_id)
        if path.anchor or len(path.parts) != 1 or path.name != run_id or run_id == "..":
            return None
        try:
            candidate = self.output_root.resolve() / run_id
            resolved = candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        return resolved if resolved == candidate else None

    def _launch(self, selected: list[str], make_job: Callable[[str, str], _Job],
                *, wait: bool) -> str:
        # Busy 检查 + 注册（同一锁内原子完成：并发 start 只放行一个）
        with self._lock:
            if self._active_run_id is not None:
                raise RunBusyError(f"已有评测 run 进行中：{self._active_run_id}")
            run_id = self._new_run_id()
            started_at = now_iso()
            self._runs[run_id] = RunState(
                run_id=run_id, status="running", done=0, total=len(selected),
                failed=0, started_at=started_at)
            self._cancel_events[run_id] = threading.Event()
            self._active_run_id = run_id

        run_dir = self.output_root / run_id
        try:
            job = make_job(run_id, started_at)
            run_dir.mkdir(parents=True)
            dump_json(run_dir / "meta.json",
                      {**job.meta, "status": "running", "done": 0,
                       "total": len(selected), "failed": 0,
                       "started_at": started_at, "finished_at": None})
            thread = threading.Thread(target=self._worker, args=(job,),
                                      name=f"eval-{run_id}", daemon=True)
            thread.start()
        except Exception as exc:
            with self._lock:
                state = self._runs.get(run_id)
                if state is not None:
                    state.status = "failed"
                    state.error = f"run 启动失败：{exc}"
                self._cancel_events.pop(run_id, None)
                if self._active_run_id == run_id:
                    self._active_run_id = None
            raise
        if wait:
            thread.join()
        return run_id

    def get(self, run_id: str) -> RunState | None:
        """run 进度快照（副本）；未知 run_id → None。"""
        with self._lock:
            state = self._runs.get(run_id)
            return None if state is None else replace(state)

    def cancel(self, run_id: str) -> bool:
        """请求取消：置事件后 worker 不再提交新片（在途片跑完）。

        返回是否受理（run 不存在或已终态 → False）。幂等：重复取消
        已终态的 run 返回 False，不影响已落盘结果。
        """
        with self._lock:
            state = self._runs.get(run_id)
            event = self._cancel_events.get(run_id)
            if state is None or event is None or state.status != "running":
                return False
            event.set()
            return True

    def delete(self, run_id: str) -> bool:
        """删除指定的终态运行和对应磁盘产物。"""
        run_dir = self.run_directory(run_id)
        if run_dir is None:
            return False
        with self._lock:
            active = self._active_run_id
            if active is not None and Path(active) == Path(run_dir.name):
                raise RunActiveError(
                    f"运行中的 run 不可删除（先取消或等收尾）：{run_id}")
            run_id = run_dir.name
            registered = run_id in self._runs
            has_metadata = (run_dir / "meta.json").is_file()
            if not registered and not has_metadata:
                return False
            if run_dir.is_dir():
                shutil.rmtree(run_dir)
            self._runs.pop(run_id, None)
            self._cancel_events.pop(run_id, None)
            return True

    def active_run_id(self) -> str | None:
        """当前进行中的 run_id（无 → None）。"""
        with self._lock:
            return self._active_run_id

    def history(self) -> list[dict]:
        """历史扫描：`eval_output_root/*/meta.json` → 摘要行（started_at 降序）。

        磁盘是持久层、注册表是进程内存：meta.status=running 且不在注册表 →
        进程已死，标 `interrupted`（与正常终态在磁盘上可区分，spec §6）。
        读侧容错（宁缺勿崩）：meta 缺失/损坏的 run 跳过；report.json 缺失或
        截断（meta→raw→report 写序的非原子窗口）→ 摘要四键为 None，不抛
        异常。摘要字段由对应评测报告提供。
        """
        root = self.output_root
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return []
        rows = []
        for name in names:
            try:
                meta = json.loads((root / name / "meta.json")
                                  .read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            stored_id = meta.get("run_id")
            run_id = stored_id if isinstance(stored_id, str) and stored_id else name
            state = self.get(run_id)
            if state is not None:
                meta.update(vars(state))
            elif meta.get("status") == "running":
                meta["status"] = "interrupted"
            dataset = meta.get("dataset")
            rows.append({
                "run_id": run_id,
                "dataset_id": dataset.get("id") if isinstance(dataset, dict) else None,
                "status": meta.get("status"),
                "done": meta.get("done"),
                "total": meta.get("total"),
                "failed": meta.get("failed"),
                "started_at": meta.get("started_at"),
                "finished_at": meta.get("finished_at"),
                "error": meta.get("error"),
                **self._read_summary(root / name),
            })
        rows.sort(key=lambda row: row.get("started_at") or "", reverse=True)
        return rows

    def _worker(self, job: _Job) -> None:
        """daemon worker 线程体：收敛 → 终态落盘 → 释放 busy 位。

        任何意外（含收敛循环自身异常）都落终态、不滞留 running：
        status=failed + error 留痕，已完成的结果照常写盘。
        """
        results: list[_Result] = []
        status, error = "failed", None
        try:
            status = self._converge(job, results)
        except Exception as exc:  # noqa: BLE001 —— worker 兜底：不滞留 running
            error = f"{type(exc).__name__}: {exc}"
        # 完成产物写入后再通知轮询方进入终态。
        finished_at = now_iso()
        with self._lock:
            self._cancel_events.pop(job.run_id, None)
        try:
            self._write_artifacts(job, results, status, error, finished_at)
        except Exception as exc:
            status, error = "failed", f"评测产物写入失败：{type(exc).__name__}: {exc}"
            logger.exception("评测 run %s 终态落盘失败", job.run_id)
        finally:
            with self._lock:
                state = self._runs[job.run_id]
                state.status = status
                state.finished_at = finished_at
                state.error = error
                self._active_run_id = None

    def _converge(self, job: _Job, results: list[_Result]) -> str:
        """主收敛循环：滑动提交 + 完成序收敛，返回终态 status。

        停止条件（停止后余量片从未进池、在途片跑完、结果照记）：
        - 完成序连续 `ABORT_CONSECUTIVE` 失败 → aborted；
        - cancel 事件置位 → cancelled。
        停止后到达的结果仍记入（增量落盘：停/崩不丢已完成片），但不再
        参与连败计数（终态已定，计数无意义）。
        """
        event = self._cancel_events[job.run_id]
        order = job.movies
        inflight: dict[Future, str] = {}
        next_index = 0
        consecutive = 0
        aborted = False
        with ThreadPoolExecutor(max_workers=job.concurrency,
                                thread_name_prefix="eval-movie") as pool:
            while True:
                stopping = aborted or event.is_set()
                if not stopping:
                    # 按 tt 序滑动补位：停止判定先于提交，余量片不进池
                    while (next_index < len(order)
                           and len(inflight) < job.concurrency):
                        future = pool.submit(self._run_movie, job, order[next_index])
                        inflight[future] = order[next_index]
                        next_index += 1
                elif inflight:
                    self._cancel_pending(inflight)
                if not inflight:
                    break
                done, _not_done = wait(set(inflight),
                                       return_when=FIRST_COMPLETED)
                # 同刻多完成按 tt 序处理：完成序语义对任何合法完成序成立，
                # 固定序只为输出可复现
                for future in sorted(done, key=lambda f: inflight[f]):
                    inflight.pop(future)
                    if future.cancelled():
                        continue  # 余量取消：未启动的片不算完成
                    result = future.result()
                    results.append(result)
                    self._record_result(job.run_id, result)
                    if aborted or event.is_set():
                        continue
                    consecutive = 0 if result.ok else consecutive + 1
                    if consecutive >= ABORT_CONSECUTIVE:
                        aborted = True
                        self._cancel_pending(inflight)
        if aborted:
            return "aborted"
        if event.is_set():
            return "cancelled"
        return "succeeded"

    @staticmethod
    def _cancel_pending(inflight: dict[Future, str]) -> None:
        """取消未启动的余量 future（已取消/在途的留在 inflight 由循环收敛）。"""
        for future in list(inflight):
            if future.cancel():
                inflight.pop(future)

    def _record_result(self, run_id: str, result: MovieResult) -> None:
        """锁内更新注册表并追加 progress.jsonl 一行（增量落盘）。"""
        state = self._runs.get(run_id)
        if state is None:
            return
        line = {"tt": result.tt, "ok": result.ok,
                "produced": result.produced, "elapsed_ms": result.elapsed_ms}
        if result.error is not None:
            line["error"] = result.error
        payload = json.dumps(line, ensure_ascii=False)
        with self._lock:
            state.done += 1
            if not result.ok:
                state.failed += 1
            with open(self.output_root / run_id / "progress.jsonl", "a", encoding="utf-8") as f:
                f.write(payload + "\n")

    def _new_run_id(self) -> str:
        """`run-YYYYMMDD-HHMMSS-xxxx`：时间戳人读 + 2 字节随机防同秒撞名。

        输出根已有同名目录（撞名/残留）时换 token 重试，8 次仍撞 →
        EvalValidationError（多半是输出根异常，显式失败优于静默并入）。
        """
        for _ in range(8):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            run_id = f"run-{stamp}-{secrets.token_hex(2)}"
            if not (self.output_root / run_id).exists():
                return run_id
        raise EvalValidationError(
            "run_id 生成重试 8 次仍撞名，请检查 eval_output_root")

    @abstractmethod
    def _run_movie(self, job: _Job, tt: str) -> _Result:
        raise NotImplementedError

    @abstractmethod
    def _read_summary(self, run_dir: Path) -> dict:
        raise NotImplementedError

    @abstractmethod
    def _write_artifacts(self, job: _Job, results: list[_Result],
                         status: str, error: str | None, finished_at: str) -> None:
        raise NotImplementedError
