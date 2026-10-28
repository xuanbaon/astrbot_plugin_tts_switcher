# -*- coding: utf-8 -*-
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
from pathlib import Path
from typing import Any

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
                    voices.append({"key": key, "value": item, "label": _short_voice(item)})
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


def _plan_sync(plugin: Any) -> tuple[list[dict], list[dict], list[dict]]:
    """算出「供应商里有、声线列表里还没有」和「声线列表里有、供应商已经没了」。"""
    catalog = _catalog_snapshot(plugin)
    raw = plugin.config.get("voices")
    voices = [v for v in raw if isinstance(v, dict)] if isinstance(raw, list) else []

    covered: dict[str, set[str]] = {}
    for item in voices:
        pid = str(item.get("provider_id") or "").strip().lower()
        if not pid:
            continue
        covered.setdefault(pid, set()).add(str(item.get("voice") or "").strip().lower())

    known_ids = {row["id"].lower() for row in catalog}
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


def _merge_voices(base_rows: list[dict], pending: list[dict]) -> tuple[list[dict], int]:
    """把待加载的供应商声线追加进去，返回 (新列表, 追加条数)。"""
    rows = [dict(item) for item in base_rows]
    used = {str(item.get("name") or "").strip().lower() for item in rows}
    added = 0
    for item in pending:
        rows.append(
            {
                "__template_key": "voice",
                "name": _unique_name(item.get("label") or item.get("provider_id"), used),
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


def register_web_apis(plugin: Any) -> None:
    """把面板要用的接口挂到核心上。"""
    context = plugin.context

    def _state() -> dict:
        pending, orphans, catalog = _plan_sync(plugin)
        return {
            "ok": True,
            "voices": _voice_rows(plugin),
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
        ("panel/preview", api_preview, ["POST"], "面板：试听一条声线"),
    )
    for route, handler, methods, desc in routes:
        context.register_web_api(f"/{PLUGIN_NAME}/{route}", handler, methods, desc)
    logger.info("[tts_switcher] 已注册 WebUI 面板接口 %d 条", len(routes))
