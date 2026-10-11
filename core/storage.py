# BiliSubscribe - 订阅 B站 UP 主的视频与动态更新，推送到群聊
# Copyright (C) 2026  Dowond (bilibili-Subscribe)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""订阅数据持久化：优先 GsCore 数据库，退化为本地 JSON。

两个后端对外是同一套 API（全部 async，内部按后端分派）：

    load_subs()      → 快照 {uid: {name, last_dynamic, last_video, pushed_av, groups:{gid:{...}}}}
    add_sub / remove_sub / clear_group / merge_state

快照相关的纯函数不碰后端：``uids_in_group`` / ``group_entry`` / ``remember_av`` / ``parse_uid_arg``。

后端选择：
- 能同时导入 ``sqlmodel`` 和 ``gsuid_core.utils.database.base_models`` → 数据库；
- 否则（核心版本较老、或离线单测）→ ``data/BiliSubscribe/subscribe.json``。

首次用数据库时会做两件事：建表兜底（见 ``models.ensure_table``）、
以及把已有 ``subscribe.json`` 里的订阅一次性导进去（表里非空则不导，不覆盖）。
"""
import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional

from .config import SUBS_PATH

MAX_PUSHED_AV = 30


def _log(msg: str, level: str = "warning") -> None:
    try:
        from gsuid_core.logger import logger

        getattr(logger, level, logger.info)(msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 后端选择
# ---------------------------------------------------------------------------
BACKEND = "json"
BiliSubscribeSub = None  # type: ignore
_ensure_table = None  # type: ignore

try:
    from .models import BiliSubscribeSub as _SubModel
    from .models import ensure_table as _ensure

    BiliSubscribeSub = _SubModel
    _ensure_table = _ensure
    BACKEND = "database"
except Exception as e:  # sqlmodel 缺失 / 核心没有数据库层
    _log(f"BiliSubscribe：数据库后端不可用，订阅数据落本地 JSON（{e}）", "info")

_json_lock = asyncio.Lock()
_init_lock = asyncio.Lock()
# 上次写库失败、兜底写进了本地 JSON，还没补写回数据库（读的时候要把 JSON 并进来）
_json_pending = False


def db_available() -> bool:
    """数据库后端是否可用（能 import 到模型和核心的数据库层）。

    **不代表表已经建好了** —— 建表那一步失败不该影响读写：直接去问数据库，
    连不上再退本地 JSON（就是为了修"库里明明有数据、列表却说本群没有订阅"）。
    """
    return BACKEND == "database"


def _mark_json_pending() -> None:
    global _json_pending
    _json_pending = True
_table_state = {"ready": False, "tried_at": 0.0, "imported": False}
_TABLE_RETRY_COOLDOWN = 60.0


# ---------------------------------------------------------------------------
# JSON 后端
# ---------------------------------------------------------------------------
def _json_load() -> Dict[str, Dict]:
    if not SUBS_PATH.exists():
        return {}
    try:
        with open(SUBS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict] = {}
    for uid, info in raw.items():
        if not isinstance(info, dict):
            continue
        groups = info.get("groups")
        raw_status = info.get("live_status")
        try:
            live_status: Optional[int] = None if raw_status in (None, "") else int(raw_status)
        except (TypeError, ValueError):
            live_status = None
        out[str(uid)] = {
            "name": str(info.get("name") or ""),
            "last_dynamic": str(info.get("last_dynamic") or ""),
            "last_video": str(info.get("last_video") or ""),
            "pushed_av": [str(b) for b in (info.get("pushed_av") or []) if str(b)],
            "room_id": str(info.get("room_id") or ""),
            "live_status": live_status,
            "groups": groups if isinstance(groups, dict) else {},
        }
    return out


def _json_save(subs: Dict[str, Dict]) -> None:
    tmp = SUBS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(subs, f, ensure_ascii=False, indent=2)
    tmp.replace(SUBS_PATH)


# ---------------------------------------------------------------------------
# 数据库后端
# ---------------------------------------------------------------------------
# 每条订阅自己的推送开关 / 分类 @全体（存哪一行都带着）
FLAG_KEYS = ("push_dynamic", "push_video", "push_live")
AT_KEYS = ("at_all_dynamic", "at_all_video", "at_all_live")


def _flag(raw: Dict, key: str, default: bool) -> bool:
    """读一个布尔开关：缺省 / None / 空串都按 ``default`` 算。

    这次加开关最怕的就是"老数据没有新键" —— 数据库里老行没有新列、JSON 里老文件
    没有新键，所以读写两边都走这个函数，缺了就给默认值。
    """
    value = raw.get(key, default)
    if value is None or value == "":
        return default
    return bool(value)


def _flag_row(row: Any, key: str, default: bool) -> bool:
    """从数据库行读一个布尔开关（老库可能没有这一列 → 按默认值算）。"""
    if not hasattr(row, key):
        return default
    return _flag({key: getattr(row, key)}, key, default)


def _load_av(row: Any) -> List[str]:
    raw = getattr(row, "pushed_av", None)
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except Exception:
            return []
        if isinstance(data, list):
            return [str(x) for x in data if str(x)]
    return []


def _row_group(row: Any) -> Dict:
    entry = {
        "bot_id": str(getattr(row, "bot_id", "") or ""),
        "bot_self_id": str(getattr(row, "bot_self_id", "") or ""),
        "at_all": _flag_row(row, "at_all", False),
    }
    for key in FLAG_KEYS:
        entry[key] = _flag_row(row, key, True)
    for key in AT_KEYS:
        entry[key] = _flag_row(row, key, False)
    return entry


async def _import_json_once(snapshot: Optional[Dict] = None) -> None:
    """把 subscribe.json 导进数据库（表里已有数据就不动）。

    首次用数据库后端时会调一次（把老 JSON 数据带过来）；``load_subs`` 发现
    "数据库空着、JSON 有数据"时也会调一次补写回去（见那里的注释）。
    """
    if _table_state["imported"] and snapshot is None:
        return
    _table_state["imported"] = True
    snapshot = snapshot if snapshot is not None else _json_load()
    if not snapshot:
        return
    try:
        rows = await _fetch_all_rows()
    except Exception as e:
        _log(f"BiliSubscribe：读取订阅表失败（{e}）")
        return
    if rows:
        return
    count = 0
    for uid, info in snapshot.items():
        for gid, entry in (info.get("groups") or {}).items():
            entry = entry if isinstance(entry, dict) else {}
            await BiliSubscribeSub.full_insert_data(
                uid=str(uid),
                group_id=str(gid),
                bot_id=str(entry.get("bot_id") or ""),
                bot_self_id=str(entry.get("bot_self_id") or ""),
                at_all=bool(entry.get("at_all")),
                name=str(info.get("name") or ""),
                last_dynamic=str(info.get("last_dynamic") or ""),
                last_video=str(info.get("last_video") or ""),
                pushed_av=json.dumps(info.get("pushed_av") or [], ensure_ascii=False),
            )
            count += 1
    if count:
        _log(f"BiliSubscribe：已把 subscribe.json 的 {count} 条订阅导入数据库", "info")


async def _db_ready() -> bool:
    """确认表可用；失败后按冷却时间重试，避免一次抖动就永久退回 JSON。"""
    if BACKEND != "database":
        return False
    if _table_state["ready"]:
        return True
    async with _init_lock:
        if _table_state["ready"]:
            return True
        if _table_state["tried_at"] and time.time() - _table_state["tried_at"] < _TABLE_RETRY_COOLDOWN:
            return False
        _table_state["tried_at"] = time.time()
        ok = bool(await _ensure_table())
        _table_state["ready"] = ok
        if ok:
            await _import_json_once()
        return ok


async def _fetch_all_rows() -> List[Any]:
    """把表里所有行读出来。

    不同核心版本给的方法不一样，**按"实测最稳"的顺序逐个试**：

    1. ``select_rows()``：不带条件就是全表，实测最稳（`bili订阅` 判重、`bili取消订阅`
       走的都是它，能正常工作）；
    2. ``get_all_data()``：文档里的读全表方法，但**有的核心版本会抛异常**
       （线上就是它抛了：`bili列表` 显示"数据库 读不到"，可 `bili订阅` 却能认出已订阅）；
    3. 自己用 ``async_maker`` 开 session 查（文档给的"手动管理 Session"写法）。

    三种都不行才把异常抛出去，由调用方退本地 JSON。
    """
    errors: List[str] = []
    for name in ("select_rows", "get_all_data"):
        method = getattr(BiliSubscribeSub, name, None)
        if not callable(method):
            errors.append(f"{name}: 核心没有这个方法")
            continue
        try:
            rows = await method()
            return list(rows or [])
        except Exception as e:
            errors.append(f"{name}: {e}")
    try:
        from sqlalchemy import select as sa_select

        from gsuid_core.utils.database.base_models import async_maker

        async with async_maker() as session:
            result = await session.execute(sa_select(BiliSubscribeSub))
            return list(result.scalars().all())
    except Exception as e:
        errors.append(f"async_maker: {e}")
    raise RuntimeError("；".join(errors) or "读数据库失败")


async def _db_load() -> Dict[str, Dict]:
    rows = await _fetch_all_rows()
    subs: Dict[str, Dict] = {}
    for row in rows or []:
        uid = str(getattr(row, "uid", "") or "")
        gid = str(getattr(row, "group_id", "") or "")
        if not uid or not gid:
            continue
        if uid not in subs:
            raw_status = getattr(row, "live_status", None)
            try:
                live_status: Optional[int] = None if raw_status in (None, "") else int(raw_status)
            except (TypeError, ValueError):
                live_status = None
            subs[uid] = {
                "name": str(getattr(row, "name", "") or ""),
                "last_dynamic": str(getattr(row, "last_dynamic", "") or ""),
                "last_video": str(getattr(row, "last_video", "") or ""),
                "pushed_av": _load_av(row),
                "room_id": str(getattr(row, "room_id", "") or ""),
                "live_status": live_status,
                "groups": {},
            }
        subs[uid]["groups"][gid] = _row_group(row)
    return subs


async def _db_add(
    uid: str, gid: str, bot_id: str, bot_self_id: str, at_all: bool
) -> bool:
    row = await BiliSubscribeSub.base_select_data(uid=uid, group_id=gid)
    if row is None:
        await BiliSubscribeSub.full_insert_data(
            uid=uid,
            group_id=gid,
            bot_id=bot_id,
            bot_self_id=bot_self_id,
            at_all=bool(at_all),
        )
        return True

    update: Dict[str, Any] = {}
    if at_all and not bool(getattr(row, "at_all", False)):
        update["at_all"] = True
    # 群里的 bot 换了（换适配器/换号）时更新成最新的，推送才发得出去
    if bot_id and str(getattr(row, "bot_id", "") or "") != bot_id:
        update["bot_id"] = bot_id
        update["bot_self_id"] = bot_self_id
    if not update:
        return False
    await BiliSubscribeSub.update_data_by_data(
        select_data={"uid": uid, "group_id": gid}, update_data=update
    )
    return True


async def _db_remove(uid: str, gid: str) -> bool:
    deleted = await BiliSubscribeSub.delete_row(uid=uid, group_id=gid)
    return bool(deleted)


async def _db_clear(gid: str) -> int:
    rows = await BiliSubscribeSub.select_rows(group_id=gid)
    if not rows:
        return 0
    uids = {str(getattr(r, "uid", "") or "") for r in rows}
    uids.discard("")
    await BiliSubscribeSub.delete_row(group_id=gid)
    return len(uids)


async def _db_merge(updates: Dict[str, Dict]) -> None:
    for uid, up in updates.items():
        update: Dict[str, Any] = {}
        for key in ("name", "last_dynamic", "last_video", "room_id"):
            if up.get(key):
                update[key] = str(up[key])
        if up.get("pushed_av") is not None:
            update["pushed_av"] = json.dumps(
                list(up["pushed_av"])[-MAX_PUSHED_AV:], ensure_ascii=False
            )
        if up.get("live_status") is not None:
            try:
                update["live_status"] = int(up["live_status"])
            except (TypeError, ValueError):
                pass
        if not update:
            continue
        # 按 uid 更新：该 UP 所有群的行一起改（游标对所有群是同一份）
        await BiliSubscribeSub.update_data_by_data(
            select_data={"uid": str(uid)}, update_data=update
        )


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------
def _merge_snapshots(db: Dict[str, Dict], snapshot: Dict[str, Dict]) -> Dict[str, Dict]:
    """把兜底写进 JSON 的那些订阅并进数据库快照（数据库里已有的行不动）。

    只在 ``_json_pending`` 为真时用（上一次写库失败过），用来让"刚订阅成功"立刻
    可见；不然就会出现"订阅回复成功、紧接着 bili列表 / 按名字取消说本群没有订阅"。
    """
    out: Dict[str, Dict] = {
        uid: {**info, "groups": dict(info.get("groups") or {})}
        for uid, info in db.items()
    }
    for uid, info in snapshot.items():
        groups = info.get("groups") or {}
        if not groups:
            continue
        node = out.setdefault(
            uid,
            {
                "name": info.get("name") or "",
                "last_dynamic": info.get("last_dynamic") or "",
                "last_video": info.get("last_video") or "",
                "pushed_av": list(info.get("pushed_av") or []),
                "groups": {},
            },
        )
        for gid, entry in groups.items():
            node["groups"].setdefault(str(gid), entry)
    return out


async def _resync_pending(db: Dict[str, Dict], snapshot: Dict[str, Dict]) -> bool:
    """把"写库失败兜底进 JSON"的那些订阅补写回数据库（逐行 upsert，幂等）。"""
    global _json_pending
    if not db_available():
        return False
    todo = [
        (uid, str(gid), entry if isinstance(entry, dict) else {})
        for uid, info in snapshot.items()
        for gid, entry in (info.get("groups") or {}).items()
        if not ((db.get(uid) or {}).get("groups") or {}).get(str(gid))
    ]
    if not todo:
        _json_pending = False
        return True
    for uid, gid, entry in todo:
        try:
            await _db_add(
                str(uid),
                gid,
                str(entry.get("bot_id") or ""),
                str(entry.get("bot_self_id") or ""),
                bool(entry.get("at_all")),
            )
        except Exception as e:
            _log(f"BiliSubscribe：把兜底订阅补写回数据库失败（{e}）")
            return False
    _log(f"BiliSubscribe：已把 {len(todo)} 条兜底订阅补写回数据库", "info")
    _json_pending = False
    return True


async def load_subs() -> Dict[str, Dict]:
    """读一份订阅快照（数据库优先，JSON 兜底）。

    两点讲究：

    1. **不拿"建表成功"标记当读的前提**：以前 `_db_ready()` 为假就直接读 JSON，
       于是建表那一步失败过一次、数据库里明明有数据（控制台能看到），
       列表却还是"本群还没有订阅" —— 现在只要后端是数据库就直接去问数据库，
       连不上才退 JSON；
    2. **写库失败兜底进 JSON 的那些，读这边要能看见**：``_json_pending`` 为真时
       把 JSON 并进数据库快照用（并尝试补写回数据库），免得刚订阅成功就查不到。
       数据库里已有的行永远以数据库为准，已删的订阅不会被 JSON 残留复活。
    """
    if db_available():
        await _db_ready()  # 建表兜底；失败也照样试着读
        db: Dict[str, Dict] = {}
        try:
            db = await _db_load()
        except Exception as e:
            _log(f"BiliSubscribe：读数据库失败，本次回退本地 JSON（{e}）")
        snapshot = _json_load()
        if _json_pending and snapshot:
            merged = _merge_snapshots(db, snapshot)
            await _resync_pending(db, snapshot)
            return merged
        if db:
            return db
        if snapshot:
            _log(
                f"BiliSubscribe：数据库里没有订阅，本地 JSON 有 {len(snapshot)} 个 UP"
                "（多半是上次写库失败兜底写进去的），按 JSON 用并补写回数据库"
            )
            await _import_json_once(snapshot)
            return snapshot
        return {}
    return _json_load()


async def stats() -> Dict[str, Any]:
    """两个后端各有几个 UP 的订阅（``-1`` = 这个后端没启用/读不到）。

    排查"订阅回复说成功、列表却是空的"这类读写错位时，`bili配置` 会打这一行。
    """
    out: Dict[str, Any] = {
        "backend": BACKEND,
        "db": -1,
        "json": len(_json_load()),
        "pending": _json_pending,
    }
    if db_available():
        await _db_ready()
        try:
            out["db"] = len(await _db_load())
        except Exception as e:
            out["db_error"] = str(e)[:160]
            _log(f"BiliSubscribe：读数据库失败（{e}）")
    return out


async def add_sub(
    uid: str,
    gid: str,
    bot_id: str = "",
    bot_self_id: str = "",
    at_all: bool = False,
) -> bool:
    """订阅：给 UP 记录当前群。返回是否发生了变化。"""
    uid, gid = str(uid), str(gid)
    if db_available():
        await _db_ready()  # 建表兜底（失败也照样试着写，别把数据写去别处）
        try:
            return await _db_add(uid, gid, bot_id, bot_self_id, at_all)
        except Exception as e:
            _log(f"BiliSubscribe：写数据库失败，本次回退本地 JSON（{e}）")
            _mark_json_pending()

    async with _json_lock:
        subs = _json_load()
        info = subs.setdefault(
            uid,
            {"name": "", "last_dynamic": "", "last_video": "", "pushed_av": [], "groups": {}},
        )
        groups = info.setdefault("groups", {})
        old = groups.get(gid)
        changed = True
        if old is not None:
            changed = False
            if at_all and not old.get("at_all"):
                old["at_all"] = True
                changed = True
            if bot_id and old.get("bot_id") != bot_id:
                old["bot_id"] = bot_id
                old["bot_self_id"] = bot_self_id
                changed = True
        else:
            groups[gid] = {
                "bot_id": bot_id,
                "bot_self_id": bot_self_id,
                "at_all": bool(at_all),
            }
        _json_save(subs)
        return changed


async def remove_sub(uid: str, gid: str) -> bool:
    """取消订阅：把某 UP 从该群移除；该 UP 再无群则整条删掉。"""
    uid, gid = str(uid), str(gid)
    if db_available():
        await _db_ready()
        try:
            return await _db_remove(uid, gid)
        except Exception as e:
            _log(f"BiliSubscribe：写数据库失败，本次回退本地 JSON（{e}）")
            _mark_json_pending()

    async with _json_lock:
        subs = _json_load()
        info = subs.get(uid)
        if not info:
            return False
        groups = info.get("groups") or {}
        if gid not in groups:
            return False
        del groups[gid]
        if not groups:
            del subs[uid]
        _json_save(subs)
        return True


async def clear_group(gid: str) -> int:
    """清空某群的所有订阅，返回被清掉的 UP 数。"""
    gid = str(gid)
    if db_available():
        await _db_ready()
        try:
            return await _db_clear(gid)
        except Exception as e:
            _log(f"BiliSubscribe：写数据库失败，本次回退本地 JSON（{e}）")
            _mark_json_pending()

    async with _json_lock:
        subs = _json_load()
        empty: List[str] = []
        for uid, info in subs.items():
            groups = info.get("groups") or {}
            if gid in groups:
                del groups[gid]
            if not groups:
                empty.append(uid)
        for uid in empty:
            subs.pop(uid, None)
        _json_save(subs)
        return len(empty)


async def set_flags(uid: str, gid: str, flags: Dict[str, bool]) -> bool:
    """改某群里这条订阅的开关（``push_*`` / ``at_all_*``）；没有这条订阅返回 False。"""
    uid, gid = str(uid), str(gid)
    if not flags:
        return False
    if db_available():
        await _db_ready()
        try:
            row = await BiliSubscribeSub.base_select_data(uid=uid, group_id=gid)
        except Exception as e:
            _log(f"BiliSubscribe：查订阅失败（{e}）")
            row = None
        if row is not None:
            try:
                await BiliSubscribeSub.update_data_by_data(
                    select_data={"uid": uid, "group_id": gid},
                    update_data=dict(flags),
                )
                return True
            except Exception as e:
                _log(f"BiliSubscribe：写数据库失败，本次回退本地 JSON（{e}）")
                _mark_json_pending()

    async with _json_lock:
        subs = _json_load()
        info = subs.get(uid)
        if not info:
            return False
        groups = info.get("groups")
        if not isinstance(groups, dict) or gid not in groups:
            return False
        entry = groups[gid]
        if not isinstance(entry, dict):
            entry = {}
            groups[gid] = entry
        entry.update(flags)
        _json_save(subs)
        return True


async def merge_state(updates: Dict[str, Dict]) -> None:
    """把轮询结果（``name`` / 游标 / ``pushed_av`` / 直播状态）合并写回。

    只更新这几个字段，且 UID 已被取消订阅时跳过，避免与用户操作互相覆盖。
    """
    if not updates:
        return
    if db_available():
        await _db_ready()
        try:
            await _db_merge(updates)
            return
        except Exception as e:
            _log(f"BiliSubscribe：写数据库失败，本次回退本地 JSON（{e}）")
            _mark_json_pending()

    async with _json_lock:
        subs = _json_load()
        for uid, up in updates.items():
            info = subs.get(uid)
            if not info:
                continue
            for key in ("name", "last_dynamic", "last_video", "room_id"):
                if up.get(key):
                    info[key] = up[key]
            if up.get("pushed_av") is not None:
                info["pushed_av"] = list(up["pushed_av"])[-MAX_PUSHED_AV:]
            if up.get("live_status") is not None:
                try:
                    info["live_status"] = int(up["live_status"])
                except (TypeError, ValueError):
                    pass
        _json_save(subs)


# ---------------------------------------------------------------------------
# 快照上的纯函数（两个后端通用）
# ---------------------------------------------------------------------------
def uids_in_group(subs: Dict[str, Dict], gid: str) -> List[str]:
    """列出某群订阅的所有 UID（保持快照里的顺序）。"""
    gid = str(gid)
    return [uid for uid, info in subs.items() if gid in (info.get("groups") or {})]


def group_entry(info: Dict, gid: str) -> Dict:
    """取某群里该 UP 的推送配置（bot + 每条订阅自己的开关 + 分类 @全体）。

    **总是返回完整字典**（缺的键补默认值）：老库里没有新列、老 JSON 里没有新键，
    调用方直接 ``entry["push_dynamic"]`` 也不会炸。
    """
    raw = (info.get("groups") or {}).get(str(gid)) or {}
    entry: Dict[str, Any] = {
        "bot_id": str(raw.get("bot_id") or ""),
        "bot_self_id": str(raw.get("bot_self_id") or ""),
        "at_all": _flag(raw, "at_all", False),
    }
    for key in FLAG_KEYS:
        entry[key] = _flag(raw, key, True)
    for key in AT_KEYS:
        entry[key] = _flag(raw, key, False)
    return entry


def groups_for(info: Dict, kind: str) -> Dict[str, Dict]:
    """这个 UP 的订阅群里，**开着这类推送**的那些（kind = dynamic / video / live）。

    开关是按群存的，所以"这条动态推给谁"必须逐群看 —— 同一个 UP，A 群只想要视频、
    B 群只想要动态是合法需求，不能拿全局开关一刀切。
    """
    key = f"push_{kind}"
    out: Dict[str, Dict] = {}
    for gid in (info.get("groups") or {}):
        entry = group_entry(info, gid)
        if entry.get(key, True):
            out[str(gid)] = entry
    return out


def at_all_for(entry: Dict, kind: str) -> bool:
    """这个群、这类推送要不要 @全体：老 ``at_all`` 是哪类都@，新的三个是单类@。"""
    if not isinstance(entry, dict):
        return False
    if _flag(entry, "at_all", False):
        return True
    return _flag(entry, f"at_all_{kind}", False)


# 命令里「设置项 → 字段」的对照表（中文好记，英文也认）
FLAG_WORDS: Dict[str, str] = {
    "开动态": "push_dynamic",
    "关动态": "!push_dynamic",
    "开视频": "push_video",
    "关视频": "!push_video",
    "开直播": "push_live",
    "关直播": "!push_live",
    "动态@全体": "at_all_dynamic",
    "取消动态@全体": "!at_all_dynamic",
    "视频@全体": "at_all_video",
    "取消视频@全体": "!at_all_video",
    "直播@全体": "at_all_live",
    "取消直播@全体": "!at_all_live",
    "@全体": "*at_all",
    "取消@全体": "!at_all",
    "+dynamic": "push_dynamic",
    "-dynamic": "!push_dynamic",
    "+video": "push_video",
    "-video": "!push_video",
    "+live": "push_live",
    "-live": "!push_live",
}


def parse_flags(words: List[str]) -> Optional[Dict[str, bool]]:
    """把 ``开直播 关动态 全关`` 这类设置项解析成 ``{字段: 值}``。

    认不出来返回 ``None``（调用方回用法提示）。``全开/全关`` 会展开成三类通吃。
    """
    out: Dict[str, bool] = {}
    for word in words:
        w = str(word or "").strip()
        if not w:
            continue
        if w in ("全开", "全关", "+all", "-all"):
            value = w in ("全开", "+all")
            for key in FLAG_KEYS:
                out[key] = value
            continue
        target = FLAG_WORDS.get(w) or FLAG_WORDS.get(w.lower())
        if not target:
            return None
        if target.startswith("*"):  # 三类一起
            for key in AT_KEYS:
                out[key] = True
            continue
        negative = target.startswith("!")
        key = target.lstrip("!")
        out[key] = not negative
    return out or None


def describe_flags(entry: Dict) -> str:
    """把一条订阅的开关说成人话（列表里显示用；全开且不@就返回空串）。"""
    if not isinstance(entry, dict):
        return ""
    names = {"push_dynamic": "动态", "push_video": "视频", "push_live": "直播"}
    parts: List[str] = []
    off = [names[k] for k in FLAG_KEYS if not _flag(entry, k, True)]
    on = [names[k] for k in FLAG_KEYS if _flag(entry, k, True)]
    if len(off) == len(FLAG_KEYS):
        parts.append("全关")
    elif off:
        parts.append("仅" + "/".join(on))
    if _flag(entry, "at_all", False):
        parts.append("@全体")
    else:
        ats = [
            names[key.replace("at_all_", "push_")]
            for key in AT_KEYS
            if _flag(entry, key, False)
        ]
        if ats:
            parts.append("@全体：" + "/".join(ats))
    return "｜".join(parts)


def remember_av(info: Dict, bvid: str) -> None:
    """就地把一个 BV 号记进已推送列表（防止动态与投稿列表重复推）。"""
    bvid = str(bvid or "").strip()
    if not bvid:
        return
    pushed = [str(b) for b in (info.get("pushed_av") or []) if str(b)]
    if bvid not in pushed:
        pushed.append(bvid)
    info["pushed_av"] = pushed[-MAX_PUSHED_AV:]


def normalize_name(name: Any) -> str:
    """名字归一化：去掉所有空白（含全角空格）、大小写不敏感。

    只用来判断"完全同名" —— B站的 UP 名里常带全角空格和 emoji，用户手打时不会带上，
    所以比对前统一收一下；但**不做模糊匹配**（包含/前缀一律不算）。
    """
    return re.sub(r"\s+", "", str(name or "")).casefold()


def match_by_name(rows: List[Dict], query: str) -> Optional[Dict]:
    """在 ``[{"uid": …, "name": …}]`` 里找**名字完全一样**的那一条，返回命中的行。

    只有**恰好一条**才算命中：同名 UP 在 B站不算少见（官方号 / 攻略组 / 搬运号），
    两条同名时必须让人来挑，替用户猜错了就是订错号。找不到或多条都返回 ``None``，
    调用方照旧把候选列出来。
    """
    key = normalize_name(query)
    if not key:
        return None
    hits = [r for r in rows or [] if normalize_name(r.get("name")) == key]
    return hits[0] if len(hits) == 1 else None


def parse_uid_arg(arg: str) -> str:
    """从参数里抠出 UID：纯数字、``uid:123``、主页链接都支持。"""
    s = str(arg or "").strip()
    if not s:
        return ""
    m = re.search(r"(\d{2,})", s)
    return m.group(1) if m else ""
