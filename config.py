"""配置加载（pydantic-settings）。所有敏感项走 .env。"""
from pydantic import Field
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
    elevenlabs_bearer_token: str = Field(..., min_length=20)

    # 代理（强制走 SOCKS5；启动期会校验出口 IP）
    proxy_url: str = "socks5://127.0.0.1:7899"
    expected_egress_ip: str = "38.15.30.162"

    # 浏览器指纹（curl_cffi impersonate）
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
