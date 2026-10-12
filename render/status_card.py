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

"""核心状态图里的插件状态卡片。

通过 ``gsuid_core.status.plugin_status.register_status`` 注册，渲染方是核心的
``draw_status()``（也就是控制台状态图 / ``信息``、``状态`` 命令输出的那张图）。

两条来自渲染方的硬约束（见 ``gsuid_core/status/draw_status.py``）：

1. 每个插件**最多画 3 个徽章**（``if indexj >= 2: break``），所以最关键的三项放最前面；
2. 徽章宽度有限（240px、字号 46），回调返回值只能是 ``str`` / ``int`` / ``float``，
   返回 ``None`` 会让核心渲染抛 ``AttributeError``（它会对非数字值调 ``.isdigit()``），
   所以下面每个回调都保证返回短字符串或数字。

图标：优先用插件目录下的 ``ICON.png``；没有就用 PIL 现场画一个 B站粉的圆角方块，
不往仓库里塞二进制文件。
"""
import time
from typing import Any, Callable, Dict, Optional

from ..core import config as cfg
from ..core import runtime
from ..core import storage
from ..paths import PLUGIN_ROOT


def _log(msg: str, level: str = "warning") -> None:
    try:
        from gsuid_core.logger import logger

        getattr(logger, level, logger.info)(msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 图标
# ---------------------------------------------------------------------------
def _build_icon(size: int = 256):
    """画一个 B站粉圆角方块 + 白色播放三角；PIL 不可用时返回 None。"""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=int(size * 0.22),
        fill=(251, 114, 153, 255),  # #FB7299
    )
    cx = cy = size / 2
    half_h = size * 0.26
    draw.polygon(
        [
            (cx - half_h * 0.62, cy - half_h),
            (cx - half_h * 0.62, cy + half_h),
            (cx + half_h * 0.95, cy),
        ],
        fill=(255, 255, 255, 255),
    )
    return img


def get_icon():
    """插件目录里的 ICON.png 优先，否则现场生成。

    大小写都试一遍：Windows 上不区分，Linux（GsCore 的实际部署环境）区分，
    用户丢进来叫 ``icon.png`` 的话大写法是找不到的。
    """
    here = PLUGIN_ROOT
    for name in ("ICON.png", "icon.png", "ICON.PNG"):
        path = here / name
        if not path.is_file():
            continue
        try:
            from PIL import Image

            with Image.open(path) as im:
                return im.convert("RGBA").copy()
        except Exception as e:
            _log(f"BiliSubscribe：读取 {name} 失败，改用内置图标（{e}）")
            break
    return _build_icon()


# ---------------------------------------------------------------------------
# 状态项（顺序 = 状态图里的显示顺序，前 3 个才会画出来）
# ---------------------------------------------------------------------------
async def up_count() -> int:
    """已订阅的 UP 数。"""
    subs = await storage.load_subs()
    return len([uid for uid, info in subs.items() if info.get("groups")])


async def group_count() -> int:
    """订阅落在多少个群里。"""
    groups = set()
    for info in (await storage.load_subs()).values():
        groups.update((info.get("groups") or {}).keys())
    return len(groups)


async def last_check() -> str:
    """上次检查距今多久（徽章很窄，值要短）。"""
    ts = float(runtime.STATE.get("last_check_ts") or 0)
    if ts <= 0:
        return "未检查"
    delta = max(0, int(time.time() - ts))
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{delta // 60}分前"
    return f"{delta // 3600}时前"


async def last_updates() -> int:
    """上一轮检查有更新的 UP 数。"""
    return int(runtime.STATE.get("last_updates") or 0)


async def pushed_total() -> int:
    """本次启动以来累计推送的消息条数。"""
    return int(runtime.STATE.get("pushed") or 0)


async def interval() -> str:
    """当前轮询间隔。"""
    try:
        return f"{int(cfg.get('check_interval') or 0)}秒"
    except (TypeError, ValueError):
        return "未知"


async def cookie_state() -> str:
    """Cookie 是否配了（动态接口风控时最关键的一项）。"""
    return "已配置" if str(cfg.get("bilibili_cookie") or "").strip() else "未配置"


async def run_state() -> str:
    """当前是否正在跑检查。"""
    return "检查中" if runtime.STATE.get("running") else "空闲"


async def pending_count() -> int:
    """待定队列里的动态条数（只有开了「未命中进待定队列」才会有）。"""
    from ..core import pending

    return await pending.count()


async def filtered_count() -> int:
    """本次启动以来被规则挡下的动态条数（命中屏蔽词 + 白名单口径没命中）。"""
    return int(runtime.STATE.get("filtered") or 0)


STATUS_ITEMS: Dict[str, Callable[[], Any]] = {
    "订阅UP": up_count,
    "订阅群": group_count,
    "上次检查": last_check,
    "已过滤": filtered_count,
    "待定": pending_count,
    "本轮更新": last_updates,
    "累计推送": pushed_total,
    "轮询间隔": interval,
    "Cookie": cookie_state,
    "运行状态": run_state,
}


def register() -> bool:
    """把状态卡片注册进核心；核心没有这套 API（或没有 PIL）时静默跳过。"""
    try:
        from gsuid_core.status.plugin_status import register_status
    except Exception as e:
        _log(f"BiliSubscribe：核心没有状态卡片 API，跳过注册（{e}）", "info")
        return False

    icon: Optional[Any] = get_icon()
    if icon is None:
        _log("BiliSubscribe：没有可用的图标（缺 PIL），跳过状态卡片注册", "info")
        return False

    try:
        register_status(icon, "BiliSubscribe", STATUS_ITEMS)
    except Exception as e:
        _log(f"BiliSubscribe：状态卡片注册失败（{e}）")
        return False
    return True
