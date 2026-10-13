# BiliSubscribe - 订阅 B站 UP 主的视频与动态更新，推送到群聊
# Copyright (C) 2026  Dowond (bilibili-subscribe)
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

"""游戏规则 ↔ 数据库镜像：让规则能在控制台的「数据库管理」里看和改。

规则本来是 ``game_rules/*.json``，改一次得进服务器/文件管理器。但"打开插件目录就能改"
和"在网页控制台里改"是两类需求，所以这里把规则**镜像**进 ``GsData.db`` 的
``biligamerule`` 表 —— 控制台「数据库管理 → 游戏过滤规则」直接编辑，改完自动写回 JSON。

**谁是权威（重要，别和 config 的启停搞混）：**

- **有没有这条规则 → 文件说了算**：加 ``xxx.json`` 就有，删掉就没有（表里那行会被清掉）。
  在控制台**新增**的行不会被采纳 —— 它没有文件载体，下一轮同步就没了；
  想加规则请加 JSON 文件。
- **规则内容（口径/官方号/关键词/说明） → 谁改的算谁**：表里存了 ``rev``
  （上次同步时的内容指纹）。文件改了（指纹对不上）就写进表；表改了（指纹对不上）
  就写回文件；**两边都改了以数据库为准**（控制台是"人正开着"的那一侧）。
  同步完两边指纹一致，所以不会来回打架。
- **启不启用** 不在这里：看配置项「启用的游戏规则」（见 config.py / games.py）。

同步是双向且幂等的，默认每 ``SYNC_INTERVAL`` 秒跑一轮（第一次用到时启动后台任务），
``bili游戏规则 同步`` 可以立刻跑一轮。

**实现上的两个坑（踩过，别改回去）：**

1. **读和写必须在同一个 session 里做**。不要"先查出一批 ORM 对象、关掉 session、
   再在新 session 里 ``session.add(那个对象)``" —— 那是 detached 实例，
   ``session.delete()`` 会直接抛异常，而异常会让**整个 session（连同前面的插入）一起回滚**，
   表现就是"控制台里数据闪现一下就没了"。
2. **文件侧读不到时绝不清表**。规则目录不存在/读不了 ≠ 用户把规则删光了，
   这时候只补不删，否则一次误判就能把整张表清空。

拿不到数据库层（核心版本较老 / 离线单测）时整模块空转，规则照旧只读 JSON。
"""
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..paths import GAME_RULES_DIR


def _log(msg: str, level: str = "warning") -> None:
    try:
        from gsuid_core.logger import logger

        getattr(logger, level, logger.info)(msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 模型 + 挂到 Web 控制台
#
# 光有模型**不会**出现在「数据库管理」页：还得写一个继承 ``GsAdminModel`` 的 Admin 类、
# 打上 ``@site.register_admin``（它同时负责权限注册），并用 ``model = ...`` 指到模型。
# 装饰器在 import 时执行，所以本模块必须待在插件加载必然 import 到的链路上
# （``core/__init__.py`` 会导入它）。
# ---------------------------------------------------------------------------
MODEL_READY = False
ADMIN_REGISTERED = False
BiliGameRule: Any = None
TABLE_NAME = "biligamerule"

try:
    from sqlmodel import Field

    from gsuid_core.utils.database.base_models import BaseIDModel

    class BiliGameRule(BaseIDModel, table=True):  # type: ignore[misc,valid-type]
        """一行 = 一条游戏过滤规则（``game_rules/<rule>.json`` 的镜像）。

        列名全部用"人类可读的文本"，因为它的主要用途就是**在控制台里手改**：
        多个官方号 / 多个关键词都写成"一行一个"，比 JSON 数组好编辑得多。

        表名特意用全小写 ``biligamerule``：控制台是按"类名小写"去 SQLAlchemy
        元数据里找表的，全小写才对得上（否则搜索/改行会失效）。
        """

        __tablename__ = TABLE_NAME

        rule: str = Field(index=True, title="规则名（= JSON 文件名，不含扩展名）")
        name: str = Field(default="", title="游戏名（展示用）")
        mode: str = Field(default="whitelist", title="口径：whitelist=白名单 / blacklist=黑名单")
        uids: str = Field(default="", title="官方号（每行一个 UID=名字）")
        dynamic_pass_keywords: str = Field(default="", title="动态放行词（每行一个）")
        dynamic_block_keywords: str = Field(default="", title="动态屏蔽词（每行一个）")
        video_pass_keywords: str = Field(default="", title="视频放行词（每行一个，只按标题）")
        video_block_keywords: str = Field(default="", title="视频屏蔽词（每行一个，只按标题）")
        filter_dynamic: str = Field(default="1", title="动态走本规则（1=是 / 0=否）")
        filter_video: str = Field(default="0", title="视频走本规则（1=是 / 0=否）")
        note: str = Field(default="", title="说明")
        rev: str = Field(default="", title="同步指纹，请勿手改")

    MODEL_READY = True
except Exception as e:  # 核心版本较老 / 离线单测：规则只读 JSON
    _log(f"BiliSubscribe：核心数据库层不可用，游戏规则只读 JSON（{e}）", "info")

if MODEL_READY:
    try:
        from gsuid_core.webconsole.mount_app import GsAdminModel, PageSchema, site

        @site.register_admin
        class BiliGameRuleAdmin(GsAdminModel):
            """数据库管理页里的「游戏过滤规则」表（改内容，增删请动 JSON 文件）。"""

            pk_name = "id"
            page_schema = PageSchema(label="游戏过滤规则", icon="fa fa-filter")
            model = BiliGameRule

        ADMIN_REGISTERED = True
    except Exception as e:  # 没有网页控制台：不影响过滤功能
        _log(f"BiliSubscribe：游戏规则未挂到网页控制台（{e}）", "info")


# 老库补列用（新装时 create_all 已经带上了，这几条会全部失败 = 跳过）
NEW_COLUMNS: Tuple[str, ...] = (
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN name TEXT DEFAULT ''",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN mode TEXT DEFAULT 'whitelist'",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN uids TEXT DEFAULT ''",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN dynamic_pass_keywords TEXT DEFAULT ''",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN dynamic_block_keywords TEXT DEFAULT ''",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN video_pass_keywords TEXT DEFAULT ''",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN video_block_keywords TEXT DEFAULT ''",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN filter_dynamic TEXT DEFAULT '1'",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN filter_video TEXT DEFAULT '0'",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN note TEXT DEFAULT ''",
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN rev TEXT DEFAULT ''",
)
# 更早的表列叫 pass_keywords / block_keywords（当年只有动态那一套词）：
# 新列补好后把老数据搬过去，免得老库的"动态词一夜清空"
MIGRATE_COLUMNS: Tuple[str, ...] = (
    f"UPDATE {TABLE_NAME} SET dynamic_pass_keywords = pass_keywords "
    f"WHERE (dynamic_pass_keywords IS NULL OR dynamic_pass_keywords = '') "
    f"AND pass_keywords IS NOT NULL AND pass_keywords != ''",
    f"UPDATE {TABLE_NAME} SET dynamic_block_keywords = block_keywords "
    f"WHERE (dynamic_block_keywords IS NULL OR dynamic_block_keywords = '') "
    f"AND block_keywords IS NOT NULL AND block_keywords != ''",
)


# ---------------------------------------------------------------------------
# 文本 ↔ 结构（都是"一行一个"，方便在控制台里手改）
# ---------------------------------------------------------------------------
def _rows_to_text(pairs: Any) -> str:
    """``{"123": "名字"}`` → ``"123=名字"``，一行一个。"""
    return "\n".join(f"{k}={v}" for k, v in pairs)


def _text_to_rows(text: Any) -> Dict[str, str]:
    """``"123=名字"``（一行一个）→ ``{"123": "名字"}``；只写 UID 也认。"""
    out: Dict[str, str] = {}
    for line in str(text or "").replace("，", "\n").replace(",", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        uid, _, uname = line.partition("=")
        uid = uid.strip()
        if uid:
            out[uid] = uname.strip() or uid
    return out


def _text_to_list(text: Any) -> List[str]:
    """关键词：一行一个（也认逗号分隔）。"""
    out: List[str] = []
    for line in str(text or "").replace("，", "\n").splitlines():
        for part in line.split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def _any_list(raw: Any) -> List[str]:
    """关键词字段：JSON 数组、逗号串、单个字符串都收。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return _text_to_list(raw)


def _list_to_text(items: Any) -> str:
    if isinstance(items, str):
        return items
    return "\n".join(str(x).strip() for x in (items or []) if str(x).strip())


def _as_bool(raw: Any, default: bool) -> bool:
    """开关字段：JSON 里写 ``true`` / ``"开"`` / ``1`` 都认，认不出用 default。

    和 games.py 的 ``_as_bool`` 口径保持一致（这里不 import 它，免得绕出依赖）。
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("1", "true", "on", "yes", "开", "是", "启用", "开启"):
            return True
        if s in ("0", "false", "off", "no", "关", "否", "禁用", "关闭"):
            return False
    return default


# ---------------------------------------------------------------------------
# 内容指纹：只认"能落到表里的那几个字段"，改 ``_说明`` 之类不算改动
# ---------------------------------------------------------------------------
def _canon(
    name: Any,
    mode: Any,
    uids: Any,
    dynamic_pass_keywords: Any,
    dynamic_block_keywords: Any,
    video_pass_keywords: Any,
    video_block_keywords: Any,
    note: Any,
    filter_dynamic: Any = True,
    filter_video: Any = False,
) -> Dict[str, Any]:
    if isinstance(uids, str):
        pairs = _text_to_rows(uids)
    else:
        pairs = {
            str(k).strip(): (str(v).strip() or str(k).strip())
            for k, v in (uids or {}).items()
            if str(k).strip()
        }
    return {
        "name": str(name or "").strip(),
        "mode": str(mode or "whitelist").strip().lower() or "whitelist",
        "uids": sorted(pairs.items()),
        # 动态、视频各一套词（互不影响）
        "dynamic_pass": _any_list(dynamic_pass_keywords),
        "dynamic_block": _any_list(dynamic_block_keywords),
        "video_pass": _any_list(video_pass_keywords),
        "video_block": _any_list(video_block_keywords),
        "note": str(note or "").strip(),
        # 这条规则管哪类内容（默认动态过滤、视频不过滤，和 games.py 口径一致）
        "filter_dynamic": _as_bool(filter_dynamic, True),
        "filter_video": _as_bool(filter_video, False),
    }


#: 指纹带版本前缀：字段集一变（比如这次新增 video_* 两套词）旧指纹就整体作废 ——
#: 否则老库里"没有新字段"会被当成"表被改过"，反过来把用户的 JSON 覆盖掉。
_REV_PREFIX = "v2:"


def _rev_of(canon: Dict[str, Any]) -> str:
    blob = json.dumps(canon, ensure_ascii=False, sort_keys=True)
    return _REV_PREFIX + hashlib.md5(blob.encode("utf-8")).hexdigest()[:16]


def _rev_is_current(rev: str) -> bool:
    """这个指纹是不是本版写的（老版本的指纹没有前缀，一律当作"无历史"）。"""
    return str(rev or "").startswith(_REV_PREFIX)


def _file_canon(raw: Dict[str, Any], stem: str) -> Dict[str, Any]:
    return _canon(
        name=raw.get("name") or stem,
        mode=raw.get("mode"),
        uids=raw.get("uids") or {},
        # 老名字兼容：pass_keywords / block_keywords 就是「动态」那套词，
        # filter_keywords 是更早的名字（当年只有一套词）
        dynamic_pass_keywords=raw.get(
            "dynamic_pass_keywords", raw.get("pass_keywords")
        ),
        dynamic_block_keywords=raw.get(
            "dynamic_block_keywords",
            raw.get("block_keywords", raw.get("filter_keywords")),
        ),
        video_pass_keywords=raw.get("video_pass_keywords"),
        video_block_keywords=raw.get("video_block_keywords"),
        note=raw.get("note"),
        filter_dynamic=raw.get("filter_dynamic"),
        filter_video=raw.get("filter_video"),
    )


def _row_canon(row: Any) -> Dict[str, Any]:
    return _canon(
        name=getattr(row, "name", ""),
        mode=getattr(row, "mode", ""),
        uids=getattr(row, "uids", ""),
        dynamic_pass_keywords=getattr(row, "dynamic_pass_keywords", ""),
        dynamic_block_keywords=getattr(row, "dynamic_block_keywords", ""),
        video_pass_keywords=getattr(row, "video_pass_keywords", ""),
        video_block_keywords=getattr(row, "video_block_keywords", ""),
        note=getattr(row, "note", ""),
        # 表里存的是 "1"/"0" 文本（_as_bool 认它）
        filter_dynamic=getattr(row, "filter_dynamic", ""),
        filter_video=getattr(row, "filter_video", ""),
    )


# ---------------------------------------------------------------------------
# 文件读写
# ---------------------------------------------------------------------------
_DEFAULT_NOTE = (
    "一个游戏一个 JSON，文件名就是规则名（启停看文件里的 enabled 字段）。"
    "uids 必填（{UID: 号的名字}，可多个）；mode=whitelist：命中放行词 → 直接推，"
    "其余过滤；mode=blacklist：默认全推，命中屏蔽词 → 直接丢弃。"
    "动态、视频各一套词：dynamic_pass_keywords / dynamic_block_keywords（标题+正文）、"
    "video_pass_keywords / video_block_keywords（只按标题）；"
    "filter_dynamic / filter_video 决定这条规则管不管动态、投稿视频（默认只过滤动态）。"
    "本文件也可以在控制台「数据库管理 → 游戏过滤规则」里改，改完会自动写回这里。"
)


def _read_files() -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
    """``{规则名: (原始 JSON, canon)}``；读不了/没 uids 的文件直接跳过（games.py 会记日志）。"""
    out: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
    try:
        paths = sorted(GAME_RULES_DIR.glob("*.json"))
    except OSError as e:
        _log(f"BiliSubscribe：读不到规则目录 {GAME_RULES_DIR}（{e}）")
        return out
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, dict) or not isinstance(raw.get("uids"), dict) or not raw.get("uids"):
            continue
        out[path.stem] = (raw, _file_canon(raw, path.stem))
    return out


def _write_file(stem: str, canon: Dict[str, Any], old_raw: Optional[Dict[str, Any]]) -> bool:
    """按 canon 写回 ``<stem>.json``；``_说明`` 沿用文件里原有的，没有就写默认的。"""
    path = GAME_RULES_DIR / f"{stem}.json"
    data: Dict[str, Any] = {
        "_说明": (old_raw or {}).get("_说明") or _DEFAULT_NOTE,
        "name": canon["name"],
        # 保留文件里原本的启停状态（别把用户手写的 enabled: false 抹掉）
        "enabled": (old_raw or {}).get("enabled", True),
        "mode": canon["mode"],
        "filter_dynamic": bool(canon["filter_dynamic"]),
        "filter_video": bool(canon["filter_video"]),
        "uids": {k: v for k, v in canon["uids"]},
        "dynamic_pass_keywords": list(canon["dynamic_pass"]),
        "dynamic_block_keywords": list(canon["dynamic_block"]),
        "video_pass_keywords": list(canon["video_pass"]),
        "video_block_keywords": list(canon["video_block"]),
        "note": canon["note"],
    }
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        _log(f"BiliSubscribe：写规则文件 {path.name} 失败（{e}）")
        return False
    return True


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
#: 后台自动同步间隔（秒）；命令里的 ``bili游戏规则 同步`` 用 force 绕过节流。
SYNC_INTERVAL = 10.0
_state: Dict[str, Any] = {
    "ready": False,
    "ready_at": 0.0,
    "last_sync": 0.0,
    "loop": False,
    "last_error": "",
    "last_result": "还没同步过",
}
_READY_RETRY_COOLDOWN = 60.0
_lock = asyncio.Lock()


def db_available() -> bool:
    """数据库层能不能用（模型建得出来）。"""
    return MODEL_READY


def _session():
    """拿一个核心认可的 session（文档里的"手动管理 Session"写法）。"""
    from gsuid_core.utils.database.base_models import async_maker

    return async_maker()


async def _exec(sql: str) -> None:
    """执行一条 SQL（建表 / 补列用）。

    走核心自己给的 session，**不碰引擎在核心内部的私有名字** —— 老实现直接
    ``from ...base_models import engine`` 再 ``conn.exec_driver_sql()``，这两步任一对不上
    都会被下面那个 ``except`` 吞掉，结果就是"列没补上、还一声不吭"。
    """
    from sqlalchemy import text as sa_text

    async with _session() as session:
        await session.execute(sa_text(sql))
        await session.commit()


#: 这些报错属于"本来就该发生"：列已存在（新装）、老库没有老列可迁（新库）
_BENIGN_SQL_ERRORS = (
    "duplicate column",
    "already exists",
    "no such column",
    "no such table",
)


def _benign(err: Exception) -> bool:
    msg = str(err).lower()
    return any(x in msg for x in _BENIGN_SQL_ERRORS)


async def ensure_table() -> bool:
    """确保 ``biligamerule`` 表存在，并给老库补上新列（缺列就直接 ``no such column``）。

    ⚠️ 控制台「数据库管理」是按**模型**直接 SELECT 的（``admin_api.get_table_data``），
    它不关心这张表实际有哪些列 —— 所以列必须在这里补齐，否则一打开页面就报
    ``no such column: biligamerule.xxx``。失败**不再静默**：真补不上会记日志 + 返回 False，
    交给上层在冷却后重试。
    """
    if not MODEL_READY:
        return False

    try:
        async with _session() as session:
            conn = await session.connection()
            await conn.run_sync(
                lambda c: BiliGameRule.__table__.create(c, checkfirst=True)
            )
            await session.commit()
    except Exception as e:
        _state["last_error"] = f"建表失败：{type(e).__name__}: {e}"
        _log(f"BiliSubscribe：{_state['last_error']}，游戏规则只读 JSON")
        return False

    problems: List[str] = []
    for sql in NEW_COLUMNS + MIGRATE_COLUMNS:
        try:
            await _exec(sql)
        except Exception as e:
            if _benign(e):
                continue
            problems.append(f"{type(e).__name__}: {e} ← {sql}")

    if problems:
        _state["last_error"] = "补列/迁移失败：" + "；".join(problems[:3])
        _log(
            f"BiliSubscribe：{_state['last_error']}"
            "（控制台「数据库管理 → 游戏过滤规则」会因此报 no such column）"
        )
        return False

    _state["last_error"] = ""
    return True


async def count_rows() -> Optional[int]:
    """表里现在有多少行；拿不到数据库返回 None（``bili游戏规则 诊断`` 用）。"""
    if not MODEL_READY:
        return None
    try:
        from sqlalchemy import func
        from sqlalchemy import select as sa_select

        async with _session() as session:
            result = await session.execute(sa_select(func.count()).select_from(BiliGameRule))
            return int(result.scalar() or 0)
    except Exception as e:
        _state["last_error"] = f"读表失败：{e}"
        return None


def _row_data(stem: str, canon: Dict[str, Any], rev: str) -> Dict[str, Any]:
    return {
        "rule": stem,
        "name": canon["name"],
        "mode": canon["mode"],
        "uids": _rows_to_text(canon["uids"]),
        "dynamic_pass_keywords": _list_to_text(canon["dynamic_pass"]),
        "dynamic_block_keywords": _list_to_text(canon["dynamic_block"]),
        "video_pass_keywords": _list_to_text(canon["video_pass"]),
        "video_block_keywords": _list_to_text(canon["video_block"]),
        "filter_dynamic": "1" if canon["filter_dynamic"] else "0",
        "filter_video": "1" if canon["filter_video"] else "0",
        "note": canon["note"],
        "rev": rev,
    }


async def _sync_in_session(session: Any, files: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]],
                           allow_delete: bool) -> int:
    """真正的对齐逻辑：**在同一个 session 里**读旧行 + 改/插/删。

    返回改动条数。特意不把 ORM 对象带出 session（detached 实例会炸，见模块说明）。
    """
    from sqlalchemy import select as sa_select

    rows = list((await session.execute(sa_select(BiliGameRule))).scalars().all())
    existing: Dict[str, Any] = {}
    for row in rows:
        stem = str(getattr(row, "rule", "") or "")
        if stem:
            existing[stem] = row

    changed = 0

    # ① 文件 → 表：没有的行就建；指纹对不上就判断是谁改的
    for stem, (raw, canon) in files.items():
        frev = _rev_of(canon)
        row = existing.pop(stem, None)
        if row is None:
            session.add(BiliGameRule(**_row_data(stem, canon, frev)))
            changed += 1
            continue
        rrev = str(getattr(row, "rev", "") or "")
        if rrev == frev:
            continue  # 指纹一致：两边都没动
        rcanon = _row_canon(row)
        if rcanon == canon:
            # 内容其实一样，只是指纹没跟上（老库刚补列 / 只动了空白）：对齐指纹即可
            row.rev = frev
            changed += 1
            continue
        rrev_now = _rev_of(rcanon)
        if _rev_is_current(rrev) and rrev_now != rrev:
            # 表有本版指纹、又和表的当前内容不符 → 表侧改过（或两边都改了）：表赢，写回文件
            _write_file(stem, rcanon, raw)
            for key, value in _row_data(stem, rcanon, rrev_now).items():
                setattr(row, key, value)
        else:
            # 没有本版指纹（老版本写的 / 刚补列）或表没动过：以文件为准，写进表
            for key, value in _row_data(stem, canon, frev).items():
                setattr(row, key, value)
        changed += 1

    # ② 表里有、文件没了 = 规则被删了（"删掉就是删掉"）
    #    但**文件侧读不到时绝不清表**：目录没了不等于用户把规则删光了
    for stem, row in existing.items():
        if allow_delete:
            await session.delete(row)
            changed += 1
            _log(f"BiliSubscribe：规则 {stem} 的 JSON 已删除，同步清掉表里那一行", "info")
        else:
            _log(f"BiliSubscribe：规则 {stem} 在表里但读不到对应 JSON，先留着不动", "info")
    return changed


async def sync(force: bool = False) -> bool:
    """跑一轮双向同步；真动过东西返回 True（拿不到数据库就恒 False）。

    加了锁：后台任务和命令可能同时调它。``force`` 只跳过"刚跑过"的节流。
    """
    if not MODEL_READY:
        return False
    now = time.time()
    if not force and now - float(_state["last_sync"] or 0.0) < SYNC_INTERVAL:
        return False
    if _lock.locked():
        return False

    async with _lock:
        _state["last_sync"] = time.time()
        if not _state["ready"]:
            if now - float(_state["ready_at"] or 0.0) < _READY_RETRY_COOLDOWN:
                return False
            _state["ready_at"] = now
            _state["ready"] = await ensure_table()
            if not _state["ready"]:
                return False

        dir_ok = GAME_RULES_DIR.is_dir()
        files = _read_files()
        if not files and not dir_ok:
            # 两边都空的唯一解释就是"读不到文件"，这时候动库没意义
            _state["last_result"] = f"跳过：读不到规则目录 {GAME_RULES_DIR}"
            return False

        try:
            async with _session() as session:
                changed = await _sync_in_session(session, files, allow_delete=dir_ok)
                if not changed:
                    _state["last_result"] = "两边一致，无需改动"
                    return False
                await session.commit()
        except Exception as e:
            _state["last_error"] = f"{type(e).__name__}: {e}"
            _log(f"BiliSubscribe：游戏规则同步失败（{_state['last_error']}）")
            return False

        _state["last_result"] = f"本轮改动 {changed} 条（文件 {len(files)} 份）"
        _log(f"BiliSubscribe：游戏规则同步完成，{_state['last_result']}", "info")
        return True


async def _sync_loop() -> None:
    """后台循环：每 ``SYNC_INTERVAL`` 秒同步一轮（控制台改完 ~10 秒内落到文件）。"""
    while True:
        try:
            await asyncio.sleep(SYNC_INTERVAL)
            await sync()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(f"BiliSubscribe：游戏规则后台同步出错（{type(e).__name__}: {e}）", "info")


def ensure_loop() -> None:
    """第一次用到时起后台同步任务（没在事件循环里 / 没数据库就什么都不做）。"""
    if _state["loop"] or not MODEL_READY:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _state["loop"] = True
    loop.create_task(_sync_loop())
    _log(f"BiliSubscribe：游戏规则后台同步已启动（每 {int(SYNC_INTERVAL)} 秒一轮）", "info")


async def sync_now() -> bool:
    """给命令用的"立刻同步一轮"。"""
    ensure_loop()
    return await sync(force=True)


async def diagnose() -> str:
    """``bili游戏规则 诊断``：把这套东西当前的状态列出来，方便定位"控制台里没数据"。"""
    lines = ["【B站订阅·游戏规则自检】"]
    lines.append(f"规则目录：{GAME_RULES_DIR}")
    lines.append(f"　目录存在：{GAME_RULES_DIR.is_dir()}")
    files = _read_files()
    lines.append(f"　读到规则文件：{len(files)} 份" + (f"（{'、'.join(sorted(files))}）" if files else ""))
    lines.append(f"启用名单：{cfg_enabled_text()}")

    lines.append(f"数据库模块：{'就绪' if MODEL_READY else '不可用（只读 JSON）'}")
    lines.append(f"控制台页面：{'已注册' if ADMIN_REGISTERED else '未注册'}")
    rows = await count_rows()
    lines.append(f"表 {TABLE_NAME} 行数：{'读不到' if rows is None else rows}")
    lines.append(f"上次同步：{_state['last_result']}")
    if _state["last_error"]:
        lines.append(f"最近错误：{_state['last_error']}")
    lines.append("")
    lines.append("控制台没数据时按这个顺序看：")
    lines.append("1) 「数据库模块」必须是就绪；不是的话看上面那条错误");
    lines.append("2) 「表行数」是 0 → 执行 bili游戏规则 同步 再看末尾日志");
    lines.append("3) 行数 >0 但页面仍空 → 刷新浏览器缓存 / 换个浏览器试试")
    return "\n".join(lines)


def cfg_enabled_text() -> str:
    """启用名单（延迟 import，避免 config ↔ 本模块的导入环）。"""
    try:
        from . import config as cfg

        names = [str(x) for x in cfg.get_list("game_rules_enabled")]
        return "、".join(names) if names else "（空 = 全部停用）"
    except Exception as e:
        return f"（读不到：{e}）"
