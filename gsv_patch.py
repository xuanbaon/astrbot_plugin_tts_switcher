"""把 GSV 的权重切换从「启动时」挪到「合成前」。

核心 4.29.x 的 ``ProviderGSVTTS.initialize()`` 会立刻 ``set_gpt_weights`` +
``set_sovits_weights``。每个「音色」都是一个独立 provider，所以 30 多个音色
就是启动时串行加载 30 多次；GPT-SoVITS 一旦没开，每个还要重试 3 次 × 2 组，
启动能被拖到 5 分钟。

但 GPT-SoVITS 服务端本来就懂「同一组权重直接跳过」（``set_gpt_weights`` 里
比对当前路径后返回 ``skipped``），所以正确做法是：启动什么都不做，等真要
合成时再 ensure 一次。

两个代价都很小：
- 同一个音色连续合成：服务端直接 skipped，只多两个本地 HTTP 往返（毫秒级）；
- 换音色：真要加载一次权重（实测 148MB 的 .ckpt 冷读约 0.14s，含显存分配
  与 empty_cache 约 0.6~1.2s）。

同一端口的 set 与 tts 用一把锁串起来，避免 A 音色 set 完还没合成，B 音色
又把权重换掉，结果 A 念出来的是 B 的声音。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger

# 权重切换失败后，这段时间内不再重试，免得服务没起时每次合成都白等一轮。
FAIL_COOLDOWN = 20.0

_PATCHED = False


def _lock_for(provider: Any) -> asyncio.Lock:
    """按 api_base 共用一把锁：同一个 GPT-SoVITS 实例是全局权重状态。"""
    cls = type(provider)
    locks = getattr(cls, "_gsv_locks", None)
    if not isinstance(locks, dict):
        locks = {}
        cls._gsv_locks = locks
    base = str(getattr(provider, "api_base", ""))
    lock = locks.get(base)
    if lock is None:
        lock = asyncio.Lock()
        locks[base] = lock
    return lock


def _cooldown_map(provider: Any) -> dict[str, float]:
    cls = type(provider)
    table = getattr(cls, "_gsv_cooldown", None)
    if not isinstance(table, dict):
        table = {}
        cls._gsv_cooldown = table
    return table


async def _apply_weights(provider: Any) -> None:
    """把本 provider 的权重刷到服务端；同权重时服务端会自己跳过。

    用 ``retries=1``：连不上就没必要按核心的 3 次重试慢慢等，外层有冷却兜底。
    """
    request = getattr(provider, "_make_request", None)
    if not callable(request):
        return
    base = str(getattr(provider, "api_base", "")).rstrip("/")
    gpt = str(getattr(provider, "gpt_weights_path", "") or "")
    sovits = str(getattr(provider, "sovits_weights_path", "") or "")
    if gpt:
        await request(f"{base}/set_gpt_weights", {"weights_path": gpt}, retries=1)
    if sovits:
        await request(f"{base}/set_sovits_weights", {"weights_path": sovits}, retries=1)


def install() -> bool:
    """给核心的 GSV provider 打补丁，重复调用无副作用。"""
    global _PATCHED
    if _PATCHED:
        return True

    try:
        from astrbot.core.provider.sources.gsv_selfhosted_source import (
            ProviderGSVTTS as cls,
        )
    except Exception as exc:  # 核心改路径/换实现时安静降级，别拖垮插件
        logger.debug(f"[tts_switcher] 没找到 GSV provider，跳过权重补丁：{exc}")
        return False

    if getattr(cls, "_gsv_lazy_weights", False):
        _PATCHED = True
        return True

    original_get_audio = cls.get_audio

    async def initialize(self) -> None:  # noqa: ANN001 - 与核心签名一致
        """只建会话，不加载权重。"""
        import aiohttp

        self._session = aiohttp.ClientSession(
            headers=self.request_headers,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        )
        logger.info("[GSV TTS] 初始化完成（权重留到合成时按需加载）")

    async def get_audio(self, text: str) -> str:  # noqa: ANN001
        """合成前确保权重是本音色的，再交给原实现。"""
        if not str(text).strip():
            raise ValueError("[GSV TTS] TTS 文本不能为空")

        base = str(getattr(self, "api_base", ""))
        cooldown = _cooldown_map(self)
        async with _lock_for(self):
            if time.monotonic() >= cooldown.get(base, 0.0):
                try:
                    await _apply_weights(self)
                    cooldown.pop(base, None)
                except Exception as exc:
                    cooldown[base] = time.monotonic() + FAIL_COOLDOWN
                    logger.warning(
                        f"[GSV TTS] 切换权重失败（{base}），"
                        f"{FAIL_COOLDOWN:.0f} 秒内不再尝试：{exc}",
                    )
            return await original_get_audio(self, text)

    cls.initialize = initialize
    cls.get_audio = get_audio
    cls._gsv_lazy_weights = True
    _PATCHED = True
    logger.info(
        "[tts_switcher] 已接管 GSV 权重加载：启动不再串行 set，换音色时自动切换",
    )
    return True


def is_installed() -> bool:
    return _PATCHED
