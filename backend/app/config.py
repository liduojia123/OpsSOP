"""应用配置:环境变量优先,不从数据库读取密钥。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 数据库
    postgres_user: str = "opssop"
    postgres_password: str = ""
    postgres_db: str = "opssop"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_test_db: str = "opssop_test"

    # 后端
    secret_key: str = "change-me-random-secret"
    session_expire_hours: int = 8
    opssop_env: str = "development"  # development | production

    # 初始账号
    opssop_admin_username: str = "admin"
    opssop_admin_password: str = "change-me-admin-password"
    opssop_seed_demo: bool = False
    opssop_demo_operator_password: str = "demo-operator-password"
    opssop_demo_reviewer_password: str = "demo-reviewer-password"

    # AI 聊天模型配置
    ai_mode: str = "disabled"  # disabled | demo | cloud | local
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    ai_timeout_seconds: int = 60
    # 能力配置:不同服务对结构化输出/usage 支持不一致,默认保守,由管理员显式开启
    ai_supports_json_mode: bool = False  # response_format={"type":"json_object"}
    ai_supports_usage: bool = False  # 目标服务会返回 usage 字段(仅作提示;返回了就记录)
    # 上下文与输出限制(单次分析)
    ai_max_context_chars: int = 24000  # 发送给模型的输入上限(脱敏后)
    ai_max_output_tokens: int = 2048
    # 单用户限流(单实例内存计数;多实例部署需外部限流,文档已注明)
    ai_rate_limit_per_minute: int = 6
    ai_max_concurrent_per_user: int = 2

    # embedding 独立配置(与聊天模型独立)
    embedding_mode: str = "disabled"  # disabled | cloud | local
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_timeout_seconds: int = 30
    embedding_batch_size: int = 16  # 单次 embed 请求的最大文本条数
    embedding_max_input_chars: int = 6000  # 单条文本进入 embedding 前的长度上限(脱敏后)
    # 查询向量独立限流/并发(与聊天 AI_RATE_LIMIT 分离:
    # 检索与分析不得互相挤占配额,向量限流时降级关键词,不影响分析额度)
    embedding_rate_limit_per_minute: int = 30
    embedding_max_concurrent_per_user: int = 4

    # 启用后禁止聊天与 embedding 使用 cloud 配置(应用级约束,不等于网络隔离)
    local_only: bool = False

    # 反向代理:仅信任这些网段传来的 X-Forwarded-For / X-Real-IP(逗号分隔 CIDR)
    # 留空表示无代理,忽略转发头;Docker 部署由 compose 传入容器网络网段
    trusted_proxy_cidrs: str = ""

    # 平台内置备份(单实例;生产环境启动每日任务)
    backup_enabled: bool = True
    backup_dir: str = "/data/OpsSop/BackUp"
    backup_hour: int = 2  # 服务器本地时间(容器 TZ),0-23
    backup_retention_days: int = 30

    # 审计日志保留;业务知识数据不自动删除
    audit_retention_days: int = 30

    # 邮件发送接口(默认 disabled,只预留;启用 smtp 后才会外发)
    email_mode: str = "disabled"  # disabled | smtp
    email_host: str = ""
    email_port: int = 587
    email_username: str = ""
    email_password: str = ""
    email_from: str = ""
    email_use_tls: bool = True

    def _build_url(self, database: str) -> str:
        """用 SQLAlchemy URL 对象拼连接串,特殊字符密码(如 p@ss/wo%rd)被正确转义。"""
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=database,
        ).render_as_string(hide_password=False)

    @property
    def database_url(self) -> str:
        return self._build_url(self.postgres_db)

    @property
    def test_database_url(self) -> str:
        return self._build_url(self.postgres_test_db)

    @property
    def is_production(self) -> bool:
        return self.opssop_env.lower() == "production"

    @property
    def ai_config_version(self) -> str:
        """模型配置版本:绑定分析请求与响应,配置变化后旧结果标记过期。

        不含密钥内容;密钥轮换但端点/模型不变时版本不变(输出契约不变)。
        """
        import hashlib

        raw = "|".join(
            [
                self.ai_mode,
                self.ai_base_url,
                self.ai_model,
                str(self.ai_supports_json_mode),
                str(self.ai_max_output_tokens),
                str(self.ai_max_context_chars),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    @property
    def knowledge_version_seed(self) -> str:
        """知识版本种子:与检索缓存键组合使用(实际版本由知识表状态计算)。"""
        return self.ai_config_version

    def validate_production_config(self) -> None:
        """生产启动前的配置预检(在 Alembic 迁移之前执行)。

        只检查配置本身,不连接数据库。遗留账号检查仍由 ensure_seed 在迁移后执行。
        """
        if not self.is_production:
            return
        from app.seed import WEAK_PASSWORDS

        problems: list[str] = []
        if self.opssop_admin_password in WEAK_PASSWORDS:
            problems.append("OPSSOP_ADMIN_PASSWORD 仍是示例默认口令")
        if len(self.opssop_admin_password) < 12:
            problems.append("OPSSOP_ADMIN_PASSWORD 长度不足 12")
        if self.secret_key in WEAK_PASSWORDS:
            problems.append("SECRET_KEY 仍是示例默认值")
        if len(self.secret_key) < 32:
            problems.append("SECRET_KEY 长度不足 32")
        if self.opssop_seed_demo:
            problems.append("OPSSOP_SEED_DEMO 必须为 0")
        if problems:
            raise ValueError("生产配置预检失败: " + "; ".join(problems))

    def validate_backup_config(self) -> None:
        """备份、保留与邮件接口的配置预检(不连接数据库、不访问文件系统)。"""
        if not 0 <= self.backup_hour <= 23:
            raise ValueError(f"BACKUP_HOUR 必须在 0-23 之间,当前: {self.backup_hour}")
        if self.backup_retention_days < 1:
            raise ValueError("BACKUP_RETENTION_DAYS 必须至少为 1")
        if self.audit_retention_days < 1:
            raise ValueError("AUDIT_RETENTION_DAYS 必须至少为 1")
        if self.backup_enabled and not self.backup_dir.strip():
            raise ValueError("BACKUP_ENABLED=1 时必须配置 BACKUP_DIR")
        if self.email_mode not in ("disabled", "smtp"):
            raise ValueError("EMAIL_MODE 必须是 disabled 或 smtp")
        if self.email_mode == "smtp" and (not self.email_host.strip() or not self.email_from.strip()):
            raise ValueError("EMAIL_MODE=smtp 时需要配置 EMAIL_HOST 和 EMAIL_FROM")

    def validate_ai_config(self) -> None:
        """启动时校验 AI 配置冲突;LOCAL_ONLY 下禁止 cloud 模式。"""
        if self.local_only and self.ai_mode == "cloud":
            raise ValueError("LOCAL_ONLY=1 时禁止 AI_MODE=cloud:禁止聊天模型外发")
        if self.local_only and self.embedding_mode == "cloud":
            raise ValueError("LOCAL_ONLY=1 时禁止 EMBEDDING_MODE=cloud:禁止 embedding 外发")
        valid_ai = {"disabled", "demo", "cloud", "local"}
        valid_emb = {"disabled", "cloud", "local"}
        if self.ai_mode not in valid_ai:
            raise ValueError(f"AI_MODE 必须是 {sorted(valid_ai)} 之一,当前: {self.ai_mode}")
        if self.embedding_mode not in valid_emb:
            raise ValueError(
                f"EMBEDDING_MODE 必须是 {sorted(valid_emb)} 之一,当前: {self.embedding_mode}"
            )
        if self.ai_mode in ("cloud", "local") and not self.ai_base_url:
            raise ValueError(f"AI_MODE={self.ai_mode} 需要配置 AI_BASE_URL")
        if self.ai_mode == "local":
            from app.ai.llm import _is_trusted_local_target

            if not _is_trusted_local_target(self.ai_base_url):
                raise ValueError(
                    "AI_MODE=local 的 AI_BASE_URL 不是受信任的本机/内网地址;请管理员修正配置"
                )
        # embedding 独立校验(与聊天同一约束:LOCAL_ONLY 已在上方覆盖 cloud 冲突)
        if self.embedding_mode in ("cloud", "local") and not self.embedding_base_url:
            raise ValueError(
                f"EMBEDDING_MODE={self.embedding_mode} 需要配置 EMBEDDING_BASE_URL"
            )
        if self.embedding_mode == "local" and self.embedding_base_url:
            from app.ai.llm import _is_trusted_local_target

            if not _is_trusted_local_target(self.embedding_base_url):
                raise ValueError(
                    "EMBEDDING_MODE=local 的 EMBEDDING_BASE_URL 不是受信任的本机/内网地址;请管理员修正配置"
                )
        if self.embedding_mode != "disabled" and not self.embedding_model:
            raise ValueError("EMBEDDING_MODE 启用时需要配置 EMBEDDING_MODEL")


@lru_cache
def get_settings() -> Settings:
    return Settings()
