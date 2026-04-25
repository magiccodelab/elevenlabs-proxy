"""FastAPI 入口。

启动期硬性校验：
1) SOCKS5 代理可用
2) 通过代理出口 IP == EXPECTED_EGRESS_IP
3) ElevenLabs Bearer token 有效

任一失败即拒绝启动，避免直连泄露真实 IP。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import upstream
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("elevenlabs-proxy")

# MVP：内存中保留最近一次合成的音频，按 audio_id 索引
AUDIO_CACHE: dict[str, bytes] = {}
AUDIO_CACHE_MAX = 32


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("[boot] 校验代理 %s ...", settings.proxy_url)
    try:
        ip = await upstream.fetch_egress_ip()
    except Exception as e:
        raise RuntimeError(f"代理不可用 ({settings.proxy_url}): {e}") from e
    if ip != settings.expected_egress_ip:
        raise RuntimeError(
            f"代理出口 IP 不匹配：实测 {ip}，要求 {settings.expected_egress_ip}"
        )
    log.info("[boot] 代理 OK，出口 IP=%s", ip)

    log.info("[boot] 校验 ElevenLabs token ...")
    info = await upstream.fetch_auth_account()
    log.info("[boot] token OK，账号=%s (%s)", info.get("email"), info.get("auth_account_id"))

    app.state.egress_ip = ip
    app.state.account_email = info.get("email")
    yield
    log.info("[shutdown]")


app = FastAPI(title="elevenlabs-proxy", version="0.1.0", lifespan=lifespan)


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    voice_id: str | None = None
    model_id: str | None = None
    stability: float | None = Field(default=None, ge=0.0, le=1.0)


def _evt(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _put_audio(data: bytes) -> str:
    aid = uuid.uuid4().hex
    if len(AUDIO_CACHE) >= AUDIO_CACHE_MAX:
        # FIFO 简单淘汰
        for k in list(AUDIO_CACHE)[: len(AUDIO_CACHE) - AUDIO_CACHE_MAX + 1]:
            AUDIO_CACHE.pop(k, None)
    AUDIO_CACHE[aid] = data
    return aid


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "egress_required": settings.expected_egress_ip,
        "egress_actual": getattr(app.state, "egress_ip", None),
        "account": getattr(app.state, "account_email", None),
        "impersonate": settings.impersonate,
    }


@app.post("/api/tts")
async def tts(req: TTSRequest):
    voice_id = req.voice_id or settings.default_voice_id
    model_id = req.model_id or settings.default_model_id
    stability = req.stability if req.stability is not None else settings.default_stability

    async def gen() -> AsyncIterator[bytes]:
        t0 = time.perf_counter()
        yield _evt({"type": "log", "level": "info",
                    "msg": f"接收文本 ({len(req.text)} 字符) voice={voice_id} model={model_id} stability={stability}"})
        yield _evt({"type": "log", "level": "info",
                    "msg": f"经 {settings.proxy_url} → ElevenLabs (impersonate={settings.impersonate})"})

        buf = bytearray()
        first_byte_at: float | None = None
        try:
            async for chunk in upstream.text_to_dialogue_stream(
                text=req.text, voice_id=voice_id, model_id=model_id, stability=stability,
            ):
                if first_byte_at is None:
                    first_byte_at = time.perf_counter()
                    yield _evt({"type": "log", "level": "ok",
                                "msg": f"首字节到达 TTFB={int((first_byte_at - t0) * 1000)}ms"})
                buf.extend(chunk)
                yield _evt({"type": "progress", "received": len(buf), "chunk": len(chunk)})
        except Exception as e:
            log.exception("upstream error")
            yield _evt({"type": "log", "level": "error", "msg": f"上游失败: {e}"})
            yield _evt({"type": "error", "msg": str(e)})
            return

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        aid = _put_audio(bytes(buf))
        yield _evt({"type": "log", "level": "ok",
                    "msg": f"合成完成 size={len(buf)} bytes elapsed={elapsed_ms}ms"})
        yield _evt({"type": "done", "audio_id": aid, "size": len(buf),
                    "elapsed_ms": elapsed_ms, "url": f"/api/audio/{aid}"})

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/api/audio/{audio_id}")
async def get_audio(audio_id: str):
    data = AUDIO_CACHE.get(audio_id)
    if not data:
        raise HTTPException(404, "audio not found or expired")
    return Response(
        content=data,
        media_type="audio/mpeg",
        headers={"content-disposition": f'inline; filename="{audio_id}.mp3"'},
    )


# 前端
app.mount("/", StaticFiles(directory="static", html=True), name="static")
