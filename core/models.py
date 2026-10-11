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

"""订阅数据表（对接 GsCore 的数据库层）。

一行 = 一个「群 × UP」订阅：

- 群维度：``group_id`` / ``bot_id`` / ``bot_self_id`` / ``at_all``
  ＋**每条订阅自己的开关**：``push_dynamic`` / ``push_video`` / ``push_live``
  ＋分类 @全体：``at_all_dynamic`` / ``at_all_video`` / ``at_all_live``
- 游标维度：``name`` / ``last_dynamic`` / ``last_video`` / ``pushed_av``
  ＋直播：``room_id`` / ``live_status``

游标按 UID 冗余存在每一行里（同一个 UP 的游标对所有群是同一份），
更新时按 uid 一次改掉所有行，读的时候先到先得 —— 换来的是单表、无 JOIN、
删除订阅也就是删一行，不用维护第二张表的引用关系。

**开关也按行存**（不是按 UID）：粒度是「这个群里对这条订阅推什么」，
所以同一个 UP 在不同群可以有不同配置。
"""
from typing import Optional, Tuple

from sqlmodel import Field

from gsuid_core.utils.database.base_models import BaseIDModel


def _log(msg: str, level: str = "warning") -> None:
    try:
        from gsuid_core.logger import logger

        getattr(logger, level, logger.info)(msg)
    except Exception:
        pass


class BiliSubscribeSub(BaseIDModel, table=True):
    """B站订阅表：一行一个「群 × UP」。"""

    __tablename__ = "BiliSubscribeSub"

    uid: str = Field(index=True, title="UP UID")
    group_id: str = Field(index=True, title="群号")
    bot_id: Optional[str] = Field(default=None, title="平台")
    bot_self_id: Optional[str] = Field(default=None, title="机器人账号")
    at_all: bool = Field(default=False, title="是否@全体成员")
    # 每条订阅自己的开关：默认全开 = 跟以前行为一致，想少收哪类就关哪类
    push_dynamic: bool = Field(default=True, title="推送动态")
    push_video: bool = Field(default=True, title="推送视频")
    push_live: bool = Field(default=True, title="推送直播")
    # 分类 @全体（老 at_all 是"哪类都@"，这三个是单类@）
    at_all_dynamic: bool = Field(default=False, title="动态@全体")
    at_all_video: bool = Field(default=False, title="视频@全体")
    at_all_live: bool = Field(default=False, title="直播@全体")
    name: Optional[str] = Field(default=None, title="UP 名字")
    last_dynamic: Optional[str] = Field(default=None, title="动态游标")
    last_video: Optional[str] = Field(default=None, title="投稿游标")
    pushed_av: Optional[str] = Field(default=None, title="已推送投稿(JSON)")
    room_id: Optional[str] = Field(default=None, title="直播间号")
    live_status: Optional[int] = Field(default=None, title="开播状态(0未播/1直播中/2轮播)")


# ---------------------------------------------------------------------------
# 挂到 Web 控制台的「数据库管理」页
#
# 光有模型**不会**出现在那个页面：必须再写一个继承 ``GsAdminModel`` 的 Admin 类，
# 打上 ``@site.register_admin``（它同时负责权限注册），并用 ``model = ...`` 指到模型。
# 装饰器在 import 时执行，所以这段必须待在插件加载必然 import 到的模块里 ——
# ``models.py`` 由 ``storage.py`` 在加载时导入，符合条件。
#
# 以后给表加列，记得同时往 ``gsuid_core.utils.database.startup`` 的 ``exec_list``
# 里加 ``ALTER TABLE BiliSubscribeSub ADD COLUMN …``，否则老库不会自动补列。
# ---------------------------------------------------------------------------
ADMIN_REGISTERED = False

try:
    from gsuid_core.webconsole.mount_app import GsAdminModel, PageSchema, site

    @site.register_admin
    class BiliSubscribeSubAdmin(GsAdminModel):
        """数据库管理页里的「B站订阅」表（订阅/游标都在这一张表）。"""

        pk_name = "id"
        page_schema = PageSchema(label="B站订阅", icon="fa fa-rss")
        model = BiliSubscribeSub

    ADMIN_REGISTERED = True
except Exception as e:  # 核心版本较老 / 离线单测：没这个模块就当没有控制台
    _log(f"BiliSubscribe：未挂到网页控制台的数据库管理页（{e}）", "info")


# 老库补列用的 ALTER（新装的话 create_all 已经带上了这些列，这几条会全部失败=跳过）。
# 用 BOOLEAN DEFAULT 1/0 这种写法：SQLite / MySQL / PostgreSQL 都吃。
NEW_COLUMNS: Tuple[str, ...] = (
    "ALTER TABLE BiliSubscribeSub ADD COLUMN push_dynamic BOOLEAN DEFAULT 1",
    "ALTER TABLE BiliSubscribeSub ADD COLUMN push_video BOOLEAN DEFAULT 1",
    "ALTER TABLE BiliSubscribeSub ADD COLUMN push_live BOOLEAN DEFAULT 1",
    "ALTER TABLE BiliSubscribeSub ADD COLUMN at_all_dynamic BOOLEAN DEFAULT 0",
    "ALTER TABLE BiliSubscribeSub ADD COLUMN at_all_video BOOLEAN DEFAULT 0",
    "ALTER TABLE BiliSubscribeSub ADD COLUMN at_all_live BOOLEAN DEFAULT 0",
    "ALTER TABLE BiliSubscribeSub ADD COLUMN room_id TEXT DEFAULT ''",
    "ALTER TABLE BiliSubscribeSub ADD COLUMN live_status INTEGER",
)


async def ensure_table() -> bool:
    """确保本插件的表存在，**并给老库补新列**。

    核心启动时会对 ``SQLModel.metadata`` 跑一次 ``create_all``，而插件在启动前就被
    import，模型那时已经注册进 metadata，所以正常情况下表会被一起建出来。
    插件加载晚于那个时点（热重载/手动装插件）时这里兜底补建一次，
    只建自己这一张表，不动核心的表；``checkfirst=True`` 保证幂等。

    ``create_all(checkfirst=True)`` **不会给已存在的表加列**，所以新加的列还得自己
    ``ALTER``：逐条执行、失败就跳过（列已存在时必然报错，属于正常情况）。
    不补列的话，老库上读写这一行会直接报 "no such column"，整条订阅就废了。
    """
    try:
        from gsuid_core.utils.database.base_models import engine
    except Exception as e:
        _log(f"BiliSubscribe：核心数据库层不可用，改用本地 JSON（{e}）", "info")
        return False

    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: BiliSubscribeSub.__table__.create(c, checkfirst=True)
            )
    except Exception as e:
        _log(f"BiliSubscribe：建表失败，改用本地 JSON（{e}）")
        return False

    added = 0
    for sql in NEW_COLUMNS:
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql(sql)
            added += 1
        except Exception:
            continue  # 列已存在（或这版数据库不吃这条语法）：跳过就行
    if added:
        _log(f"BiliSubscribe：给订阅表补了 {added} 个新列（老库升级）", "info")
    return True
