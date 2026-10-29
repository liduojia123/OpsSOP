"""应用日志文件输出(可选):按天轮转,按 LOG_RETENTION_DAYS 删除过期文件。

Docker json-file 驱动只按大小轮转(10MB × 5),不按时间保留:低流量时旧日志
可能留存远超 30 天,高流量时不到一天就被覆盖。需要 30 天日志留存时,开启
LOG_FILE_ENABLED 把应用日志同时写入挂载目录,由本模块按天轮转并清理过期
文件。默认关闭,不改变现有 stdout 日志采集;也可以不改配置,由宿主
logrotate 或日志平台负责(见 docs/deployment.md)。
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from app.config import Settings

logger = logging.getLogger("opssop.logging")

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def build_file_handler(settings: Settings) -> TimedRotatingFileHandler:
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = TimedRotatingFileHandler(
        filename=log_dir / "opssop.log",
        when="midnight",
        backupCount=max(1, settings.log_retention_days),
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(logging.Formatter(_FORMAT))
    return handler


def setup_file_logging(settings: Settings) -> bool:
    """开启时给 root logger 挂 stderr + 按天轮转文件 handler,并把级别提到 INFO。

    uvicorn 自己的日志器不向上传播,保持原有 stdout 行为;root 原本没有
    handler(opssop.* 的 INFO 记录被丢弃、WARNING 靠 lastResort 兜底),
    显式挂 stderr handler 保证开启文件日志后容器日志不丢内容。
    目录不可写等失败只降级为继续使用 stdout,不阻断启动。
    """
    if not settings.log_file_enabled:
        return False
    try:
        handler = build_file_handler(settings)
    except OSError:
        logger.warning("启用文件日志失败,继续使用 stdout 日志", exc_info=True)
        return False
    root = logging.getLogger()
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(stream)
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    logger.info(
        "文件日志已启用: dir=%s retention_days=%s",
        settings.log_dir,
        settings.log_retention_days,
    )
    return True
