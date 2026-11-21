"""标签文件解析与后台导入任务。

四层重构 Task 6 起住 tasks/（执行器横切层）：TagImportExecutor 是导入
任务的线程池执行器（提交/复位/关停），parse_upload 是上传文件的格式
分发薄壳（xml/xlsx 判定 + 错误转译）——真正的解析与治理业务在
`tagplatform.services.tags.governance`（Task 8 归位），本模块
零业务实现。
"""

import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from tagplatform.capabilities.storage.models import TagImportTask
from tagplatform.capabilities.embed import Embedder
from tagplatform.services.tags.governance import ImportParseResult, parse_tag_excel, parse_tag_xml, run_governance

logger = logging.getLogger(__name__)


class TagImportFileError(ValueError):
    """可向上传者展示的文件格式或表头错误。"""


def parse_upload(data: bytes, file_name: str) -> ImportParseResult:
    name = file_name.lower()
    is_xml = name.endswith(".xml") or data.lstrip().startswith(b"<")
    if not is_xml and not name.endswith(".xlsx"):
        raise TagImportFileError("仅支持 .xlsx 或 .xml 文件")
    try:
        parsed = parse_tag_xml(data) if is_xml else parse_tag_excel(data)
    except Exception as exc:
        logger.exception("标签文件解析失败: %s", file_name)
        raise TagImportFileError("文件无法解析为有效的 XML 或 xlsx 文件") from exc
    if parsed.header_error:
        raise TagImportFileError(parsed.header_error)
    return parsed


class TagImportExecutor:
    def __init__(self, session_factory: sessionmaker[Session], embedder: Embedder):
        self._session_factory = session_factory
        self._embedder = embedder
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tag-import")

    def reset_interrupted(self) -> None:
        with self._session_factory.begin() as session:
            session.execute(
                update(TagImportTask).where(TagImportTask.status == "processing").values(
                    status="failed",
                    result_summary=json.dumps({"error": "服务重启中断了导入，请重新上传文件"},
                                              ensure_ascii=False),
                )
            )

    def submit(self, file_name: str, data: bytes) -> TagImportTask:
        task = TagImportTask(
            task_id=f"task_{uuid.uuid4().hex[:8]}",
            file_name=file_name,
            status="processing",
            created_at=datetime.now(timezone.utc),
        )
        with self._session_factory.begin() as session:
            session.add(task)
        try:
            self._pool.submit(self._execute, task.task_id, file_name, data)
        except RuntimeError:
            logger.exception("无法提交标签导入任务 %s", task.task_id)
            self._fail(task.task_id, "无法启动导入，请重新上传文件")
            task.status = "failed"
        return task

    def _execute(self, task_id: str, file_name: str, data: bytes) -> None:
        try:
            parsed = parse_upload(data, file_name)
            with self._session_factory() as session:
                task = session.get(TagImportTask, task_id)
                if task is None or task.status != "processing":
                    return
                run_governance(parsed.rows, session, self._embedder, task=task,
                               parse_errors=parsed.errors)
        except TagImportFileError as exc:
            self._fail(task_id, str(exc))
        except Exception:
            logger.exception("标签导入任务失败: %s", task_id)
            self._fail(task_id, "导入处理失败，请重新上传文件或联系管理员")

    def _fail(self, task_id: str, message: str) -> None:
        with self._session_factory.begin() as session:
            session.execute(
                update(TagImportTask).where(
                    TagImportTask.task_id == task_id, TagImportTask.status == "processing",
                ).values(
                    status="failed", result_summary=json.dumps({"error": message}, ensure_ascii=False),
                )
            )

    def shutdown(self) -> None:
        self._pool.shutdown()
