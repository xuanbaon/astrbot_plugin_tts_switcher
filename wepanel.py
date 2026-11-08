"""WebUI 面板后端：给「TTS 声线切换」插件页面提供读写声线与指令设置的接口。

页面本身放在 pages/voice_console/ 下，通过 AstrBot 的插件页面桥
（window.AstrBotPluginPage）调用这里的接口，路由前缀由核心自动拼成
/astrbot_plugin_tts_switcher/<route>。

写配置的策略：只改 JSON 文件里相关的那几个键，其余键原样保留；写完再同步
插件内存里的 config，这样面板保存后立刻生效，不用重启核心。

供应商声线自动读取：核心的 TTS 提供商（面板 → 模型供应商）里其实已经写了
音色字段（通义的 qwen_audio_tts_voice、OpenAI 的 openai_tts_voice、本地
GPT-SoVITS 的权重文件等）。这里把它们读出来做成一张「供应商声线」清单，
没有对应声线条目的自动补一条进配置，供应商被改名/删掉时把旧条目判断为
「供应商已不存在」，可以在页面上一键改绑。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from astrbot.api import logger

try:  # 核心用的是 quart
    from quart import jsonify, request
except Exception:  # pragma: no cover
    from flask import jsonify, request  # type: ignore[no-redef]

PLUGIN_NAME = "astrbot_plugin_tts_switcher"

# 面板上允许改的设置项（白名单，避免误改 schema 之外的键）
BOOL_SETTINGS = (
    "enabled",
    "voices_auto_load",
    "access.allow_all",
    "access.allow_group",
    "access.allow_private",
    "access.auto_enable",
    "say.enable",
    "say.allow_all",
    "say.nl_enable",
    "say.append_text",
    "say.notice_truncate",
    "say.allow_group",
    "say.allow_private",
    "answer.enable",
    "answer.allow_all",
    "answer.allow_group",
    "answer.allow_private",
    "help.inject_into_help",
    "list.show_provider_id",
    "list.forward",
)
INT_SETTINGS = (
    "say.max_chars",
    "say.cooldown",
    "say.timeout",
    "answer.cooldown",
    "answer.expire",
    "answer.segment_chars",
    "answer.timeout",
    "list.single_max",
    "list.forward_rows",
)
STR_SETTINGS = ("access.extra_admins", "say.voices", "answer.voices")

# 供应商配置里，哪种键算「音色」
_VOICE_KEY_RE = re.compile(r"(voice|voices|speaker|timbre|singer)$", re.I)
# 哪种键算「声音身份」（本地模型的权重、参考音频），只用来显示和生成备注
_IDENTITY_KEY_RE = re.compile(r"(weights_path|ref_audio_path|ckpt|checkpoint)$", re.I)
_HEX_SEG = re.compile(r"^[0-9a-f]{8,}$", re.I)


async def _read_json() -> dict:
    """读请求体；空 body 或非 JSON 就当空字典。"""
    try:
        data = await request.get_json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _config_path(plugin: Any) -> Path | None:
    """插件的配置文件路径。"""
    path = getattr(plugin.config, "config_path", None)
    if isinstance(path, str) and path:
        return Path(path)
    # 兜底：按核心的约定拼一个
    core = Path(__file__).resolve().parents[2]
    guess = core / "data" / "config" / f"{PLUGIN_NAME}_config.json"
    return guess if guess.parent.is_dir() else None


def _read_file(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("[tts_switcher] 读取配置失败 %s: %s", path, exc)
        return {}


def _write_file(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _get_path(data: dict, dotted: str, default: Any = None) -> Any:
    node: Any = data
    for key in dotted.split("."):
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return default
    return default if node is None else node


def _set_path(data: dict, dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    node = data
    for key in keys[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[keys[-1]] = value


def _provider_cfg(provider: Any) -> dict:
    """把 provider 实例上的配置字典掏出来（不同核心版本放的位置不一样）。"""
    for attr in ("provider_config", "config"):
        cfg = getattr(provider, attr, None)
        if isinstance(cfg, dict) and cfg:
            return cfg
    return {}


def _provider_id(provider: Any) -> str:
    cfg = _provider_cfg(provider)
    for key in ("id", "provider_id"):
        value = cfg.get(key)
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


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "开", "是"}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _split_alias(text: Any) -> list[str]:
    """别名分隔：半角逗号 , 与全角逗号 ，都认，另外兼容顿号/分号/竖线/空格。"""
    if isinstance(text, list):
        return [str(x).strip() for x in text if str(x).strip()]
    raw = str(text or "")
    for sep in ("，", ",", "、", ";", "；", "|", "/", " "):
        raw = raw.replace(sep, ",")
    return [x.strip() for x in raw.split(",") if x.strip()]


# ------------------------------------------------------------ 供应商声线


def _str_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, dict):
        # {"zh-CN-XiaoxiaoNeural": "晓晓"} 这种映射，取键
        return [str(k).strip() for k in value if str(k).strip()]
    return []


def _short_voice(value: str) -> str:
    """把一长串音色 id 缩写成人看得懂的一小段。"""
    text = str(value or "").strip()
    if len(text) <= 28:
        return text
    parts = [p for p in re.split(r"[-_/]", text) if p]
    keep: list[str] = []
    for part in reversed(parts):
        if _HEX_SEG.match(part):
            if keep:
                break
            continue
        keep.insert(0, part)
        if len(keep) >= 2:
            break
    short = "-".join(keep) if keep else text[:24]
    return f"…{short}"


def _is_voice_key(key: str) -> bool:
    if not isinstance(key, str) or not key.strip():
        return False
    low = key.strip().lower()
    if low.endswith(("_format", "_lang", "_language", "_volume", "_rate", "_speed")):
        return False
    if low in {"voice", "voices", "speaker", "timbre", "singer"}:
        return True
    return bool(_VOICE_KEY_RE.search(low))


def _is_identity_key(key: str) -> bool:
    if not isinstance(key, str) or not key.strip():
        return False
    return bool(_IDENTITY_KEY_RE.search(key.strip().lower()))


# provider 配置条目里代表「音色」的字段名，按优先级找
VOICE_FIELDS = (
    "qwen_audio_tts_voice",
    "dashscope_tts_voice",
    "openai_tts_voice",
    "tts_voice",
    "voice_id",
    "voice",
    "speaker",
    "timbre",
)

# provider 实例上代表「音色」的属性名（qwen / edge / openai 都是 self.voice）
VOICE_ATTRS = ("voice", "voice_id", "speaker", "timbre", "singer")


def _entry_voice_key(entry: Any) -> tuple[str, str]:
    """从 provider 配置条目里挑出「音色」字段，返回 (字段名, 当前值)。

    只认「单个字符串」的音色字段；像本地 GSV 的 voices（列表）或权重路径
    这类东西不在这里改，交由 GSV 自己的配置管。
    """
    if not isinstance(entry, dict):
        return "", ""
    for key in VOICE_FIELDS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return key, value.strip()
    for key, value in entry.items():
        if not _is_voice_key(key):
            continue
        values = _str_values(value)
        if len(values) == 1:
            return key, values[0]
    return "", ""


def _instance_voice(provider: Any) -> str:
    """读运行中实例的当前音色（比配置文件更准，改过实例时以实例为准）。"""
    for attr in VOICE_ATTRS:
        try:
            text = str(getattr(provider, attr, "") or "").strip()
        except Exception:
            continue
        if text:
            return text
    return ""


def _provider_instance(plugin: Any, provider_id: str) -> Any:
    wanted = str(provider_id or "").strip().lower()
    if not wanted:
        return None
    try:
        providers = plugin._providers()
    except Exception:
        return None
    for provider in providers or []:
        if _provider_id(provider).lower() == wanted:
            return provider
    return None


def _find_provider_entry(cfg: Any, provider_id: str) -> dict | None:
    """在主配置的 provider 列表里按 id 找条目（大小写不敏感）。"""
    wanted = str(provider_id or "").strip().lower()
    if not wanted or not isinstance(cfg, dict):
        return None
    rows = cfg.get("provider")
    if not isinstance(rows, list):
        return None
    for item in rows:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "").strip().lower() == wanted:
            return item
    return None


def _catalog_snapshot(plugin: Any) -> list[dict]:
    """当前核心里的所有 TTS 提供商 + 它们各自的音色。"""
    try:
        providers = plugin._providers()
    except Exception:
        providers = []
    rows: list[dict] = []
    seen: set[str] = set()
    for provider in providers:
        pid = _provider_id(provider)
        if not pid or pid.lower() in seen:
            continue
        seen.add(pid.lower())
        cfg = _provider_cfg(provider)
        voices: list[dict] = []
        identity: list[dict] = []
        for key, value in cfg.items():
            if _is_voice_key(key):
                for item in _str_values(value):
                    voices.append(
                        {"key": key, "value": item, "label": _short_voice(item)}
                    )
            elif _is_identity_key(key):
                text = str(value or "").strip()
                if text:
                    identity.append(
                        {
                            "key": key,
                            "value": text,
                            "label": Path(text.replace("\\", "/")).name,
                        }
                    )
        ptype = str(cfg.get("type") or "").strip()
        voice_key, voice_value = _entry_voice_key(cfg)
        # 实例上的属性才是「此刻真正会用的音色」——qwen/edge/openai 都是 self.voice
        current = _instance_voice(provider) or voice_value
        rows.append(
            {
                "id": pid,
                "name": _provider_name(provider) or pid,
                "type": ptype,
                "model": str(cfg.get("model") or "").strip(),
                "local": "gsv" in ptype.lower()
                or any(k in pid.lower() for k in ("gsv", "sovits", "local")),
                "voices": voices,
                "identity": identity,
                "voice_key": voice_key,
                "voice": voice_value,
                "current_voice": current,
            }
        )
    rows.sort(key=lambda r: (not r["local"], r["id"].lower()))
    return rows


def _auto_note(row: dict, opt: dict | None = None) -> str:
    parts: list[str] = []
    if row.get("local"):
        parts.append("本地语音合成")
    elif row.get("model"):
        parts.append(row["model"])
    elif row.get("type"):
        parts.append(row["type"])
    if opt:
        parts.append(f"音色 {opt['label']}")
    for item in (row.get("identity") or [])[:1]:
        parts.append(item["label"])
    parts.append("自动读取")
    return " · ".join(parts)


def _unique_name(base: str, used: set[str]) -> str:
    name = re.sub(r"\s+", " ", str(base or "").strip())[:24] or "声线"
    if name.lower() not in used:
        used.add(name.lower())
        return name
    stem = name[:20]
    for index in range(2, 500):
        candidate = f"{stem} {index}"
        if candidate.lower() not in used:
            used.add(candidate.lower())
            return candidate
    fallback = f"{stem} {os.urandom(2).hex()}"
    used.add(fallback.lower())
    return fallback


def _voice_rows(plugin: Any) -> list[dict]:
    """把配置里的 voices 整理成面板要的扁平结构（保留原始字段）。"""
    raw = plugin.config.get("voices")
    if not isinstance(raw, list):
        return []
    rows: list[dict] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "index": idx,
                "name": str(item.get("name") or "").strip(),
                "provider_id": str(item.get("provider_id") or "").strip(),
                "voice": str(item.get("voice") or "").strip(),
                "alias": str(item.get("alias") or "").strip(),
                "note": str(item.get("note") or "").strip(),
                "enabled": _as_bool(item.get("enabled", True), True),
            }
        )
    return rows


def _tts_provider_rows(plugin: Any) -> list[dict]:
    """主配置里所有 TTS 供应商的原始条目（含已被禁用的那些）。"""
    context = getattr(plugin, "context", None)
    getter = getattr(context, "get_config", None)
    if not callable(getter):
        return []
    try:
        cfg = getter()
    except Exception as exc:  # pragma: no cover - 核心接口异常只降级不报错
        logger.debug(f"[tts_switcher] 读取主配置失败: {exc}")
        return []
    rows = cfg.get("provider") if isinstance(cfg, dict) else None
    if not isinstance(rows, list):
        return []

    result: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        ptype = str(item.get("provider_type") or "").strip().lower()
        adapter = str(item.get("type") or "").strip().lower()
        if ptype != "text_to_speech" and "tts" not in adapter:
            continue
        result.append(item)
    return result


def _provider_enable_map(plugin: Any) -> dict[str, bool]:
    """主配置里所有 TTS 供应商的启用状态（含已被禁用的那些）。

    ``_catalog_snapshot`` 只拿得到「已启用且实例化成功」的供应商，单看它
    分不清「供应商被禁用」和「供应商已被删掉」，于是再去主配置里捞一遍，
    这样面板才能给出准确提示。
    """
    result: dict[str, bool] = {}
    for item in _tts_provider_rows(plugin):
        pid = str(item.get("id") or "").strip()
        if pid:
            result[pid.lower()] = _as_bool(item.get("enable", True), True)
    return result


def _endpoint_port(api_base: str) -> int | None:
    """从 api_base 里取端口，取不出来返回 None。"""
    text = str(api_base or "").strip()
    if not text:
        return None
    if "//" not in text:
        text = f"//{text}"
    try:
        parts = urlsplit(text)
        if parts.port:
            return int(parts.port)
        return 443 if parts.scheme == "https" else 80
    except Exception:
        return None


def _endpoint_online(api_base: str, timeout: float = 0.3) -> bool:
    """探一下端口是否在听（只为面板显示，超时压得很短，失败当离线）。"""
    text = str(api_base or "").strip()
    if not text:
        return False
    if "//" not in text:
        text = f"//{text}"
    try:
        parts = urlsplit(text)
        host = parts.hostname or "127.0.0.1"
        port = _endpoint_port(api_base)
        if not port:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _is_local_endpoint(api_base: str) -> bool:
    """只看本机/内网的端口：云端 TTS（dashscope 之类）不占端口，别混进来。"""
    text = str(api_base or "").strip()
    if not text:
        return False
    if "//" not in text:
        text = f"//{text}"
    try:
        host = (urlsplit(text).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if host.startswith("127.") or host.endswith(".local"):
        return True
    if host.startswith(("10.", "192.168.")):
        return True
    # 172.16.0.0/12
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) > 1 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return True
    return False


def _gsv_lazy_on(plugin: Any) -> bool:
    """GSV 权重是否已经改成「合成时加载」。

    配置开着、补丁也打上了才算生效——插件被禁用或者核心换了实现时要如实说没生效，
    否则面板会骗人（还标着「按需切换」，其实每个音色都在用最后那套权重）。
    """
    conf = getattr(plugin, "config", None)
    if isinstance(conf, dict) and not _as_bool(conf.get("gsv_lazy_weights", True), True):
        return False
    try:
        from .gsv_patch import is_installed

        return bool(is_installed())
    except Exception:
        return False


def _port_roles(plugin: Any) -> list[dict]:
    """按 api_base（端口）把 TTS 供应商分组，回答「这个端口上挂了哪些角色」。

    同一个 GPT-SoVITS 服务端同一时刻只保留一套权重，而核心是一个 provider
    一组权重、初始化时逐条 set——所以一个端口上挂多个角色时，只有最后被
    set 的那个会生效，其余都拿不到自己的音色。面板把这层关系摊开，便于
    判断哪些角色可以并、哪些必须各起一个端口。
    """
    bound: dict[str, list[str]] = {}
    for row in _voice_rows(plugin):
        pid = str(row.get("provider_id") or "").strip().lower()
        if pid:
            bound.setdefault(pid, []).append(row.get("name") or row.get("provider_id") or pid)

    groups: dict[str, dict] = {}
    for item in _tts_provider_rows(plugin):
        base = str(item.get("api_base") or "").strip().rstrip("/")
        if not base or not _is_local_endpoint(base):
            continue
        key = base.lower()
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "api_base": base,
                "port": _endpoint_port(base),
                "roles": [],
                "online": False,
            }
        pid = str(item.get("id") or "").strip()
        parms = item.get("gsv_default_parms")
        ref_audio = ""
        if isinstance(parms, dict):
            ref_audio = str(parms.get("gsv_ref_audio_path") or "")
        group["roles"].append(
            {
                "provider_id": pid,
                "enabled": _as_bool(item.get("enable", True), True),
                "adapter": str(item.get("type") or ""),
                "gpt": os.path.basename(str(item.get("gpt_weights_path") or "")),
                "sovits": os.path.basename(str(item.get("sovits_weights_path") or "")),
                "ref_audio": os.path.basename(ref_audio),
                "voices": bound.get(pid.lower(), []),
            }
        )

    for group in groups.values():
        roles = group["roles"]
        group["total"] = len(roles)
        group["enabled_count"] = sum(1 for r in roles if r["enabled"])
        # 核心按配置顺序逐条初始化，也就是逐条 set 权重，最后一条赢
        active = ""
        for role in roles:
            if role["enabled"] and (role["gpt"] or role["sovits"]):
                active = role["provider_id"]
        group["active"] = active
        group["online"] = _endpoint_online(group["api_base"])

    return sorted(groups.values(), key=lambda g: (-g["total"], str(g["api_base"])))


def _annotate_suppliers(
    voices: list[dict],
    catalog: list[dict],
    enable_map: dict[str, bool],
) -> dict[str, int]:
    """给每条声线打上供应商状态，并返回各状态计数。

    - ready    供应商有可用实例，能直接切
    - disabled 供应商还在主配置里，但 enable=false
    - error    供应商是启用状态，却没加载出实例（初始化失败）
    - missing  主配置里根本没有这个 id（改名或删掉了）
    """
    ready_ids = {str(row.get("id") or "").lower() for row in catalog}
    stats = {"ready": 0, "disabled": 0, "missing": 0, "error": 0}
    for item in voices:
        pid = str(item.get("provider_id") or "").strip().lower()
        if pid and pid in ready_ids:
            state_name = "ready"
        elif pid and pid in enable_map:
            state_name = "disabled" if not enable_map[pid] else "error"
        else:
            state_name = "missing"
        item["supplier"] = state_name
        stats[state_name] += 1
    return stats


def _plan_sync(plugin: Any) -> tuple[list[dict], list[dict], list[dict]]:
    """算出「供应商里有、声线列表里还没有」和「声线列表里有、供应商已经没了」。"""
    catalog = _catalog_snapshot(plugin)
    # 被禁用（或加载失败）的供应商在主配置里仍然存在，不算「已经没了」
    enabled_map = _provider_enable_map(plugin)
    raw = plugin.config.get("voices")
    voices = [v for v in raw if isinstance(v, dict)] if isinstance(raw, list) else []

    covered: dict[str, set[str]] = {}
    for item in voices:
        pid = str(item.get("provider_id") or "").strip().lower()
        if not pid:
            continue
        covered.setdefault(pid, set()).add(str(item.get("voice") or "").strip().lower())

    known_ids = {row["id"].lower() for row in catalog} | set(enabled_map)
    pending: list[dict] = []
    for row in catalog:
        have = covered.get(row["id"].lower(), set())
        if not row["voices"]:
            if not have:
                pending.append(
                    {
                        "provider_id": row["id"],
                        "voice": "",
                        "label": row["name"],
                        "note": _auto_note(row),
                        "local": row["local"],
                    }
                )
            continue
        if "" in have:  # 已经有一条绑到这个 provider 且没指定音色，够用了
            continue
        for opt in row["voices"]:
            if opt["value"].lower() in have:
                continue
            pending.append(
                {
                    "provider_id": row["id"],
                    "voice": opt["value"],
                    "label": f"{row['name']} · {opt['label']}",
                    "note": _auto_note(row, opt),
                    "local": row["local"],
                }
            )

    orphans: list[dict] = []
    for item in voices:
        pid = str(item.get("provider_id") or "").strip()
        if pid and pid.lower() not in known_ids:
            orphans.append(
                {
                    "name": str(item.get("name") or "").strip(),
                    "provider_id": pid,
                    "voice": str(item.get("voice") or "").strip(),
                }
            )
    return pending, orphans, catalog


def _scan_report(plugin: Any) -> dict:
    """扫一遍所有 TTS 供应商：哪些在线、现在是什么音色、哪些声线还没接上。

    报告除了给前端画表格，还带两样能直接判断的东西：
    - drift：声线里写的音色和供应商当前音色不一致的（改了但没同步）
    - added / removed：和上一次扫描相比，供应商多了谁、少了谁
    """
    catalog = _catalog_snapshot(plugin)
    enable_map = _provider_enable_map(plugin)
    voices = _voice_rows(plugin)
    pending, orphans, _catalog = _plan_sync(plugin)
    ports = _port_roles(plugin)

    bound: dict[str, list[str]] = {}
    bound_rows: dict[str, list[dict]] = {}
    for item in voices:
        pid = str(item.get("provider_id") or "").strip().lower()
        if not pid:
            continue
        bound.setdefault(pid, []).append(str(item.get("name") or ""))
        bound_rows.setdefault(pid, []).append(item)

    providers: list[dict] = []
    for row in catalog:
        pid = str(row["id"])
        key = str(row.get("voice_key") or "")
        current = str(row.get("current_voice") or "")
        drift = [
            {"name": str(v.get("name") or ""), "voice": str(v.get("voice") or "")}
            for v in bound_rows.get(pid.lower(), [])
            if str(v.get("voice") or "").strip()
            and str(v.get("voice") or "").strip() != current
        ]
        providers.append(
            {
                "id": pid,
                "name": row.get("name") or pid,
                "type": row.get("type") or "",
                "model": row.get("model") or "",
                "local": bool(row.get("local")),
                "voice_key": key,
                "voice": str(row.get("voice") or ""),
                "current_voice": current,
                "can_push": bool(key),
                "bound": bound.get(pid.lower(), []),
                "drift": drift,
                "state": "ready",
            }
        )

    # 主配置里有、但没加载出实例的：被禁用 or 初始化失败，也要让人看见
    loaded_ids = {str(row["id"]).lower() for row in catalog}
    for pid, enabled in enable_map.items():
        if pid in loaded_ids:
            continue
        entry = next(
            (
                e
                for e in _tts_provider_rows(plugin)
                if str(e.get("id") or "").strip().lower() == pid
            ),
            None,
        )
        entry = entry or {}
        key, value = _entry_voice_key(entry)
        real_id = str(entry.get("id") or pid)
        providers.append(
            {
                "id": real_id,
                "name": real_id,
                "type": str(entry.get("type") or ""),
                "model": str(entry.get("model") or ""),
                "local": "gsv" in str(entry.get("type") or "").lower(),
                "voice_key": key,
                "voice": value,
                "current_voice": "",
                "can_push": bool(key),
                "bound": bound.get(pid, []),
                "drift": [],
                "state": "disabled" if not enabled else "error",
            }
        )

    providers.sort(key=lambda p: (p["state"] != "ready", str(p["id"]).lower()))

    ids = sorted({str(p["id"]).lower() for p in providers})
    cache = getattr(plugin, "_tts_scan_cache", None) or {}
    prev = set(cache.get("ids") or [])
    previous = set(cache.get("by_id") or {})
    added = [p["id"] for p in providers if str(p["id"]).lower() not in prev]
    removed = sorted(previous - set(ids))
    try:
        plugin._tts_scan_cache = {
            "at": time.time(),
            "ids": ids,
            "by_id": {str(p["id"]).lower(): str(p["id"]) for p in providers},
        }
    except Exception:  # pragma: no cover - 只影响差异提示
        pass

    return {
        "ok": True,
        "at": int(time.time()),
        "first_scan": not cache,
        "added": added,
        "removed": removed,
        "providers": providers,
        "pending": pending,
        "orphans": orphans,
        "ports": ports,
        "summary": {
            "providers": len(providers),
            "ready": sum(1 for p in providers if p["state"] == "ready"),
            "disabled": sum(1 for p in providers if p["state"] == "disabled"),
            "error": sum(1 for p in providers if p["state"] == "error"),
            "in_config": len(enable_map),
            "voices": len(voices),
            "bound_providers": len(bound),
            "pending": len(pending),
            "orphans": len(orphans),
            "drift": sum(len(p.get("drift") or []) for p in providers),
            "ports": len(ports),
            "ports_online": sum(1 for p in ports if p.get("online")),
        },
    }


def _merge_voices(base_rows: list[dict], pending: list[dict]) -> tuple[list[dict], int]:
    """把待加载的供应商声线追加进去，返回 (新列表, 追加条数)。"""
    rows = [dict(item) for item in base_rows]
    used = {str(item.get("name") or "").strip().lower() for item in rows}
    added = 0
    for item in pending:
        rows.append(
            {
                "__template_key": "voice",
                "name": _unique_name(
                    item.get("label") or item.get("provider_id"), used
                ),
                "provider_id": item["provider_id"],
                "voice": item.get("voice") or "",
                "alias": "",
                "note": item.get("note") or "",
                "enabled": True,
            }
        )
        added += 1
    return rows, added


def _persist(plugin: Any, data: dict, path: Path) -> None:
    _write_file(path, data)
    if isinstance(plugin.config, dict):
        plugin.config.clear()
        plugin.config.update(data)


def auto_sync(plugin: Any) -> int:
    """启动时按供应商把缺的声线补上，返回补了几条。开关在配置 voices_auto_load。"""
    if not _as_bool(plugin.config.get("voices_auto_load", True), True):
        return 0
    try:
        pending, _orphans, _catalog = _plan_sync(plugin)
    except Exception as exc:
        logger.debug(f"[tts_switcher] 读取供应商声线失败: {exc}")
        return 0
    if not pending:
        return 0
    path = _config_path(plugin)
    if path is None:
        return 0
    data = _read_file(path)
    if not data:
        logger.warning("[tts_switcher] 配置文件读不出来，跳过自动加载")
        return 0
    base = data.get("voices")
    if not isinstance(base, list):
        base = list(plugin.config.get("voices") or [])
    rows, added = _merge_voices([v for v in base if isinstance(v, dict)], pending)
    data["voices"] = rows
    try:
        _persist(plugin, data, path)
    except Exception as exc:
        logger.warning(f"[tts_switcher] 自动加载声线写配置失败: {exc}")
        return 0
    logger.info("[tts_switcher] 已从模型供应商自动加载 %d 条声线", added)
    return added


def _settings_snapshot(plugin: Any) -> dict:
    data = plugin.config if isinstance(plugin.config, dict) else {}
    out: dict[str, Any] = {}
    for key in BOOL_SETTINGS:
        out[key] = _as_bool(_get_path(data, key, False), False)
    for key in INT_SETTINGS:
        out[key] = _as_int(_get_path(data, key, 0), 0)
    for key in STR_SETTINGS:
        out[key] = str(_get_path(data, key, "") or "")
    return out


def _providers_snapshot(plugin: Any) -> list[dict]:
    rows: list[dict] = []
    try:
        providers = plugin._providers()
    except Exception:
        providers = []
    for provider in providers:
        pid = _provider_id(provider)
        if not pid:
            continue
        rows.append({"id": pid, "name": _provider_name(provider) or pid})
    rows.sort(key=lambda r: r["id"].lower())
    return rows


def _normalize_voices(payload: Any) -> list[dict]:
    if not isinstance(payload, list):
        raise ValueError("voices 必须是数组")
    result: list[dict] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        provider_id = str(item.get("provider_id") or "").strip()
        if not name or not provider_id:
            continue
        key = name.lower()
        if key in seen:
            raise ValueError(f"声线名字重复：{name}")
        seen.add(key)
        aliases = ",".join(_split_alias(item.get("alias")))
        result.append(
            {
                "__template_key": "voice",
                "name": name,
                "provider_id": provider_id,
                "voice": str(item.get("voice") or "").strip(),
                "alias": aliases,
                "note": str(item.get("note") or "").strip(),
                "enabled": _as_bool(item.get("enabled", True), True),
            }
        )
    return result


# ------------------------------------------------------------ 会话声线（面板里
# 等价执行 /tts <名字>、/tts=全局 <名字>：默认声线、单个会话、全部会话）

TTS_PREF_KEY = "provider_perf_text_to_speech"
SESSION_CFG_KEY = "session_service_config"
CURR_TTS_KEY = "curr_provider_tts"

_KIND_LABEL = {
    "FriendMessage": "私聊",
    "GroupMessage": "群聊",
    "GuildMessage": "频道",
    "TempMessage": "临时会话",
    "OtherMessage": "其他",
}


def _tts_type() -> Any:
    """核心的 ProviderType.TEXT_TO_SPEECH（拿不到就返回 None，走偏好兜底）。"""
    for path in ("astrbot.api.provider", "astrbot.core.provider.entities"):
        try:
            module = __import__(path, fromlist=["ProviderType"])
            return module.ProviderType.TEXT_TO_SPEECH
        except Exception:
            continue
    return None


def _prefs(plugin: Any) -> Any:
    """核心的 shared_preferences 实例。"""
    try:
        from astrbot.core import sp as _sp

        if _sp is not None:
            return _sp
    except Exception:
        pass
    return getattr(getattr(plugin, "context", None), "shared_preferences", None)


def _unwrap_pref(value: Any) -> str:
    """偏好表里存的是 {"val": "..."} 这种结构，拆出真正的值。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return value.strip()
    if isinstance(value, dict):
        return str(value.get("val") or "").strip()
    return str(value or "").strip()


def _parse_umo(umo: str) -> dict:
    parts = str(umo or "").split(":")
    kind = parts[1] if len(parts) > 1 else ""
    return {
        "platform": parts[0] if parts else "",
        "kind": kind,
        "kind_label": _KIND_LABEL.get(kind, kind or "会话"),
        "session_id": ":".join(parts[2:]) if len(parts) > 2 else "",
    }


async def _db_select(plugin: Any, sql: str, params: dict | None = None) -> list[dict]:
    """跑一条只读 SQL；数据库不可用就返回空列表（面板不至于整页挂掉）。"""
    getter = getattr(getattr(plugin, "context", None), "get_db", None)
    if not callable(getter):
        return []
    try:
        db = getter()
    except Exception:
        return []
    if db is None:
        return []
    try:
        from sqlalchemy import text as sql_text
    except Exception:
        return []
    try:
        async with db.get_db() as session:
            result = await session.execute(sql_text(sql), params or {})
            return [dict(row) for row in result.mappings().all()]
    except Exception as exc:
        logger.debug("[tts_switcher] 查询会话数据失败: %s", exc)
        return []


def _voice_label(plugin: Any, provider_id: str) -> str:
    """提供商 ID → 声线名字（没映射过就用 ID 本身）。"""
    pid = str(provider_id or "").strip()
    if not pid:
        return ""
    low = pid.lower()
    for row in _voice_rows(plugin):
        if row["provider_id"].lower() == low:
            return row["name"] or pid
    return pid


def _resolve_voice(plugin: Any, text: str) -> dict | None:
    """按 名字 / 提供商 ID / 别名 / 唯一模糊 找一条声线。"""
    key = str(text or "").strip()
    if not key:
        return None
    low = key.lower()
    rows = _voice_rows(plugin)
    for row in rows:
        if row["name"].lower() == low or row["provider_id"].lower() == low:
            return row
    for row in rows:
        aliases = [a.strip().lower() for a in re.split(r"[,，、;；|]", row["alias"]) if a.strip()]
        if low in aliases:
            return row
    hits = [row for row in rows if low in row["name"].lower()]
    return hits[0] if len(hits) == 1 else None


def _global_tts(plugin: Any) -> dict:
    """主配置里的默认声线（面板「AI → 语音回复 → 默认文本转语音模型」那一项）。"""
    cfg = None
    getter = getattr(getattr(plugin, "context", None), "get_config", None)
    if callable(getter):
        try:
            cfg = getter()
        except Exception:
            cfg = None
    section = {}
    if isinstance(cfg, dict):
        raw = cfg.get("provider_tts_settings")
        section = raw if isinstance(raw, dict) else {}
    pid = str(section.get("provider_id") or "").strip()
    return {
        "provider_id": pid,
        "name": _voice_label(plugin, pid) if pid else "",
        "enable": _as_bool(section.get("enable", True), True),
    }


async def _session_catalog(plugin: Any) -> dict:
    """把「有记录的会话」整理成一张表：名字、平台、当前声线、来源。"""
    override_rows = await _db_select(
        plugin,
        "select scope_id, value, updated_at from preferences "
        "where key = :k and scope = 'umo'",
        {"k": TTS_PREF_KEY},
    )
    alias_rows = await _db_select(
        plugin, "select umo, auto_name, user_alias from umo_aliases"
    )
    conv_rows = await _db_select(
        plugin,
        "select user_id, max(updated_at) as last_active from conversations "
        "group by user_id",
    )
    sel_rows = await _db_select(
        plugin,
        "select scope_id, updated_at from preferences where key = 'sel_conv_id'",
    )

    table: dict[str, dict] = {}

    def touch(umo: str) -> dict | None:
        umo = str(umo or "").strip()
        if not umo or ":" not in umo:
            return None
        row = table.get(umo)
        if row is None:
            row = {"umo": umo, "name": "", "voice": "", "last_active": ""}
            row.update(_parse_umo(umo))
            table[umo] = row
        return row

    for item in override_rows:
        row = touch(item.get("scope_id") or "")
        if row is not None:
            row["voice"] = _unwrap_pref(item.get("value"))
            row["override_at"] = str(item.get("updated_at") or "")

    for item in alias_rows:
        row = touch(item.get("umo") or "")
        if row is not None:
            row["name"] = str(
                item.get("user_alias") or item.get("auto_name") or ""
            ).strip()

    for item in conv_rows:
        row = touch(item.get("user_id") or "")
        if row is not None and item.get("last_active"):
            row["last_active"] = str(item["last_active"])

    for item in sel_rows:
        row = touch(item.get("scope_id") or "")
        if row is not None and item.get("updated_at"):
            stamp = str(item["updated_at"])
            if stamp > row["last_active"]:
                row["last_active"] = stamp

    sessions = []
    for row in table.values():
        voice = row.get("voice") or ""
        row["voice_name"] = _voice_label(plugin, voice) if voice else ""
        row["source"] = "session" if voice else "global"
        row["display"] = row["name"] or f"{row['kind_label']} {row['session_id']}"
        sessions.append(row)

    sessions.sort(
        key=lambda r: (
            0 if r["source"] == "session" else 1,
            r["last_active"] or "",
            r["display"],
        ),
        reverse=False,
    )
    # 最近活跃的排前面：先按「有没有单独设置」分组，组内按时间倒序
    sessions.sort(key=lambda r: r["last_active"] or "", reverse=True)
    sessions.sort(key=lambda r: 0 if r["source"] == "session" else 1)

    return {
        "global": _global_tts(plugin),
        "sessions": sessions,
        "stats": {
            "total": len(sessions),
            "overridden": sum(1 for r in sessions if r["source"] == "session"),
            "following": sum(1 for r in sessions if r["source"] == "global"),
        },
    }


async def _set_global_voice(plugin: Any, provider_id: str) -> tuple[bool, str]:
    """等价 /tts=全局：写主配置 + 同步内存态 + 记全局偏好。"""
    cfg = None
    getter = getattr(getattr(plugin, "context", None), "get_config", None)
    if callable(getter):
        try:
            cfg = getter()
        except Exception:
            cfg = None
    if not isinstance(cfg, dict):
        return False, "拿不到主配置，改不了默认声线"

    try:
        section = cfg.get("provider_tts_settings")
        if not isinstance(section, dict):
            section = {}
            cfg["provider_tts_settings"] = section
        section["provider_id"] = provider_id
        section["enable"] = True
        saver = getattr(cfg, "save_config_async", None)
        if callable(saver):
            await saver()
        else:
            saver = getattr(cfg, "save_config", None)
            if callable(saver):
                saver()
    except Exception as exc:
        logger.error("[tts_switcher] 面板写全局声线失败: %s", exc, exc_info=True)
        return False, f"写配置失败：{exc}"

    manager = getattr(plugin.context, "provider_manager", None)
    if manager is not None:
        try:
            instance = await manager.get_provider_by_id(provider_id)
            if instance is not None:
                manager.curr_tts_provider_inst = instance
            settings = getattr(manager, "provider_tts_settings", None)
            if isinstance(settings, dict):
                settings["provider_id"] = provider_id
                settings["enable"] = True
        except Exception as exc:
            logger.debug("[tts_switcher] 同步默认声线到内存失败: %s", exc)

    prefs = _prefs(plugin)
    if prefs is not None:
        try:
            await prefs.put_async(
                key=CURR_TTS_KEY, value=provider_id, scope="global", scope_id="global"
            )
        except Exception as exc:
            logger.debug("[tts_switcher] 写全局偏好失败: %s", exc)
    return True, ""


async def _set_session_voice(plugin: Any, umo: str, provider_id: str) -> tuple[bool, str]:
    """等价 /tts <名字>：只影响这个会话。"""
    manager = getattr(plugin.context, "provider_manager", None)
    provider_type = _tts_type()
    done = False
    if manager is not None and provider_type is not None:
        try:
            await manager.set_provider(
                provider_id=provider_id, provider_type=provider_type, umo=umo
            )
            done = True
        except Exception as exc:
            logger.error("[tts_switcher] 面板设置会话声线失败: %s", exc, exc_info=True)
            return False, f"写入失败：{exc}"
    if not done:
        prefs = _prefs(plugin)
        if prefs is None:
            return False, "核心没有 shared_preferences，改不了会话声线"
        try:
            await prefs.session_put(umo, TTS_PREF_KEY, provider_id)
        except Exception as exc:
            return False, f"写入失败：{exc}"

    # 和 /tts 一样顺手把会话语音开关打开，免得切了没声音
    if _as_bool(plugin.config.get("access.auto_enable", True), True):
        prefs = _prefs(plugin)
        if prefs is not None:
            try:
                conf = await prefs.session_get(umo, SESSION_CFG_KEY, {}) or {}
                if not isinstance(conf, dict):
                    conf = {}
                conf["tts_enabled"] = True
                await prefs.session_put(umo, SESSION_CFG_KEY, conf)
            except Exception as exc:
                logger.debug("[tts_switcher] 打开会话语音失败: %s", exc)
    return True, ""


async def _clear_session_voice(plugin: Any, umo: str) -> tuple[bool, str]:
    """等价 /tts 默认：清掉会话覆盖，回到默认声线。"""
    prefs = _prefs(plugin)
    if prefs is None:
        return False, "核心没有 shared_preferences，清不了会话声线"
    try:
        await prefs.session_remove(umo, TTS_PREF_KEY)
    except Exception as exc:
        return False, f"清除失败：{exc}"
    return True, ""


def register_web_apis(plugin: Any) -> None:
    """把面板要用的接口挂到核心上。"""
    context = plugin.context

    def _state() -> dict:
        pending, orphans, catalog = _plan_sync(plugin)
        enabled_map = _provider_enable_map(plugin)
        # 每条声线标注其供应商的真实状态，别让「供应商已禁用」看起来还能用
        voices = _voice_rows(plugin)
        stats = _annotate_suppliers(voices, catalog, enabled_map)

        return {
            "ok": True,
            "voices": voices,
            "supplier_stats": stats,
            "provider_enable": enabled_map,
            "ports": _port_roles(plugin),
            "gsv_lazy_weights": _gsv_lazy_on(plugin),
            "settings": _settings_snapshot(plugin),
            "providers": _providers_snapshot(plugin),
            "catalog": catalog,
            "pending": pending,
            "orphans": orphans,
            "config_path": str(_config_path(plugin) or ""),
        }

    async def api_state():
        return jsonify(_state())

    async def api_save():
        payload = await _read_json()
        path = _config_path(plugin)
        if path is None:
            return jsonify({"ok": False, "error": "找不到插件配置文件"}), 400

        data = _read_file(path)
        try:
            if "voices" in payload:
                data["voices"] = _normalize_voices(payload.get("voices"))
            settings = payload.get("settings")
            if isinstance(settings, dict):
                for key in BOOL_SETTINGS:
                    if key in settings:
                        _set_path(data, key, _as_bool(settings[key], False))
                for key in INT_SETTINGS:
                    if key in settings:
                        _set_path(data, key, _as_int(settings[key], 0))
                for key in STR_SETTINGS:
                    if key in settings:
                        _set_path(data, key, str(settings[key] or "").strip())
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        try:
            _persist(plugin, data, path)
        except Exception as exc:
            logger.error("[tts_switcher] 写配置失败: %s", exc)
            return jsonify({"ok": False, "error": f"写配置失败：{exc}"}), 500

        logger.info(
            "[tts_switcher] 面板已保存：%d 条声线 -> %s",
            len(data.get("voices") or []),
            path,
        )
        state = _state()
        state["mode"] = "save"
        return jsonify(state)

    async def api_reload():
        """从磁盘重新读一遍配置（面板被外部改过时用）。"""
        path = _config_path(plugin)
        if path is None:
            return jsonify({"ok": False, "error": "找不到插件配置文件"}), 400
        data = _read_file(path)
        if isinstance(plugin.config, dict) and data:
            plugin.config.clear()
            plugin.config.update(data)
        state = _state()
        state["mode"] = "reload"
        return jsonify(state)

    async def api_sync():
        """把模型供应商里的声线同步进来；顺带支持把断掉的绑定改到新 provider。"""
        payload = await _read_json()
        path = _config_path(plugin)
        if path is None:
            return jsonify({"ok": False, "error": "找不到插件配置文件"}), 400

        data = _read_file(path)
        if not data:
            return jsonify({"ok": False, "error": "配置文件读不出来"}), 400
        rows = data.get("voices")
        if not isinstance(rows, list):
            rows = list(plugin.config.get("voices") or [])
        rows = [v for v in rows if isinstance(v, dict)]

        added = 0
        if _as_bool(payload.get("load_all"), True):
            pending, _orphans, _catalog = _plan_sync(plugin)
            rows, added = _merge_voices(rows, pending)

        remapped = 0
        remap = payload.get("remap")
        if isinstance(remap, list):
            for item in remap:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                new_pid = str(item.get("provider_id") or "").strip()
                if not name or not new_pid:
                    continue
                for row in rows:
                    if str(row.get("name") or "") == name:
                        row["provider_id"] = new_pid
                        remapped += 1
                        break

        data["voices"] = rows
        try:
            _persist(plugin, data, path)
        except Exception as exc:
            logger.error("[tts_switcher] 同步声线写配置失败: %s", exc)
            return jsonify({"ok": False, "error": f"写配置失败：{exc}"}), 500

        if added or remapped:
            logger.info(
                "[tts_switcher] 面板同步：新增 %d 条，改绑 %d 条", added, remapped
            )
        state = _state()
        state["added"] = added
        state["remapped"] = remapped
        return jsonify(state)

    async def api_scan():
        """扫描一遍所有语音供应商：谁在线、什么音色、哪些声线还没接上。"""
        try:
            report = _scan_report(plugin)
        except Exception as exc:
            logger.error("[tts_switcher] 扫描供应商失败: %s", exc)
            return jsonify({"ok": False, "error": f"扫描失败：{exc}"}), 500
        state = _state()
        state.update(report)
        state["mode"] = "scan"
        return jsonify(state)

    async def api_push():
        """把声线上的音色写回供应商配置，并热更新运行中的实例。

        不传 items 时=把所有「声线音色 ≠ 供应商当前音色」的自动同步一遍。
        """
        payload = await _read_json()
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            items = []
            if payload.get("provider_id"):
                items = [
                    {
                        "provider_id": payload.get("provider_id"),
                        "voice": payload.get("voice"),
                        "name": payload.get("name"),
                    }
                ]
        if not items:
            try:
                report = _scan_report(plugin)
            except Exception as exc:
                return jsonify({"ok": False, "error": f"扫描失败：{exc}"}), 500
            for row in report["providers"]:
                for drift in row.get("drift") or []:
                    items.append(
                        {
                            "provider_id": row["id"],
                            "voice": drift.get("voice"),
                            "name": drift.get("name"),
                        }
                    )
        if not items:
            return jsonify({"ok": False, "error": "没有需要同步的音色"}), 400

        getter = getattr(plugin.context, "get_config", None)
        cfg = None
        if callable(getter):
            try:
                cfg = getter()
            except Exception as exc:  # pragma: no cover - 核心接口异常只降级
                logger.warning("[tts_switcher] 读取主配置失败: %s", exc)
        if not isinstance(cfg, dict):
            return jsonify({"ok": False, "error": "读不到主配置"}), 500

        # 改主配置前先留一份，出问题能直接覆盖回去
        backup = ""
        src = str(getattr(cfg, "config_path", "") or "")
        if src and Path(src).exists():
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dst = Path(src).with_name(Path(src).name + f".bak-ttsvoice-{stamp}")
            try:
                shutil.copy2(src, dst)
                backup = str(dst)
            except Exception as exc:  # pragma: no cover
                logger.warning("[tts_switcher] 备份主配置失败: %s", exc)

        results: list[dict] = []
        changed = False
        # 同一个提供商同一时刻只能有一个音色，两条声线写不同值时后写的赢
        merged: dict[str, dict] = {}
        dropped = 0
        shared_count: dict[str, int] = {}
        for v in _voice_rows(plugin):
            owner = str(v.get("provider_id") or "").strip().lower()
            if owner:
                shared_count[owner] = shared_count.get(owner, 0) + 1
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("provider_id") or "").strip().lower()
            if not pid:
                merged[f"__blank_{len(merged)}"] = item
                continue
            if pid in merged:
                dropped += 1
            merged[pid] = item
        for item in merged.values():
            if not isinstance(item, dict):
                continue
            pid = str(item.get("provider_id") or "").strip()
            voice = str(item.get("voice") or "").strip()
            label = str(item.get("name") or pid)
            if not pid or not voice:
                results.append(
                    {
                        "name": label,
                        "provider_id": pid,
                        "ok": False,
                        "error": "缺少提供商或音色",
                    }
                )
                continue
            entry = _find_provider_entry(cfg, pid)
            if entry is None:
                results.append(
                    {
                        "name": label,
                        "provider_id": pid,
                        "ok": False,
                        "error": "主配置里没有这个提供商",
                    }
                )
                continue
            key, before = _entry_voice_key(entry)
            if not key:
                results.append(
                    {
                        "name": label,
                        "provider_id": pid,
                        "ok": False,
                        "error": "这个提供商没有可写的音色字段（本地权重类请到它自己的配置里改）",
                    }
                )
                continue
            provider = _provider_instance(plugin, pid)
            applied = False
            if provider is not None:
                for attr in VOICE_ATTRS:
                    if not hasattr(provider, attr):
                        continue
                    try:
                        setattr(provider, attr, voice)
                        applied = True
                        break
                    except Exception:  # pragma: no cover
                        continue
            entry[key] = voice
            changed = True
            results.append(
                {
                    "name": label,
                    "provider_id": pid,
                    "key": key,
                    "before": before,
                    "after": voice,
                    "ok": True,
                    "applied": applied,
                    "shared": shared_count.get(pid.lower(), 0),
                }
            )

        if not changed:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "没有一条写成功，看每条的说明",
                        "pushed": results,
                    }
                ),
                400,
            )

        try:
            saver = getattr(cfg, "save_config_async", None)
            if callable(saver):
                await saver()
            else:  # pragma: no cover - 老版本核心只有同步版本
                cfg.save_config()
        except Exception as exc:
            logger.error("[tts_switcher] 写主配置失败: %s", exc)
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"写主配置失败：{exc}",
                        "pushed": results,
                        "backup": backup,
                    }
                ),
                500,
            )

        logger.info(
            "[tts_switcher] 已把 %d 条声线的音色同步到供应商（备份：%s）",
            len([r for r in results if r.get("ok")]),
            backup or "无",
        )
        state = _state()
        state["mode"] = "push"
        state["pushed"] = results
        state["backup"] = backup
        state["merged_off"] = dropped
        try:  # 顺手带一份新的扫描摘要，前端卡片能立刻显示「待同步 0 条」
            state.update(_scan_report(plugin))
        except Exception:  # pragma: no cover
            pass
        return jsonify(state)

    async def api_pull():
        """反向同步：把供应商当前音色写回声线里的 voice 字段。"""
        payload = await _read_json()
        only = str(payload.get("provider_id") or "").strip().lower()
        path = _config_path(plugin)
        if path is None:
            return jsonify({"ok": False, "error": "找不到插件配置文件"}), 400
        data = _read_file(path)
        if not data:
            return jsonify({"ok": False, "error": "配置文件读不出来"}), 400
        rows = data.get("voices")
        if not isinstance(rows, list):
            rows = list(plugin.config.get("voices") or [])
        rows = [v for v in rows if isinstance(v, dict)]

        current: dict[str, str] = {}
        for row in _catalog_snapshot(plugin):
            value = str(row.get("current_voice") or "").strip()
            if value:
                current[str(row.get("id") or "").lower()] = value

        pulled: list[dict] = []
        for item in rows:
            pid = str(item.get("provider_id") or "").strip().lower()
            if only and pid != only:
                continue
            value = current.get(pid)
            if not value:
                continue
            before = str(item.get("voice") or "").strip()
            if before == value:
                continue
            item["voice"] = value
            pulled.append(
                {
                    "name": str(item.get("name") or ""),
                    "provider_id": str(item.get("provider_id") or ""),
                    "before": before,
                    "after": value,
                }
            )

        if pulled:
            data["voices"] = rows
            try:
                _persist(plugin, data, path)
            except Exception as exc:
                logger.error("[tts_switcher] 拉取音色写配置失败: %s", exc)
                return jsonify({"ok": False, "error": f"写配置失败：{exc}"}), 500
            logger.info("[tts_switcher] 已从供应商回填 %d 条声线的音色", len(pulled))

        state = _state()
        state["mode"] = "pull"
        state["pulled"] = pulled
        try:
            state.update(_scan_report(plugin))
        except Exception:  # pragma: no cover
            pass
        return jsonify(state)

    async def api_sessions():
        """会话声线总览：默认声线 + 每个会话现在用的是哪条、是不是单独指定过。"""
        try:
            data = await _session_catalog(plugin)
        except Exception as exc:
            logger.error("[tts_switcher] 读取会话声线失败: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": f"读取失败：{exc}"}), 500
        data["ok"] = True
        return jsonify(data)

    async def api_scope():
        """面板里执行 /tts 与 /tts=全局：

        target = global  改默认声线（所有没有单独设置的会话都跟着变）
        target = session 只改某个会话
        target = all     全部会话都用它（改默认 + 清空所有会话覆盖）
        clear = true     清掉会话覆盖，回到默认（session / all 可用）
        """
        payload = await _read_json()
        target = str(payload.get("target") or "session").strip().lower()
        clear = _as_bool(payload.get("clear", False), False)

        voice = None
        if not clear:
            voice = _resolve_voice(plugin, str(payload.get("voice") or ""))
            if voice is None:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": "没找到这条声线：名字、提供商 ID、别名都对不上"
                            f"（填的是「{payload.get('voice')}」）",
                        }
                    ),
                    400,
                )
            if not clear:
                # 声线绑定的提供商可能已被删掉，先挡住，别写出一个哑巴设置
                if plugin._resolve_provider(voice["provider_id"]) is None:
                    return (
                        jsonify(
                            {
                                "ok": False,
                                "error": f"声线「{voice['name']}」的提供商"
                                f"「{voice['provider_id']}」现在没在跑，先把它启用再切",
                            }
                        ),
                        400,
                    )

        provider_id = voice["provider_id"] if voice else ""
        applied: list[dict] = []
        failed: list[dict] = []
        cleared = 0

        if target == "global":
            ok, err = await _set_global_voice(plugin, provider_id)
            if not ok:
                return jsonify({"ok": False, "error": err}), 500
            applied.append({"scope": "global", "provider_id": provider_id})

        elif target == "session":
            umo = str(payload.get("umo") or "").strip()
            if not umo:
                return jsonify({"ok": False, "error": "缺少会话标识"}), 400
            if clear:
                ok, err = await _clear_session_voice(plugin, umo)
                if ok:
                    cleared += 1
                    applied.append({"scope": "session", "umo": umo, "cleared": True})
                else:
                    failed.append({"umo": umo, "error": err})
            else:
                ok, err = await _set_session_voice(plugin, umo, provider_id)
                if ok:
                    applied.append(
                        {"scope": "session", "umo": umo, "provider_id": provider_id}
                    )
                else:
                    failed.append({"umo": umo, "error": err})

        elif target == "all":
            if not clear:
                ok, err = await _set_global_voice(plugin, provider_id)
                if not ok:
                    return jsonify({"ok": False, "error": err}), 500
                applied.append({"scope": "global", "provider_id": provider_id})
            catalog = await _session_catalog(plugin)
            for row in catalog["sessions"]:
                if row["source"] != "session":
                    continue
                ok, err = await _clear_session_voice(plugin, row["umo"])
                if ok:
                    cleared += 1
                else:
                    failed.append({"umo": row["umo"], "error": err})
            applied.append({"scope": "all_sessions", "cleared": cleared})

        else:
            return jsonify({"ok": False, "error": f"不认识的目标：{target}"}), 400

        logger.info(
            "[tts_switcher] 面板执行声线切换 target=%s voice=%s cleared=%d failed=%d",
            target,
            provider_id or "(清除)",
            cleared,
            len(failed),
        )

        state = await _session_catalog(plugin)
        state.update(
            {
                # 部分失败也把最新状态回给页面，失败明细走 failed / error
                "ok": True,
                "target": target,
                "applied": applied,
                "failed": failed,
                "cleared": cleared,
            }
        )
        if failed:
            state["error"] = f"{len(failed)} 个会话没改成"
        return jsonify(state)

    async def api_preview():
        """试听：用指定声线合成一句话，返回 wav 的 base64 data URI。"""
        payload = await _read_json()
        name = str(payload.get("name") or "").strip()
        text = str(payload.get("text") or "你好，这是试听").strip()
        if not name:
            return jsonify({"ok": False, "error": "缺少声线名"}), 400
        voices = plugin._voices()
        voice = next((v for v in voices if v["name"] == name), None)
        if voice is None:
            return jsonify({"ok": False, "error": f"没有声线 {name}"}), 404
        provider = plugin._resolve_provider(voice["provider_id"])
        if provider is None:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"提供商没在跑：{voice['provider_id']}"
                        "（本地语音合成需要先手动启动服务）",
                    }
                ),
                400,
            )
        try:
            import asyncio
            import base64
            import mimetypes

            audio = await asyncio.wait_for(provider.get_audio(text), timeout=90)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"合成失败：{exc}"}), 500
        if not audio:
            return jsonify({"ok": False, "error": "合成返回空音频"}), 500
        try:
            raw = Path(str(audio)).read_bytes()
        except Exception as exc:
            return jsonify({"ok": False, "error": f"读音频失败：{exc}"}), 500
        mime = mimetypes.guess_type(str(audio))[0] or "audio/wav"
        encoded = base64.b64encode(raw).decode()
        return jsonify({"ok": True, "audio": f"data:{mime};base64,{encoded}"})

    routes = (
        ("panel/state", api_state, ["GET"], "面板：读声线、供应商声线与设置"),
        ("panel/save", api_save, ["POST"], "面板：保存声线与设置"),
        ("panel/reload", api_reload, ["POST"], "面板：从磁盘重载配置"),
        ("panel/sync", api_sync, ["POST"], "面板：从模型供应商同步声线"),
        ("panel/scan", api_scan, ["POST"], "面板：扫描供应商与当前音色"),
        ("panel/push", api_push, ["POST"], "面板：把声线音色写回供应商"),
        ("panel/pull", api_pull, ["POST"], "面板：从供应商回填声线音色"),
        ("panel/sessions", api_sessions, ["GET"], "面板：列出会话与各自当前声线"),
        ("panel/scope", api_scope, ["POST"], "面板：设置默认/单个/全部会话的声线"),
        ("panel/preview", api_preview, ["POST"], "面板：试听一条声线"),
    )
    for route, handler, methods, desc in routes:
        context.register_web_api(f"/{PLUGIN_NAME}/{route}", handler, methods, desc)
    logger.info("[tts_switcher] 已注册 WebUI 面板接口 %d 条", len(routes))
