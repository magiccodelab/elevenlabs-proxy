"""配置加载（pydantic-settings）。所有敏感项走 .env。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 上游 ElevenLabs（Web 私有 API，US 区）
    elevenlabs_base_url: str = "https://api.us.elevenlabs.io"
    elevenlabs_origin: str = "https://elevenlabs.io"

    # 凭证（按优先级使用）
    # 1) refresh_token 持久化在 token_state_path（程序自动维护）
    # 2) email + password：首次登录用，之后自动续期
    # 3) bearer_token：兜底静态 token（无续期，~1h 失效）
    elevenlabs_email: str | None = None
    elevenlabs_password: str | None = None
    elevenlabs_bearer_token: str | None = None

    # Firebase（留空则启动时从 elevenlabs.io 主页自动发现）
    firebase_api_key: str | None = None
    token_state_path: str = ".auth.json"
    # 提前续期（默认在过期前 5 分钟刷新）
    refresh_safety_seconds: int = 300

    # 出站代理（强制；启动期会校验出口 IP）
    proxy_url: str = "socks5://127.0.0.1:7899"
    expected_egress_ip: str = "38.15.30.162"

    # 浏览器指纹
    impersonate: str = "chrome131"
    accept_language: str = "en-US,en;q=0.9"

    # TTS 默认
    default_model_id: str = "eleven_v3"
    default_voice_id: str = "FzF9ACIefsb6wbrYVjf1"
    default_stability: float = 0.5

    # 服务
    host: str = "127.0.0.1"
    port: int = 8000


settings = Settings()
