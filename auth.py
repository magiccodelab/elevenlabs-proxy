"""ElevenLabs / Firebase 凭证管理。

凭证来源（按优先级）：
  1. .auth.json 中已存在的 refresh_token  →  调 securetoken 续期
  2. .env 中 ELEVENLABS_EMAIL + ELEVENLABS_PASSWORD  →  signInWithPassword
  3. .env 中 ELEVENLABS_BEARER_TOKEN  →  静态使用（不续期）

API key 优先用 .env 中 FIREBASE_API_KEY；缺省时从 https://elevenlabs.io 主页自动发现。

所有出站请求强制走 SOCKS5 代理（curl_cffi impersonate Chrome）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path

from curl_cffi.requests import AsyncSession

from config import settings

log = logging.getLogger(__name__)

_API_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{35}")


class AuthError(RuntimeError):
    """认证 / 凭证相关错误。"""


def _decode_jwt_exp(token: str) -> int:
    """解 JWT exp（秒）。失败返回 0。"""
    try:
        payload_seg = token.split(".")[1]
        payload_seg += "=" * (-len(payload_seg) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload_seg))["exp"])
    except Exception:
        return 0


class TokenManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._id_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: float = 0.0
        self._api_key: str | None = settings.firebase_api_key
        self._source: str = "uninit"  # refresh / password / static
        self._state_path = Path(settings.token_state_path)

    # ------- 持久化 -------

    def _persist(self) -> None:
        try:
            self._state_path.write_text(
                json.dumps(
                    {
                        "api_key": self._api_key,
                        "id_token": self._id_token,
                        "refresh_token": self._refresh_token,
                        "expires_at": self._expires_at,
                        "source": self._source,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("持久化 %s 失败: %s", self._state_path, e)

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            d = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("读取 %s 失败: %s", self._state_path, e)
            return
        self._api_key = self._api_key or d.get("api_key")
        self._refresh_token = d.get("refresh_token")
        self._id_token = d.get("id_token")
        self._expires_at = float(d.get("expires_at") or 0)
        self._source = d.get("source") or "uninit"

    # ------- 出站 helper -------

    def _new_session(self) -> AsyncSession:
        return AsyncSession(
            impersonate=settings.impersonate,
            proxy=settings.proxy_url,
            timeout=30,
        )

    # ------- API key 自动发现 -------

    async def _discover_api_key(self) -> str:
        async with self._new_session() as s:
            r = await s.get(settings.elevenlabs_origin, timeout=20)
            r.raise_for_status()
            html = r.text
            m = _API_KEY_RE.search(html)
            if m:
                return m.group(0)
            # 主页没找到则扫描内联 script 链接
            for src in re.findall(r'<script[^>]+src="([^"]+)"', html)[:30]:
                url = src if src.startswith("http") else settings.elevenlabs_origin + src
                try:
                    rr = await s.get(url, timeout=20)
                except Exception:
                    continue
                if rr.status_code != 200:
                    continue
                m = _API_KEY_RE.search(rr.text)
                if m:
                    return m.group(0)
        raise AuthError(
            "无法自动发现 Firebase API key，请在 .env 中显式配置 FIREBASE_API_KEY"
        )

    # ------- 公共 API -------

    async def initialize(self) -> dict:
        """启动时调用：恢复状态 + 必要时续期/登录。返回当前状态摘要。"""
        self._load()
        if not self._api_key:
            log.info("[auth] 自动发现 Firebase API key …")
            self._api_key = await self._discover_api_key()
            log.info("[auth] api_key=%s…%s", self._api_key[:6], self._api_key[-4:])
        # 强制 obtain
        await self.get_id_token(force=True)
        return self.status()

    async def get_id_token(self, *, force: bool = False) -> str:
        async with self._lock:
            now = time.time()
            if (
                not force
                and self._id_token
                and (self._expires_at - now) > settings.refresh_safety_seconds
            ):
                return self._id_token

            # 1) refresh_token 续期
            if self._refresh_token:
                try:
                    await self._refresh()
                    self._persist()
                    return self._id_token  # type: ignore[return-value]
                except Exception as e:  # noqa: BLE001
                    log.warning("[auth] refresh 失败，将尝试重新登录: %s", e)
                    self._refresh_token = None

            # 2) email/password 登录
            if settings.elevenlabs_email and settings.elevenlabs_password:
                await self._signin_password()
                self._persist()
                return self._id_token  # type: ignore[return-value]

            # 3) 静态 token 兜底
            if settings.elevenlabs_bearer_token:
                self._id_token = settings.elevenlabs_bearer_token
                self._expires_at = (
                    _decode_jwt_exp(self._id_token) or (now + 3600)
                )
                self._source = "static"
                self._persist()
                return self._id_token

            raise AuthError(
                "无可用凭证：请配置 ELEVENLABS_EMAIL + ELEVENLABS_PASSWORD"
                "（推荐，可自动续期），或 ELEVENLABS_BEARER_TOKEN（兜底）"
            )

    def status(self) -> dict:
        now = time.time()
        return {
            "source": self._source,
            "has_id_token": bool(self._id_token),
            "has_refresh_token": bool(self._refresh_token),
            "expires_at": int(self._expires_at),
            "expires_in_seconds": max(0, int(self._expires_at - now)),
            "api_key_loaded": bool(self._api_key),
        }

    # ------- 私有：调 Google 接口 -------

    async def _refresh(self) -> None:
        url = f"https://securetoken.googleapis.com/v1/token?key={self._api_key}"
        async with self._new_session() as s:
            r = await s.post(
                url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": settings.elevenlabs_origin,
                    "referer": settings.elevenlabs_origin + "/",
                    "x-client-version": "Chrome/JsCore/10.14.1/FirebaseCore-web",
                },
                timeout=20,
            )
        if r.status_code != 200:
            raise AuthError(f"securetoken refresh {r.status_code}: {r.text[:300]}")
        j = r.json()
        self._id_token = j["id_token"]
        self._refresh_token = j["refresh_token"]
        self._expires_at = time.time() + int(j.get("expires_in", "3600"))
        self._source = "refresh"
        log.info(
            "[auth] refreshed via securetoken, expires_in=%ss",
            int(j.get("expires_in", 3600)),
        )

    async def _signin_password(self) -> None:
        url = (
            "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
            f"?key={self._api_key}"
        )
        async with self._new_session() as s:
            r = await s.post(
                url,
                json={
                    "email": settings.elevenlabs_email,
                    "password": settings.elevenlabs_password,
                    "returnSecureToken": True,
                    "clientType": "CLIENT_TYPE_WEB",
                },
                headers={
                    "content-type": "application/json",
                    "origin": settings.elevenlabs_origin,
                    "referer": settings.elevenlabs_origin + "/",
                    "x-client-version": "Chrome/JsCore/10.14.1/FirebaseCore-web",
                },
                timeout=25,
            )
        if r.status_code != 200:
            raise AuthError(f"signInWithPassword {r.status_code}: {r.text[:300]}")
        j = r.json()
        self._id_token = j["idToken"]
        self._refresh_token = j["refreshToken"]
        self._expires_at = time.time() + int(j.get("expiresIn", "3600"))
        self._source = "password"
        log.info(
            "[auth] signed in as %s, expires_in=%ss",
            j.get("email"),
            int(j.get("expiresIn", 3600)),
        )


token_manager = TokenManager()


async def background_refresher(stop_event: asyncio.Event) -> None:
    """后台任务：周期性主动续期，避免在请求路径上踩到过期。"""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
            return  # 收到停止信号
        except asyncio.TimeoutError:
            pass
        try:
            await token_manager.get_id_token()
        except Exception as e:  # noqa: BLE001
            log.warning("[auth] background refresh error: %s", e)
