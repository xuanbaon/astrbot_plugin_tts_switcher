# -*- coding: utf-8 -*-
"""TTS 声线切换 —— 在聊天里一句话换掉当前会话使用的 TTS 提供商。

AstrBot 的"会话级 TTS"本来只能在面板里逐个会话点选，日常在群里想换个声音
很不方便。这个插件把「名字 → TTS 提供商」的映射放到插件配置页里，之后在
聊天里发 /tts 示例 就切过去了，改动即时生效，不用重启核心。

另外提供两条一次性命令（指定声线一律用等号写法）：
- /说=<声线> <文本>：拿指定声线念一句指定的话，念完就完了，不改当前会话的声线设置；
- /答=<声线> <文本>：让机器人用指定声线"回复"这句话（走正常的大模型回复），
  回复发出去之后自动切回原来 /tts 选的那个声线。

/说、/答 后面不加等号时不做声线解析，整段都当正文（否则「/说 罕见 骂谁罕见」
会被拆成声线「罕见」+ 正文「骂谁罕见」，跟「/说 <文本>」直接打架）。

设计上的几个取舍：
- /tts 只动会话级偏好（provider_perf_text_to_speech）和会话级 TTS 开关，不碰全局配置，
  出错影响面最小；
- /说 直接调 provider.get_audio() 合成再发语音，完全不碰会话偏好，天然"一次性"，
  中途失败也不会把设置改坏；
- /答 完全不动会话偏好：只记下"这一轮用哪个声线"，等大模型把回复写好之后再在
  on_decorating_result 钩子里把文字换成语音。这样不需要事后还原，中途失败也不会
  把 /tts 的设置改坏，也不用担心别人的消息插进来被念成临时声线；
- 名字可以配别名，匹配顺序是「精确 → 包含 → 反向包含」，输入多打几个字也能命中；
- /tts 权限默认只给管理员；/说 默认所有人可用（念句话不至于把声音改掉），
  靠冷却和长度上限防刷屏。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain, Record

try:  # 合并转发用；老版本核心可能没有这两个组件
    from astrbot.api.message_components import Node, Nodes
except Exception:  # pragma: no cover
    Node = None  # type: ignore[assignment]
    Nodes = None  # type: ignore[assignment]
from astrbot.api.provider import ProviderType
from astrbot.api.star import Context, Star

try:  # 让最后一个参数吃掉剩余文本，"示例 语音" 这种带空格的写法也能用
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # pragma: no cover

    class GreedyStr(str):  # type: ignore[no-redef]
        pass

try:
    from astrbot.core import sp
except Exception:  # pragma: no cover
    sp = None  # type: ignore[assignment]

try:  # 核心自带的长文本切分，优先用它，切分策略跟官方 TTS 一致
    from astrbot.core.pipeline.result_decorate.stage import (
        split_text_for_tts as _core_split_text,
    )
except Exception:  # pragma: no cover
    _core_split_text = None  # type: ignore[assignment]


PLUGIN_NAME = "astrbot_plugin_tts_switcher"
TTS_TYPE = ProviderType.TEXT_TO_SPEECH
TTS_PREF_KEY = f"provider_perf_{TTS_TYPE.value}"
SESSION_CFG_KEY = "session_service_config"

# 单条回复的字符上限，超了就截断，避免刷屏
MAX_REPLY = 1500

# /tts 列表 超过这个字数就切成多段、用合并转发发出去（可在配置里改）
LIST_SINGLE_MAX = 600
# 合并转发的每个节点放多少条声线（可在配置里改）
LIST_FORWARD_ROWS = 40
# 只有这些平台的事件发送器实现了合并转发
FORWARD_PLATFORMS = ("aiocqhttp",)

# /说 合成失败时的重试次数
SAY_MAX_ATTEMPT = 2

STATUS_WORDS = {"", "状态", "当前", "status", "now", "查看", "查"}
LIST_WORDS = {"列表", "list", "ls", "全部", "所有", "声线"}
HELP_WORDS = {"帮助", "help", "help me", "?", "？", "说明", "用法", "usage"}
ENGINE_WORDS = {"引擎", "提供商", "provider", "providers", "后端", "provider列表"}
RESET_WORDS = {"默认", "default", "reset", "重置", "恢复", "清除"}
ON_WORDS = {"开", "开启", "on", "打开", "启用"}
OFF_WORDS = {"关", "关闭", "off", "停用", "禁用"}

DEFAULT_HELP = """TTS 声线切换
/tts            看当前用的是哪个
/tts 列表       列出所有可用声线（带备注；太长时以合并转发发出）
/tts <名字>     切换声线，例如 /tts 示例
/tts 默认       清掉本会话设置，回到全局默认
/tts 关闭       本会话不再合成语音
/tts 开启       恢复本会话语音
/tts 引擎       列出所有 TTS 提供商 id（配置映射时用）
/说=<声线> <文本> 用某个声线念一句（一次性，不改当前设置）
/答=<声线> <文本> 让机器人用某个声线回复（一次性，回完自动切回）
/ttshelp        看完整用法"""

DEFAULT_FULL_HELP = """TTS 声线指令说明
━━━━━━━━━━━━━━━
【切换声线】
/tts                  看当前用的是哪个声线
/tts 列表             列出所有可用声线（含备注，太长会自动用合并转发发）
/tts <名字>           切换本会话声线，例如 /tts 示例
/tts 默认             清掉本会话设置，回到全局默认
/tts 开启、/tts 关闭  开关本会话的语音合成
/tts 引擎             列出所有 TTS 提供商 id

【念一句指定的话】
/说 <文本>            用当前声线念，例如 /说 今天风很好
/说=<声线> <文本>     用指定声线念，例如 /说=示例 今天风很好
念完就结束，本会话的声线不会被改掉。

⚠ 加等号才认声线名：/说 示例 你好 会把「示例 你好」整段念出来；
  要指定声线请写 /说=示例 你好。

【声线名怎么找】
先找完全对应的（名字或别名一模一样）；
没有就模糊搜：只命中一个就用它，命中的是同一个名字的不同档
（示例 flash / 示例 plus）就取列表里靠前的那个，不用写全名；
命中好几个不同名字会问你说哪个；一个都没命中才回「没找到声线」。

【用指定声线回复】
/答=<声线> <文本>     让机器人用那个声线回复这句话
                      例如 /答=示例 今天天气怎么样
/答 <文本>            用当前声线回复（相当于强制让它接你这句话）
回复发出去之后自动切回原来 /tts 选的声线，后面的对话不受影响。

【看这份说明】
/ttshelp 或 /tts 帮助

别名：/声线 = /tts，/say /念 /读 = /说，/回 /reply = /答"""

DEFAULT_SAY_HELP = """用声线念一句话
/说 <文本>             用当前声线念，例如：/说 今天风很好
/说=<声线> <文本>      用指定声线念，例如：/说=示例 今天风很好
念完就结束，当前会话的声线不会被改掉，下次回复还是 /tts 选的那个。

注意：等号不能省。/说 示例 你好 会把「示例 你好」整段念出来。
声线名先找完全对应的；没有就模糊搜，同名的不同档取列表里靠前的那个，
几个不同名字都对得上会让你写全，一个都对不上才会提示没找到。"""

DEFAULT_ANSWER_HELP = """用指定声线回复
/答=<声线> <文本>      例如：/答=示例 今天天气怎么样
/答 <文本>             用当前声线回复
机器人会像平时一样接这句话，只是这一条回复用你指定的声线念出来；
回复发完就自动切回原来 /tts 选的声线，后面的对话不受影响。

注意：等号不能省，/答 示例 你好 会按当前声线回复「示例 你好」。
声线名先找完全对应的；没有就模糊搜，同名的不同档取列表里靠前的那个，
几个不同名字都对得上会让你写全，一个都对不上才会提示没找到。"""

# 内置 /help 补丁的版本号；改包装逻辑时加一，热重载后会把旧补丁换掉
HELP_PATCH_VERSION = 5

# /help 输出末尾追加的摘要，由 HelpCommand.help 的包装器调用
HELP_INJECT_LINES = (
    "TTS 声线（插件「TTS 声线切换」）",
    "/ttshelp        看完整用法",
    "/tts <名字>     切换本会话声线，例如 /tts 示例",
    "/说=<声线> <文本> 用某个声线念一句（一次性）",
    "/答=<声线> <文本> 用某个声线回复一句（一次性，回完自动切回）",
)

# 声线名与正文之间认的分隔符：空白（含全角空格）、半角逗号 ,、全角逗号 ，、
# 冒号（半角 : 全角 ：）、顿号、竖线、等号（半角 = 全角 ＝）
VOICE_SEPS = " \t\u3000,，:：、|｜=＝"

# 声线名的档位后缀：模糊命中多个时，同基名不同档的（示例A flash / 示例A plus）
# 自动取配置里的第一个，算「同一个声线的不同档」，不算歧义
VOICE_TIER_WORDS = ("flash", "plus", "gsv", "v4", "v2", "pro")

# 等号紧跟在命令后面的写法：/说=声线 句子、/答=声线 句子。
# CommandFilter 只认「说 」（命令名后必须跟空格），所以这类写法单独用正则接。
SAY_EQ_RE = re.compile(
    r"^\s*/?\s*(?:说|念|读|say|speak)\s*[=＝]\s*(?P<rest>.+)$", re.S | re.I
)
ANSWER_EQ_RE = re.compile(
    r"^\s*/?\s*(?:答|回|reply)\s*[=＝]\s*(?P<rest>.+)$", re.S | re.I
)

# 自然语言入口（默认关闭）：用示例的声音说 今天风很好
NL_SAY_RE = re.compile(
    r"^(?:请|帮我)?用\s*(?P<voice>.{1,16}?)\s*的(?:声线|声音|嗓音|音色)"
    r"(?:来)?(?:说|念|读|讲|回复|复述)[:：,，]?\s*(?P<text>.{1,})$",
    re.S,
)



# ---------------------------------------------------------------- 小工具


def _split_names(text: Any) -> list[str]:
    if not text:
        return []
    return [s for s in re.split(r"[,，、;；|/\s]+", str(text)) if s]


def _regex_rest(pattern: re.Pattern, event: Any) -> str:
    """从事件原文里取出正则的 rest 组；取不到就返回空串。"""
    text = ""
    getter = getattr(event, "get_message_str", None)
    if callable(getter):
        try:
            text = str(getter() or "")
        except Exception:
            text = ""
    if not text:
        text = str(getattr(event, "message_str", "") or "")
    match = pattern.match(text.strip())
    if not match:
        return ""
    return str(match.group("rest") or "").strip()


def _clip(text: str, limit: int = MAX_REPLY) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n……（内容过长，已截断）"


def _split_text(text: str, limit: int) -> list[str]:
    """把长文本切成每块不超过 limit 字的若干段，优先复用核心实现。"""
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return [text] if text else []
    if _core_split_text is not None:
        try:
            chunks = _core_split_text(text, limit)
            if chunks:
                return chunks
        except Exception:
            pass
    chunks: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？!?…；;\n" and len(buf) >= limit * 0.6:
            chunks.append(buf.strip())
            buf = ""
        elif len(buf) >= limit:
            chunks.append(buf.strip())
            buf = ""
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if c]


def _help_command_class() -> Any:
    """拿到 AstrBot 内置 /help 的实现类；核心结构变了就返回 None。"""
    try:
        from astrbot.builtin_stars.builtin_commands.commands.help import (
            HelpCommand,
        )
    except Exception:
        return None
    return HelpCommand


def _provider_id(provider: Any) -> str:
    """从 provider 实例上把 id 抠出来，兼容几种不同的存放位置。"""
    if provider is None:
        return ""
    config = getattr(provider, "provider_config", None) or getattr(
        provider, "config", None
    )
    if isinstance(config, dict):
        for key in ("id", "provider_id"):
            value = config.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for attr in ("provider_id", "id"):
        value = getattr(provider, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    meta = getattr(provider, "meta", None)
    if callable(meta):
        try:
            meta_obj = meta()
        except Exception:
            meta_obj = None
        for attr in ("id", "name"):
            value = getattr(meta_obj, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _provider_name(provider: Any) -> str:
    if provider is None:
        return ""
    meta = getattr(provider, "meta", None)
    if callable(meta):
        try:
            meta_obj = meta()
        except Exception:
            meta_obj = None
        for attr in ("name", "id", "model"):
            value = getattr(meta_obj, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for attr in ("provider_name", "name"):
        value = getattr(provider, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# ---------------------------------------------------------------- 插件主体


class TTSSwitcher(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context, config)
        self.config = config if isinstance(config, dict) else {}
        # /说 的冷却记录：key -> 上次成功发声的时间戳
        self._say_last: dict[str, float] = {}
        # /答 的冷却记录
        self._answer_last: dict[str, float] = {}
        # /答 待转语音的那一轮：umo -> {"provider_id", "label", "expires"}
        self._pending_answer: dict[str, dict[str, Any]] = {}
        # 让 /help 能带上本插件的指令。实例挂在核心的 HelpCommand 类上而不是
        # 模块全局，插件热重载换新实例后，之前打过的补丁也能读到最新的配置。
        help_cls = _help_command_class()
        if help_cls is not None:
            help_cls._tts_switcher_provider = self
        self._install_help_patch()
        # WebUI 面板（pages/voice_console）：读改声线备注、指令设置、试听
        try:
            from .wepanel import auto_sync, register_web_apis

            register_web_apis(self)
            # 按「模型供应商」里已有的文字转语音提供商，把还没建声线的补上
            auto_sync(self)
        except Exception as exc:  # 面板挂了不影响聊天里的指令
            logger.warning(f"[tts_switcher] 初始化 WebUI 面板失败: {exc}")

    async def terminate(self) -> None:
        help_cls = _help_command_class()
        if help_cls is not None and getattr(
            help_cls, "_tts_switcher_provider", None
        ) is self:
            help_cls._tts_switcher_provider = None

    # ------------------------------------------------------------ 配置

    def _cfg(self, path: str, default: Any = None) -> Any:
        node: Any = self.config
        for key in path.split("."):
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return default
        return default if node is None else node

    def _flag(self, path: str, default: bool) -> bool:
        value = self._cfg(path, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on", "开", "是"}

    def _msg(self, key: str, default: str) -> str:
        text = self._cfg(f"texts.{key}", "")
        return str(text).strip() or default

    # ------------------------------------------------------------ /help 集成

    def _install_help_patch(self) -> None:
        """把本插件的指令摘要挂到内置 /help 的末尾。

        AstrBot 自带的 /help 只列 reserved（内置）指令，第三方插件的指令不会出现。
        这里包一层 HelpCommand.help，在它写完自己的内容后再往结果链里追加一段；
        只改内存里的方法，不动核心文件，插件卸载后自动失效。
        """
        if not self._flag("help.inject_into_help", True):
            return
        HelpCommand = _help_command_class()
        if HelpCommand is None:  # 核心结构变了就安静跳过，别影响插件本体
            logger.debug("[tts_switcher] 找不到内置 help 指令，跳过注入")
            return
        if getattr(HelpCommand, "_tts_switcher_patch_version", 0) >= HELP_PATCH_VERSION:
            return

        # 升级过补丁时不要把上一次的包装再包一层，永远包最原始的方法
        original = getattr(HelpCommand, "_tts_switcher_original_help", None)
        if original is None:
            original = HelpCommand.help
            HelpCommand._tts_switcher_original_help = original

        async def patched(inner_self, event):
            await original(inner_self, event)
            try:
                provider = getattr(HelpCommand, "_tts_switcher_provider", None)
                extra = provider._help_inject_text() if provider is not None else ""
                if not extra:
                    return
                result = event.get_result()
                if result is None or not getattr(result, "chain", None):
                    return
                result.chain.append(Plain("\n\n" + extra))
            except Exception as exc:
                logger.debug(f"[tts_switcher] 往 /help 追加内容失败: {exc}")

        HelpCommand.help = patched
        HelpCommand._tts_switcher_patched = True
        HelpCommand._tts_switcher_patch_version = HELP_PATCH_VERSION
        logger.info("[tts_switcher] 已把 TTS 指令摘要挂到 /help 末尾")

    def _help_inject_text(self) -> str:
        if not self._flag("enabled", True):
            return ""
        custom = str(self._cfg("help.inject_text", "") or "").strip()
        if custom:
            return custom
        lines = list(HELP_INJECT_LINES)
        if not self._flag("answer.enable", True):
            lines = [line for line in lines if not line.startswith("/答")]
        return "\n".join(lines)

    def _voices(self) -> list[dict[str, Any]]:
        raw = self._cfg("voices", [])
        if not isinstance(raw, list):
            return []
        voices: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            if not item.get("enabled", True):
                continue
            name = str(item.get("name") or "").strip()
            provider_id = str(item.get("provider_id") or "").strip()
            if not name or not provider_id:
                continue
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            voices.append(
                {
                    "name": name,
                    "provider_id": provider_id,
                    "aliases": _split_names(item.get("alias")),
                    "note": str(item.get("note") or "").strip(),
                }
            )
        return voices

    # ------------------------------------------------------------ provider

    def _providers(self) -> list[Any]:
        ctx = self.context
        getter = getattr(ctx, "get_all_tts_providers", None)
        if callable(getter):
            try:
                providers = list(getter() or [])
                if providers:
                    return providers
            except Exception:
                pass
        manager = getattr(ctx, "provider_manager", None)
        return list(getattr(manager, "tts_provider_insts", None) or [])

    def _resolve_provider(self, wanted: str) -> Any:
        """把配置里写的 id（或显示名）解析成真正在跑的 provider 实例。"""
        wanted_l = wanted.strip().lower()
        providers = self._providers()
        for provider in providers:
            if _provider_id(provider).lower() == wanted_l:
                return provider
        for provider in providers:
            if _provider_name(provider).lower() == wanted_l:
                return provider
        return None

    async def _current_instance(self, umo: str) -> Any:
        """当前会话真正生效的 TTS 实例（优先用官方的 get_using_tts_provider_async）。"""
        ctx = self.context
        getter = getattr(ctx, "get_using_tts_provider_async", None)
        if callable(getter):
            try:
                instance = await getter(umo or None)
                if instance is not None:
                    return instance
            except Exception as exc:
                logger.debug(f"[tts_switcher] get_using_tts_provider_async 失败: {exc}")
        manager = getattr(ctx, "provider_manager", None)
        if manager is not None:
            try:
                return await manager.get_using_provider_async(TTS_TYPE, umo or None)
            except Exception as exc:
                logger.debug(f"[tts_switcher] 解析当前 TTS 提供商失败: {exc}")
        return None

    async def _current(self, umo: str) -> tuple[str, Any]:
        """返回 (会话里显式设置的 provider id 或空串, 当前生效的 provider 实例)。"""
        preference = ""
        if sp is not None and umo:
            try:
                value = await sp.session_get(umo, TTS_PREF_KEY, None)
                if isinstance(value, str):
                    preference = value.strip()
            except Exception as exc:
                logger.debug(f"[tts_switcher] 读取会话 TTS 偏好失败: {exc}")
        instance = await self._current_instance(umo)
        return preference, instance

    async def _tts_enabled(self, umo: str) -> bool:
        if sp is None or not umo:
            return True
        try:
            config = await sp.session_get(umo, SESSION_CFG_KEY, {}) or {}
        except Exception:
            return True
        if not isinstance(config, dict):
            return True
        return bool(config.get("tts_enabled", True))

    # ------------------------------------------------------------ 权限

    def _can_use(self, event: AstrMessageEvent) -> bool:
        if self._flag("access.allow_all", False):
            return True
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        admins = _split_names(self._cfg("access.extra_admins", ""))
        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            sender = ""
        return bool(sender) and sender in admins

    def _scope_ok(self, event: AstrMessageEvent) -> bool:
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:
            group_id = ""
        if group_id:
            return self._flag("access.allow_group", True)
        return self._flag("access.allow_private", True)

    # ------------------------------------------------------------ 匹配

    @staticmethod
    def _match(text: str, voices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        needle = text.strip().lower()
        if not needle:
            return []

        exact = [
            voice
            for voice in voices
            if voice["name"].lower() == needle
            or any(alias.lower() == needle for alias in voice["aliases"])
        ]
        if exact:
            return exact

        partial = [
            voice
            for voice in voices
            if needle in voice["name"].lower()
            or any(needle in alias.lower() for alias in voice["aliases"])
        ]
        if partial:
            return partial

        # 用户多打了字，例如配置叫「示例」而输入「示例语音」
        reverse = [
            voice
            for voice in voices
            if voice["name"].lower() in needle
            or any(alias.lower() in needle for alias in voice["aliases"])
        ]
        return reverse

    # ------------------------------------------------------------ 渲染

    def _status_text(
        self,
        preference: str,
        instance: Any,
        enabled: bool,
        voices: list[dict[str, Any]],
    ) -> str:
        provider_id = _provider_id(instance) or preference or "（未解析到）"
        provider_disp = _provider_name(instance)
        if provider_disp and provider_disp != provider_id:
            engine = f"{provider_id}（{provider_disp}）"
        else:
            engine = provider_id

        source = "本会话指定" if preference else "全局默认"
        match = next(
            (v for v in voices if v["provider_id"].lower() == provider_id.lower()),
            None,
        )
        lines = [
            "TTS 声线",
            f"当前引擎：{engine}",
            f"来源：{source}",
            f"本会话语音：{'开启' if enabled else '关闭'}",
        ]
        if match:
            name_line = f"映射名字：{match['name']}"
            if match["note"]:
                name_line += f"（{match['note']}）"
            lines.append(name_line)
        lines.append(f"可用声线 {len(voices)} 个，发 /tts 列表 查看")
        return "\n".join(lines)

    def _list_rows(
        self,
        voices: list[dict[str, Any]],
        current_id: str,
        show_id: bool,
    ) -> list[str]:
        rows: list[str] = []
        for voice in voices:
            mark = " ← 当前" if voice["provider_id"].lower() == current_id else ""
            row = f"· {voice['name']}"
            if voice["aliases"]:
                row += f"（{'/'.join(voice['aliases'])}）"
            if show_id:
                row += f" → {voice['provider_id']}"
            if voice["note"]:
                row += f"  — {voice['note']}"
            rows.append(row + mark)
        return rows

    def _list_text(
        self,
        voices: list[dict[str, Any]],
        preference: str,
        instance: Any,
    ) -> str:
        if not voices:
            return (
                "还没有配置任何声线。\n"
                "去 面板 → 插件 → TTS 声线切换 → 声线映射，加一条：名字填「示例」，"
                "提供商选本地的那个。"
            )
        current_id = (_provider_id(instance) or preference or "").lower()
        show_id = self._flag("list.show_provider_id", True)
        lines = [f"可用声线（{len(voices)}）"]
        lines.extend(self._list_rows(voices, current_id, show_id))
        lines.append("用法：/tts <名字>")
        return "\n".join(lines)

    def _list_single_max(self) -> int:
        """列表短于这个字数就一条消息发完，超过就改用合并转发。"""
        try:
            value = int(self._cfg("list.single_max", LIST_SINGLE_MAX) or 0)
        except (TypeError, ValueError):
            value = LIST_SINGLE_MAX
        if value <= 0:
            value = LIST_SINGLE_MAX
        return min(value, MAX_REPLY)

    def _list_chunks(
        self,
        voices: list[dict[str, Any]],
        preference: str,
        instance: Any,
    ) -> list[tuple[str, str]]:
        """把列表拆成 [(节点名, 正文)]，每个节点放 list.forward_rows 条声线。"""
        current_id = (_provider_id(instance) or preference or "").lower()
        show_id = self._flag("list.show_provider_id", True)
        rows = self._list_rows(voices, current_id, show_id)
        total = len(rows)
        try:
            per = int(self._cfg("list.forward_rows", LIST_FORWARD_ROWS) or 0)
        except (TypeError, ValueError):
            per = LIST_FORWARD_ROWS
        if per <= 0:
            per = LIST_FORWARD_ROWS
        chunks: list[tuple[str, str]] = []
        for start in range(0, total, per):
            part = rows[start : start + per]
            end = start + len(part)
            body = "\n".join(part)
            if end >= total:
                body += f"\n共 {total} 条，用法：/tts <名字>"
            chunks.append((f"声线 {start + 1}-{end}/{total}", body))
        return chunks

    async def _send_forward_chunks(
        self, event: AstrMessageEvent, chunks: list[tuple[str, str]]
    ) -> bool:
        """把若干段文本用合并转发一次发出去；平台不支持或发送失败返回 False。"""
        if not chunks or Nodes is None or Node is None:
            return False
        try:
            platform = str(getattr(event, "platform_meta", None).name or "")
        except Exception:
            platform = ""
        if platform and platform not in FORWARD_PLATFORMS:
            return False
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""
        try:
            nodes = Nodes(
                [
                    Node(uin=self_id, name=name, content=[Plain(text)])
                    for name, text in chunks
                ]
            )
            await event.send(MessageChain([nodes]))
            return True
        except Exception as exc:
            logger.warning(f"[tts_switcher] 合并转发发送失败，改发普通消息: {exc}")
            return False

    def _help_text(self) -> str:
        return self._msg("help", DEFAULT_HELP)

    def _full_help_text(self) -> str:
        return self._msg("help_all", DEFAULT_FULL_HELP)

    # ------------------------------------------------------------ 命令

    @filter.command("tts", alias={"TTS", "Tts", "声线"})
    async def tts_cmd(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """TTS 声线切换：/tts 列表、/tts 示例、/tts 默认、/tts 关闭"""
        if not self._flag("enabled", True):
            return

        if not self._scope_ok(event):
            yield event.plain_result(
                self._msg("reply_scope_denied", "这里不让换声线。")
            )
            return

        if not self._can_use(event):
            yield event.plain_result(
                self._msg("reply_denied", "只有管理员能换声线。")
            )
            return

        raw = re.sub(r"\s+", " ", str(args or "")).strip()
        keyword = raw.lower()
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        voices = self._voices()

        try:
            if keyword in HELP_WORDS:
                yield event.plain_result(self._help_text())
                return

            if keyword in LIST_WORDS:
                preference, instance = await self._current(umo)
                text = self._list_text(voices, preference, instance)
                # 短列表直接发；长的切成多段、合并转发，实在发不了再分几条普通消息
                if not voices or len(text) <= self._list_single_max():
                    yield event.plain_result(_clip(text))
                    return
                chunks = self._list_chunks(voices, preference, instance)
                if self._flag("list.forward", True) and await self._send_forward_chunks(
                    event, chunks
                ):
                    event.stop_event()
                    return
                for _, body in chunks:
                    await self._say_send(event, body)
                event.stop_event()
                return

            if keyword in ENGINE_WORDS:
                yield event.plain_result(_clip(self._engine_text(voices)))
                return

            if keyword in RESET_WORDS:
                yield event.plain_result(await self._reset(event, umo))
                return

            if keyword in ON_WORDS:
                yield event.plain_result(await self._toggle(event, umo, True))
                return

            if keyword in OFF_WORDS:
                yield event.plain_result(await self._toggle(event, umo, False))
                return

            if keyword in STATUS_WORDS:
                preference, instance = await self._current(umo)
                enabled = await self._tts_enabled(umo)
                yield event.plain_result(
                    _clip(self._status_text(preference, instance, enabled, voices))
                )
                return

            if not voices:
                yield event.plain_result(
                    "还没有配置声线映射。\n"
                    "去 面板 → 插件 → TTS 声线切换 → 声线映射 里加一条，"
                    "名字填「示例」，提供商选你要的那个。"
                )
                return

            # 完全对应优先；没有再模糊搜：同基名不同档取第一个，多个不同名字就问一句
            candidates = self._match(raw, voices)
            if not candidates:
                names = "、".join(v["name"] for v in voices[:12])
                yield event.plain_result(
                    self._msg(
                        "reply_unknown",
                        f"没找到声线「{raw}」。\n可用的有：{names}",
                    )
                )
                return
            target = self._pick_fuzzy(candidates, raw)
            if target is None:
                names = "、".join(v["name"] for v in candidates[:10])
                yield event.plain_result(
                    f"「{raw}」能对上好几个，你说的是哪个？\n{names}"
                )
                return
            if target["name"].lower() != raw.strip().lower():
                logger.info(
                    "[tts_switcher] /tts 「%s」模糊对上「%s」", raw, target["name"]
                )

            ok, detail = await self._switch(event, umo, target)
            if ok:
                yield event.plain_result(
                    self._msg(
                        "reply_switched",
                        "已切换到 {name}（{provider}）。",
                    ).format(name=target["name"], provider=target["provider_id"])
                )
            else:
                yield event.plain_result(f"没切成：{detail}")

        except Exception as exc:  # 兜底，别让插件把整个消息管道带崩
            logger.error(f"[tts_switcher] 处理 /tts 出错: {exc}", exc_info=True)
            yield event.plain_result("换声线的时候出错了，详细原因已经写进日志。")

    @filter.command("ttshelp", alias={"tts帮助", "声线帮助", "tts用法", "TTS帮助"})
    async def ttshelp_cmd(self, event: AstrMessageEvent):
        """TTS 声线帮助：查看 /tts、/说、/答 的完整用法"""
        if not self._flag("enabled", True):
            return
        if not self._scope_ok(event):
            yield event.plain_result(
                self._msg("reply_scope_denied", "这里不让换声线。")
            )
            return
        yield event.plain_result(_clip(self._full_help_text()))

    # ------------------------------------------------------------ /说

    def _say_allowed(self, event: AstrMessageEvent) -> bool:
        """默认所有人都能用（念句话而已），allow_all 关掉才回落到管理员。"""
        if self._flag("say.allow_all", True):
            return True
        return self._can_use(event)

    def _scope_ok_for(self, prefix: str, event: AstrMessageEvent) -> bool:
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:
            group_id = ""
        if group_id:
            return self._flag(f"{prefix}.allow_group", True)
        return self._flag(f"{prefix}.allow_private", True)

    def _say_scope_ok(self, event: AstrMessageEvent) -> bool:
        return self._scope_ok_for("say", event)

    def _answer_scope_ok(self, event: AstrMessageEvent) -> bool:
        return self._scope_ok_for("answer", event)

    def _cool_left(
        self, cfg_path: str, store: dict[str, float], key: str, default: float
    ) -> float:
        try:
            cooldown = float(self._cfg(cfg_path, default) or 0)
        except (TypeError, ValueError):
            cooldown = float(default)
        if cooldown <= 0:
            return 0.0
        elapsed = time.time() - store.get(key, 0.0)
        return max(0.0, cooldown - elapsed)

    def _say_cooldown_left(self, key: str) -> float:
        return self._cool_left("say.cooldown", self._say_last, key, 5.0)

    def _answer_cooldown_left(self, key: str) -> float:
        return self._cool_left("answer.cooldown", self._answer_last, key, 5.0)

    @staticmethod
    def _clean_say_text(text: Any) -> str:
        raw = str(text or "").replace("\r", " ").replace("\n", " ")
        return re.sub(r"\s{2,}", " ", raw).strip()

    def _match_prefix(
        self, raw: str, voices: list[dict[str, Any]]
    ) -> tuple[dict[str, Any] | None, str]:
        """把 raw 开头当成声线名来切，取命中的最长名字。

        好处是声线名本身带空格也能切对（比如“示例 1”这种），而且
        「示例A flash 你好」不会因为前缀「示例A」先命中而切歪。
        rest 为空表示只写了声线、没写正文。
        """
        low = raw.lower()
        best: tuple[int, dict[str, Any], str] | None = None
        for voice in voices:
            for candidate in (voice["name"], *voice["aliases"]):
                name = str(candidate).strip()
                if not name:
                    continue
                key = name.lower()
                if not low.startswith(key):
                    continue
                rest = raw[len(name) :]
                if rest and rest[0] not in VOICE_SEPS:
                    continue
                if best is None or len(key) > best[0]:
                    best = (len(key), voice, rest)
        if best is None:
            return None, raw
        return best[1], best[2].lstrip(VOICE_SEPS)

    @staticmethod
    def _voice_base(name: str) -> str:
        """去掉末尾的档位词，拿到基名：「示例A flash」「示例A plus」都是「示例A」。"""
        base = str(name or "").strip()
        for suffix in VOICE_TIER_WORDS:
            if base.lower().endswith(suffix):
                return base[: len(base) - len(suffix)].lower()
        return base.lower()

    def _pick_fuzzy(
        self, candidates: list[dict[str, Any]], needle: str
    ) -> dict[str, Any] | None:
        """模糊命中多个时的取舍。

        只是同一个名字的不同档（示例A flash / 示例A plus）就取配置里的第一个，
        免得用户非得写全名；真分不清（示例B flash / 示例C flash）就不猜，返回 None
        由调用方提示「能对上好几个」。
        """
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        bases = {self._voice_base(c["name"]) for c in candidates}
        if len(bases) == 1:
            picked = candidates[0]
            logger.info(
                "[tts_switcher] 「%s」对上了多个，先用「%s」（同基名不同档：%s）",
                needle,
                picked["name"],
                " / ".join(c["name"] for c in candidates),
            )
            return picked
        return None

    def _resolve_voice(
        self, raw: str, voices: list[dict[str, Any]]
    ) -> tuple[dict[str, Any] | None, str, str, list[dict[str, Any]]]:
        """把等号后面那串拆成 (声线, 正文, 没找到的名字, 分不清的候选)。

        规则：
        1) 名字/别名完全对应（后面紧跟分隔符或已到结尾）→ 直接用它，剩下的是正文；
        2) 没有完全对应 → 拿第一段去声线表里模糊搜索：
           - 只命中一个，或命中的都是同一个基名的不同档（xx flash / xx plus）→ 取第一个；
           - 命中好几个不同基名 → 不猜，返回候选让调用方提示说全名；
        3) 一个都没命中 → 第三个返回值是名字，由调用方回「没找到这个声线」。
        """
        if not voices:
            return None, raw, "", []

        # 1) 完全对应：从头按最长声线名/别名切（空白、逗号、等号、竖线都算分隔）
        voice, rest = self._match_prefix(raw, voices)
        if voice is not None:
            return voice, rest, "", []

        # 2) 显式竖线/等号分隔：名字没配进列表也允许模糊猜一把
        match = re.match(r"^(.{1,32}?)\s*[|｜=＝]\s*(.+)$", raw, re.S)
        if match:
            keyword = match.group(1).strip()
            candidates = self._match(keyword, voices)
            picked = self._pick_fuzzy(candidates, keyword)
            if picked is not None:
                return picked, match.group(2).strip(), "", []
            if candidates:
                return None, raw, "", candidates

        # 3) 兜底写法：第一段当声线名，允许模糊与多打字
        match = re.match(r"^(\S{1,32})[\s\u3000,，:：、|｜]+(.+)$", raw, re.S)
        keyword = match.group(1) if match else raw.strip()
        text = match.group(2).strip() if match else ""
        candidates = self._match(keyword, voices)
        picked = self._pick_fuzzy(candidates, keyword)
        if picked is not None:
            return picked, text, "", []
        if candidates:
            return None, raw, "", candidates

        # 4) 没找到
        return None, raw, keyword, []

    def _voice_whitelist_ok(self, voice: dict[str, Any], cfg_path: str) -> bool:
        """cfg_path 里配的白名单为空表示不限制。"""
        allowed = _split_names(self._cfg(cfg_path, ""))
        if not allowed:
            return True
        names = {item.lower() for item in allowed}
        if voice["name"].lower() in names:
            return True
        return any(alias.lower() in names for alias in voice["aliases"])

    def _say_voice_allowed(self, voice: dict[str, Any]) -> bool:
        return self._voice_whitelist_ok(voice, "say.voices")

    async def _synthesize(
        self, provider: Any, text: str, timeout: float
    ) -> tuple[str, str]:
        """调 provider.get_audio() 合成，返回 (音频路径, 错误)。"""
        last_error = ""
        for attempt in range(1, SAY_MAX_ATTEMPT + 1):
            audio = ""
            try:
                audio = await asyncio.wait_for(provider.get_audio(text), timeout=timeout)
            except asyncio.TimeoutError:
                last_error = f"合成超时（超过 {int(timeout)} 秒）"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if audio:
                path = str(audio)
                if os.path.exists(path):
                    return path, ""
                last_error = f"合成返回的文件不存在：{path}"
            logger.warning(
                f"[tts_switcher] /说 第 {attempt}/{SAY_MAX_ATTEMPT} 次合成失败：{last_error}"
            )
            if attempt < SAY_MAX_ATTEMPT:
                await asyncio.sleep(1.0 * attempt)
        return "", last_error or "未知错误"

    async def _say_send(self, event: AstrMessageEvent, text: str) -> None:
        """主动发文字（不走 result_decorate，所以不会被 TTS 再转一次）。"""
        event.stop_event()
        try:
            await event.send(MessageChain().message(text))
        except Exception as exc:
            logger.error(f"[tts_switcher] 发送文字失败: {exc}")

    @filter.command("说", alias={"say", "speak", "念", "读"})
    async def say_cmd(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """/说 <文本>：用当前声线念这句，整段都当正文。"""
        await self._do_say(event, args)

    @filter.regex(SAY_EQ_RE)
    async def say_eq_cmd(self, event: AstrMessageEvent):
        """等号写法：/说=声线 句子（只有这种写法会解析声线）"""
        if not self._flag("say.enable", True):
            return
        rest = _regex_rest(SAY_EQ_RE, event)
        if not rest:
            return
        await self._do_say(event, rest, explicit=True)

    @filter.regex(NL_SAY_RE)
    async def say_nl(self, event: AstrMessageEvent):
        """自然语言入口（配置里默认关闭）：用示例的声音说 今天风很好"""
        if not self._flag("say.nl_enable", False):
            return
        if not getattr(event, "is_wake", False):
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text or text.startswith("/"):
            return
        match = NL_SAY_RE.search(text)
        if not match:
            return
        # 自然语言里声线是明说的，按「指定声线」处理（explicit）
        await self._do_say(
            event,
            f"{match.group('voice')} | {match.group('text')}",
            explicit=True,
        )

    async def _do_say(
        self, event: AstrMessageEvent, args: Any, *, explicit: bool = False
    ) -> None:
        """explicit=True 只在 /说= 等号写法下置位：只有它去解析声线名。

        普通 /说 <文本> 不解析声线，否则「/说 罕见 骂谁罕见」会被当成
        声线「罕见」+ 正文「骂谁罕见」，跟「用当前声线念这句话」的语义打架。
        """
        if not self._flag("enabled", True) or not self._flag("say.enable", True):
            return

        raw = self._clean_say_text(args)
        if not raw or raw.lower() in HELP_WORDS:
            await self._say_send(event, self._msg("help_say", DEFAULT_SAY_HELP))
            return

        if not self._say_scope_ok(event):
            await self._say_send(
                event, self._msg("reply_say_scope_denied", "这里不让用 /说。")
            )
            return

        if not self._say_allowed(event):
            await self._say_send(event, self._msg("reply_say_denied", "只有管理员能用 /说。"))
            return

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            sender = ""
        key = f"{umo}#{sender}" if sender else umo

        left = self._say_cooldown_left(key)
        if left > 0:
            await self._say_send(event, f"慢一点，{max(1, int(left + 0.999))} 秒后再来。")
            return

        voices = self._voices()
        if explicit:
            voice, text, missing, ambiguous = self._resolve_voice(raw, voices)
            text = self._clean_say_text(text)
            if missing:
                await self._say_send(
                    event,
                    self._msg(
                        "reply_say_unknown_voice",
                        "没找到声线「{name}」。用 /tts 列表 看看有哪些。",
                    ).format(name=missing),
                )
                return
            if ambiguous:
                names = "、".join(v["name"] for v in ambiguous[:10])
                await self._say_send(
                    event, f"这名字能对上好几个，写全一点：\n{names}"
                )
                return
        else:
            # 命令行写法：不解析声线，整段都是正文
            voice, text = None, raw

        if voice is not None and not self._say_voice_allowed(voice):
            await self._say_send(event, f"「{voice['name']}」现在不让用，换一个吧。")
            return

        try:
            max_chars = int(self._cfg("say.max_chars", 200) or 0)
        except (TypeError, ValueError):
            max_chars = 200
        truncated = False
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars].strip()
            truncated = True

        if not text:
            if voice is not None:
                hint = f"要念什么？例如：/说={voice['name']} 你好"
            else:
                hint = "要念什么？例如：/说 示例 今天风很好"
            await self._say_send(event, self._msg("reply_say_empty", hint))
            return

        label = ""
        if voice is not None:
            provider = self._resolve_provider(voice["provider_id"])
            label = voice["name"]
            if provider is None:
                await self._say_send(
                    event,
                    self._msg(
                        "reply_say_no_provider",
                        "声线「{name}」对应的提供商没在跑（{provider}）。",
                    ).format(name=voice["name"], provider=voice["provider_id"]),
                )
                return
        else:
            provider = await self._current_instance(umo)
            if provider is None:
                await self._say_send(
                    event,
                    "当前会话没有可用的 TTS 提供商，先用 /tts <名字> 选一个，"
                    "或者直接写 /说=<声线> <文本>。",
                )
                return
            label = _provider_id(provider) or "当前声线"

        try:
            await event.send_typing()
        except Exception:
            pass

        try:
            timeout = float(self._cfg("say.timeout", 90) or 90)
        except (TypeError, ValueError):
            timeout = 90.0

        try:
            audio, error = await self._synthesize(provider, text, timeout)
        except Exception as exc:
            logger.error(f"[tts_switcher] /说 合成异常: {exc}", exc_info=True)
            audio, error = "", f"{type(exc).__name__}: {exc}"

        if not audio:
            await self._say_send(event, f"没合成出来（{label}）：{error}")
            return

        self._say_last[key] = time.time()
        event.stop_event()
        try:
            chain = MessageChain()
            chain.chain.append(Record(file=audio, url=audio, text=text))
            await event.send(chain)
        except Exception as exc:
            logger.error(f"[tts_switcher] 发送语音失败: {exc}", exc_info=True)
            await self._say_send(event, f"语音发送失败：{exc}")
            return

        logger.info(f"[tts_switcher] {umo} /说 via {label}：{text[:30]}")

        if self._flag("say.append_text", False):
            await self._say_send(event, text)
        elif truncated and self._flag("say.notice_truncate", True):
            await self._say_send(
                event, f"（原话超过 {max_chars} 字，只念了前 {max_chars} 字）"
            )

    # ------------------------------------------------------------ /答

    def _answer_expire(self) -> float:
        try:
            return max(10.0, float(self._cfg("answer.expire", 300) or 300))
        except (TypeError, ValueError):
            return 300.0

    async def _do_answer(
        self, event: AstrMessageEvent, args: Any, *, explicit: bool = False
    ):
        """/答：让机器人用指定声线"回复"这句话。

        explicit=True 只在 /答= 等号写法下置位；普通 /答 <文本> 不解析声线名。

        实现上不去动会话偏好：这里只记下"这一轮要用哪个 provider"，等大模型把回复
        写好后，由 on_decorating_result 钩子把文字换成语音。好处是不需要事后还原，
        中途失败也不会把 /tts 的设置改坏。
        """
        if not self._flag("enabled", True) or not self._flag("answer.enable", True):
            return

        raw = self._clean_say_text(args)
        if not raw or raw.lower() in HELP_WORDS:
            yield event.plain_result(self._msg("help_answer", DEFAULT_ANSWER_HELP))
            return

        if not self._answer_scope_ok(event):
            yield event.plain_result(
                self._msg("reply_answer_scope_denied", "这里不让用 /答。")
            )
            return

        if not self._flag("answer.allow_all", True) and not self._can_use(event):
            yield event.plain_result(
                self._msg("reply_answer_denied", "只有管理员能用 /答。")
            )
            return

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            yield event.plain_result("拿不到当前会话标识，这条先用不了。")
            return

        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            sender = ""
        key = f"{umo}#{sender}" if sender else umo

        left = self._answer_cooldown_left(key)
        if left > 0:
            yield event.plain_result(f"慢一点，{max(1, int(left + 0.999))} 秒后再来。")
            return

        voices = self._voices()
        if explicit:
            voice, text, missing, ambiguous = self._resolve_voice(raw, voices)
            text = self._clean_say_text(text)
            if missing:
                yield event.plain_result(
                    self._msg(
                        "reply_answer_unknown_voice",
                        "没找到声线「{name}」。用 /tts 列表 看看有哪些。",
                    ).format(name=missing)
                )
                return
            if ambiguous:
                names = "、".join(v["name"] for v in ambiguous[:10])
                yield event.plain_result(
                    f"这名字能对上好几个，写全一点：\n{names}"
                )
                return
        else:
            # 命令行写法：不解析声线，整段都是正文
            voice, text = None, raw
        if not text:
            hint = (
                f"要回复什么？例如：/答={voice['name']} 今天天气怎么样"
                if voice is not None
                else "要回复什么？例如：/答 示例 今天天气怎么样"
            )
            yield event.plain_result(self._msg("reply_answer_empty", hint))
            return

        if voice is not None:
            if not self._voice_whitelist_ok(voice, "answer.voices"):
                yield event.plain_result(f"「{voice['name']}」现在不让用，换一个吧。")
                return
            provider = self._resolve_provider(voice["provider_id"])
            if provider is None:
                yield event.plain_result(
                    self._msg(
                        "reply_answer_no_provider",
                        "声线「{name}」对应的提供商没在跑（{provider}）。",
                    ).format(name=voice["name"], provider=voice["provider_id"])
                )
                return
            label = voice["name"]
        else:
            provider = await self._current_instance(umo)
            if provider is None:
                yield event.plain_result(
                    "当前会话没有可用的 TTS 提供商，先用 /tts <名字> 选一个，"
                    "或者写 /答=<声线> <文本>。"
                )
                return
            label = _provider_id(provider) or "当前声线"

        provider_id = _provider_id(provider)
        self._pending_answer[umo] = {
            "provider_id": provider_id,
            "label": label,
            "expires": time.time() + self._answer_expire(),
        }
        self._answer_last[key] = time.time()

        # 让后面的 LLM 只看到正文，不带 /答、声线名这些字样
        try:
            event.message_str = text
        except Exception as exc:
            logger.debug(f"[tts_switcher] 改写消息文本失败: {exc}")

        logger.info(f"[tts_switcher] {umo} /答 via {label}：{text[:30]}")

    @filter.command("答", alias={"回", "回复", "reply", "answer"})
    async def answer_cmd(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """/答 <文本>：用当前声线回复这句，整段都当正文。"""
        async for result in self._do_answer(event, args):
            yield result

    @filter.regex(ANSWER_EQ_RE)
    async def answer_eq_cmd(self, event: AstrMessageEvent):
        """等号写法：/答=声线 句子（只有这种写法会解析声线）"""
        if not self._flag("answer.enable", True):
            return
        rest = _regex_rest(ANSWER_EQ_RE, event)
        if not rest:
            return
        async for result in self._do_answer(event, rest, explicit=True):
            yield result

    @filter.on_decorating_result()
    async def decorate_answer(self, event: AstrMessageEvent):
        """把 /答 指定的那一条回复换成语音（在官方 TTS 装饰之前动手）。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        info = self._pending_answer.pop(umo, None)
        if not info:
            return
        if time.time() > float(info.get("expires", 0)):
            logger.debug("[tts_switcher] /答 已过期，这条按普通回复处理")
            return
        if not self._flag("enabled", True) or not self._flag("answer.enable", True):
            return

        result = event.get_result()
        if result is None or not result.chain:
            return
        # 流式输出拿不到完整文本，交给官方 TTS 的概率开关
        if "STREAMING" in str(getattr(result, "result_content_type", "")):
            return
        # 带图片、At 等混合结果不动，免得把别的段吃掉
        if any(not isinstance(comp, Plain) for comp in result.chain):
            logger.debug("[tts_switcher] /答 结果是富文本，跳过替换")
            return

        text = "".join(comp.text for comp in result.chain).strip()
        if not text:
            return

        provider = self._resolve_provider(str(info.get("provider_id") or ""))
        if provider is None:
            logger.warning(
                f"[tts_switcher] /答 用的 provider 不在了（{info.get('provider_id')}），退回文字"
            )
            return

        records = await self._answer_records(provider, text)
        if not records:
            return
        result.chain = records
        logger.info(
            f"[tts_switcher] {umo} /答 已用 {info.get('label')} 合成 {len(records)} 条语音"
        )

    async def _answer_records(self, provider: Any, text: str) -> list[Any]:
        """把回复正文切成若干段分别合成，返回 Record 列表（失败返回空）。"""
        try:
            limit = int(self._cfg("answer.segment_chars", 200) or 0)
        except (TypeError, ValueError):
            limit = 200
        try:
            timeout = float(self._cfg("answer.timeout", 120) or 120)
        except (TypeError, ValueError):
            timeout = 120.0

        segments = _split_text(text, limit) if limit > 0 else [text]
        records: list[Any] = []
        for segment in segments:
            audio, error = await self._synthesize(provider, segment, timeout)
            if not audio:
                logger.warning(f"[tts_switcher] /答 合成失败，这条改发文字：{error}")
                return []
            records.append(Record(file=audio, url=audio, text=segment))
        return records

    # ------------------------------------------------------------ 动作

    def _engine_text(self, voices: list[dict[str, Any]]) -> str:
        providers = self._providers()
        if not providers:
            return "现在一个 TTS 提供商都没有，先去面板的「服务提供商」里加一个。"
        mapped = {v["provider_id"].lower(): v["name"] for v in voices}
        lines = [f"TTS 提供商（{len(providers)}）"]
        for provider in providers:
            pid = _provider_id(provider) or "（无 id）"
            name = _provider_name(provider)
            row = f"· {pid}"
            if name and name != pid:
                row += f"（{name}）"
            alias = mapped.get(pid.lower())
            if alias:
                row += f"  已映射为「{alias}」"
            lines.append(row)
        lines.append("把这些 id 填进插件配置的「声线映射」里即可。")
        return "\n".join(lines)

    async def _switch(
        self,
        event: AstrMessageEvent,
        umo: str,
        voice: dict[str, Any],
    ) -> tuple[bool, str]:
        manager = getattr(self.context, "provider_manager", None)
        if manager is None:
            return False, "拿不到 provider_manager，插件可能加载异常"
        if not umo:
            return False, "拿不到当前会话标识"

        provider = self._resolve_provider(voice["provider_id"])
        if provider is None:
            return False, (
                f"没有找到 id 为 {voice['provider_id']} 的 TTS 提供商。"
                "发 /tts 引擎 看看现在都有哪些，id 改了要重启核心"
            )

        provider_id = _provider_id(provider)
        try:
            await manager.set_provider(
                provider_id=provider_id,
                provider_type=TTS_TYPE,
                umo=umo,
            )
        except Exception as exc:
            logger.error(f"[tts_switcher] 设置 TTS 提供商失败: {exc}", exc_info=True)
            return False, f"写入失败：{exc}"

        # 顺手把会话的 TTS 开关打开，不然切了也没声音，容易让人以为没生效
        if self._flag("access.auto_enable", True):
            await self._set_enabled(umo, True)

        logger.info(f"[tts_switcher] {umo} 切换到 {provider_id}")
        return True, ""

    async def _set_enabled(self, umo: str, enabled: bool) -> None:
        if sp is None or not umo:
            return
        try:
            config = await sp.session_get(umo, SESSION_CFG_KEY, {}) or {}
        except Exception:
            config = {}
        if not isinstance(config, dict):
            config = {}
        config["tts_enabled"] = bool(enabled)
        try:
            await sp.session_put(umo, SESSION_CFG_KEY, config)
        except Exception as exc:
            logger.error(f"[tts_switcher] 写入 TTS 开关失败: {exc}", exc_info=True)

    async def _toggle(
        self, event: AstrMessageEvent, umo: str, enabled: bool
    ) -> str:
        if sp is None:
            return "这个 AstrBot 版本没有 shared_preferences，改不了会话开关。"
        await self._set_enabled(umo, enabled)
        if enabled:
            return self._msg("reply_tts_on", "本会话语音已开启。")
        return self._msg("reply_tts_off", "本会话语音已关闭，之后只发文字。")

    async def _reset(self, event: AstrMessageEvent, umo: str) -> str:
        if sp is None or not umo:
            return "这个 AstrBot 版本没有 shared_preferences，改不了会话设置。"
        try:
            await sp.session_remove(umo, TTS_PREF_KEY)
        except Exception as exc:
            logger.error(f"[tts_switcher] 清除会话 TTS 偏好失败: {exc}", exc_info=True)
            return f"清除失败：{exc}"

        preference, instance = await self._current(umo)
        provider_id = _provider_id(instance) or "未解析到"
        return self._msg(
            "reply_reset",
            "已清掉本会话的声线设置，回到全局默认（{provider}）。",
        ).format(provider=provider_id)
