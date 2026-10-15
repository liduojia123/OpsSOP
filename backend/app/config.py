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

    # AI 聊天模型(第二阶段接入;当前仅暴露配置与状态)
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

    # embedding 独立配置
    embedding_mode: str = "disabled"  # disabled | cloud | local
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""

    # 启用后禁止聊天与 embedding 使用 cloud 配置(应用级约束,不等于网络隔离)
    local_only: bool = False

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
