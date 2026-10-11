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

"""内置游戏白名单：不是"人工审核"，而是**只有符合白名单的才推**。

背景：游戏官方号的动态里混着大量推广、联动、抽奖，绝大多数是群里不想看的。
所以这里按游戏给每个官方号定一套**白名单栏目**：

- 命中白名单（固定栏目：海特洛特别资讯、活动日历……）→ **直接推群**；
- 没命中的动态 → **过滤掉**（不推群）。要不要留一份"待定"，由配置
  ``queue_unmatched`` 决定：默认直接过滤，开了就进待定队列、等你手动 ``可以推送``；
- 投稿视频不受影响，照常自动推送（视频本来就是官方号的主内容）。

规则按**游戏**写，而不是让管理员手工维护一份 UID 名单：各游戏官方号的发帖习惯
不一样（有的图文、有的专栏、有的把公告做进图里），口径自然不同；订阅了某游戏的
官方号，就自动套用该游戏的规则。要新增/调整游戏，改下面 ``GAMES`` 即可：

    "崩铁": GamePreset(
        name="崩铁",
        uids={"1340190821": "崩坏星穹铁道"},
        pass_keywords=("活动日历", "角色档案"),
    )
"""
from typing import Callable, Dict, List, Optional

from gsuid_core.logger import logger

# 一条动态的三种处理方式
PUSH = "push"        # 命中白名单：直接推群
PENDING = "pending"  # 没命中：过滤；配置里把「未命中的动态进待定队列」打开则进队列
FILTER = "filter"    # 明确丢弃：连待定都不进（纯广告/抽奖这类）


class GamePreset:
    """一个游戏的官方号 + 白名单规则。"""

    def __init__(
        self,
        name: str,
        uids: Dict[str, str],
        pass_keywords: "tuple[str, ...]" = (),
        decide: Optional[Callable[[Dict], str]] = None,
        note: str = "",
    ):
        self.name = str(name)
        # {UID: 号的名字}，一个游戏可以有多个官方号
        self.uids = {str(k).strip(): (str(v).strip() or str(name)) for k, v in uids.items()}
        self.pass_keywords = tuple(str(k) for k in pass_keywords if str(k).strip())
        self.note = str(note or "")
        self._decide = decide

    def uid_of(self) -> List[str]:
        return list(self.uids)

    def match_keyword(self, parsed: Dict, extra_keywords: "tuple[str, ...]" = ()) -> Optional[str]:
        """命中了哪个白名单关键词？没命中返回 None。

        匹配范围是**标题 + 正文**（B站的固定栏目有的写在标题里，如
        「海特洛特别资讯 | 曳尔沐」「角色档案介绍 | 黑羽」，有的写在正文里）。
        """
        blob = f"{parsed.get('title') or ''}\n{parsed.get('text') or ''}"
        for kw in list(self.pass_keywords) + list(extra_keywords):
            if kw and kw in blob:
                return str(kw)
        return None

    def decide(self, parsed: Dict, extra_keywords: "tuple[str, ...]" = ()) -> str:
        """这条动态怎么处理：``PUSH`` / ``PENDING`` / ``FILTER``。

        挂了自定义 ``decide`` 的游戏走它自己的判断；否则用默认规则：
        标题/正文命中白名单栏目就直接推，其余交给调用方（默认过滤、
        开了待定队列就进队列）。
        ``extra_keywords`` 是管理员额外加的（``bili白名单``），对所有游戏生效。
        """
        if self._decide is not None:
            try:
                return str(self._decide(parsed) or PENDING)
            except Exception as e:
                logger.warning(f"B站订阅：《{self.name}》的自定义白名单规则出错（{e}），按未命中处理")
                return PENDING
        return PUSH if self.match_keyword(parsed, extra_keywords) else PENDING


# ---------------------------------------------------------------------------
# 内置游戏（目前只有异环）
# ---------------------------------------------------------------------------
GAMES: Dict[str, GamePreset] = {
    "异环": GamePreset(
        name="异环",
        # 官方号（UID 用搜索接口核对过：304 万粉、名字就叫「异环」的那个）
        uids={"3546636978489848": "异环"},
        pass_keywords=(
            "活动日历",
            "角色档案",
            "前瞻回顾",
            "更新公告",
            "海特洛特别资讯",
            "海特洛衣橱",
            "限定棋盘",
            "弧盘研募计划",
            "海特洛档案",
            "礼遇",
            "时装",
        ),
        note="公告与资讯都发在官方号动态里，图文、专栏、多图混排",
    ),
}


def find_by_uid(uid: str) -> Optional[GamePreset]:
    """这个 UID 是哪个游戏的官方号？不是则返回 None（不受白名单过滤）。"""
    target = str(uid or "").strip()
    if not target:
        return None
    for preset in GAMES.values():
        if target in preset.uids:
            return preset
    return None


def find_by_name(text: str) -> Optional[GamePreset]:
    """按名字找游戏（如「异环」）。"""
    target = str(text or "").strip()
    if not target:
        return None
    if target in GAMES:
        return GAMES[target]
    for preset in GAMES.values():
        if target == preset.name or target in preset.uids.values():
            return preset
    return None


def filter_notice(uid: str) -> str:
    """订阅时的提示：这个 UP 的动态要走白名单过滤。"""
    preset = find_by_uid(uid)
    if preset is None:
        return ""
    return (
        f"\n注意：这是《{preset.name}》官方号，动态按白名单过滤——"
        "命中固定栏目才推群，其它（推广、联动、抽奖等）过滤掉；投稿视频照常自动推送"
    )


def summary_lines() -> List[str]:
    """``bili过滤规则`` 展示用：内置了哪些游戏、白名单里有什么。"""
    if not GAMES:
        return ["（还没有内置任何游戏白名单）"]
    lines: List[str] = []
    for preset in GAMES.values():
        lines.append(f"《{preset.name}》")
        uids = "、".join(f"{name}（{uid}）" for uid, name in preset.uids.items())
        lines.append(f"  官方号：{uids}")
        lines.append(f"  白名单栏目：{'、'.join(preset.pass_keywords) or '（无）'}")
        if preset.note:
            lines.append(f"  说明：{preset.note}")
        lines.append("  命中栏目 → 直接推群；没命中 → 过滤（不开待定队列就不推）")
    return lines
