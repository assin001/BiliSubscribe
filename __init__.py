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

"""B站订阅插件（GsCore）。

订阅 UP 主后，插件定时轮询该 UP 的**视频投稿**与**动态**，一有更新就推送到
订阅它的群聊。

安装：把本目录整个放到 ``gsuid_core/plugins/BiliSubscribe/`` 下，重启 GsCore 生效。
数据：``gsuid_core/data/BiliSubscribe/{config.json, subscribe.json}``

说明：
- 动态接口（polymer feed/space）需要 WBI 签名 + buvid 指纹，部分时段还要求
  登录 Cookie，否则返回 -636；投稿列表接口可能返回 -352（风控）。
  两种情况都用 ``bili设置ck`` 配置 Cookie 解决。
- 单个 UP 首次检查只建立基线，不会把历史动态刷一屏。
- ``bili帮助`` 优先发插件目录里的 ``bilihelp.jpg``，``bili管理帮助`` 发
  ``biliadminhelp.jpg``（图不在就各自退回文字帮助）；管理帮助挂 pm=1，
  普通成员发不会回。
- 官方号（异环、卡拉彼丘这类）走**动态过滤**（见 ``core/games.py``）：规则是插件目录
  ``game_rules/`` 里的**外挂 JSON**（一个游戏一个文件），每套规则自己选口径 ——
  ``whitelist``（命中栏目才推，其余过滤）或 ``blacklist``（默认全推，只丢命中的屏蔽词）；
  订阅了就自动套用，视频不受影响。
- 推送画成**卡片图**（见 ``card.py``）：头部 + 标题 + 正文 + 首图；链接单独一条；
  其余图片**一律按原链接原样发**（同一条消息里，平台自己排版）——不拼图、不转码，
  动了图才还是动的；长文默认只发卡片预览，要看全文点链接
  （``推送全文`` 开关可让它再补发全文）。
"""
from typing import Dict, List, Optional

import asyncio
import re
import time

import aiohttp
from pathlib import Path

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import MessageSegment
from gsuid_core.sv import SV

from .bili import fulltext
from .bili import login as bili_login
from .bili.api import api
from .bili.dynamic import (
    extract_dynamic,
    is_animated_url,
    latest_id,
    select_new_dynamics,
)
from .core import adfilter
from .core import config as cfg
from .core import game_rules_db
from .core import games
from .core import pending
from .core import runtime
from .core import storage
from .paths import HELP_ADMIN_IMAGE, HELP_IMAGE
from .render import status_card
from .render.push import (
    build_dynamic_msgs,
    build_live_msgs,
    build_video_msgs,
    format_dynamic,
    format_live,
    format_video,
    forward_batches,
    push_to_groups,
    reply,
)

try:  # 定时器来自核心；极端情况下导入失败也不该让整个插件加载不了
    from gsuid_core.aps import scheduler
except Exception as _e:  # pragma: no cover
    scheduler = None
    logger.warning(f"B站订阅：定时器不可用（{_e}），自动轮询不会启动")

sv_bili = SV("B站订阅")
sv_bili_admin = SV("B站订阅管理", pm=1)

HELP = """【B站订阅】
订阅 UP 主后，一有新的视频投稿或动态更新就推送到本群。

bili订阅 <UID / 主页链接 / UP名>   订阅该 UP
bili订阅 <UID> at                 订阅并 @全体成员
bili取消订阅 <UID|UP名>           取消订阅
bili取消订阅 all                  清空本群订阅（需群主/管理员）
bili设置 <UID|UP名> [设置项]      改这条订阅推什么（见下）
bili列表                          查看本群订阅了哪些 UP
bili动态 <UID>                    看看该 UP 的最新动态（调试用）
bili帮助                          本帮助
bili管理帮助                      管理员命令（超级用户以上）

每条订阅可以**单独设**：开/关动态、开/关视频、开/关直播，
以及按类 @全体（动态@全体 / 视频@全体 / 直播@全体）。
例：bili设置 异环 关视频 开直播         不带设置项就是看现在怎么推。
开播提醒要管理员先把「推送直播」总开关打开（bili修改配置 直播 开）。
用名字订阅/取消时，**名字完全一样且只有一个**才会直接生效；
对不上或重名的，会列出候选让你用 UID 指定。
订阅首次只建基线，不会把历史动态刷一屏；
想补推订阅生效「之前」的历史动态，管理员可以用 bili推这条 <链接>。
"""

HELP_ADMIN = """【B站订阅 · 管理员】
以下命令需要超级用户以上权限（普通群命令见 bili帮助）。

Cookie 与配置：
bili登录                          扫码登录 B站，自动获取并写入 Cookie（推荐）
bili设置ck <Cookie>               手动设置 B站 Cookie（动态/投稿接口风控时必填）
bili配置                          查看当前配置
bili修改配置 <项> <值>            修改配置
bili检查更新                      立刻跑一轮更新检查

手动推送：
bili推这条 <链接|ID|cv号|BV号|短链> [订阅群] 手动补推一条动态或视频（默认推到本会话）
bili测试全文 <动态链接|ID|cv号|短链>   调试：看长文正文能抓到多少

动态过滤（游戏规则）：每套规则自己选口径——白名单=命中栏目才推、黑名单=默认全推只挡屏蔽词
bili过滤规则（bili游戏）          查看游戏规则：口径、栏目、规则文件在哪
bili游戏规则 [停用|启用 <规则名>] 停用 / 启用一套规则（bili游戏规则 重载 重新读文件）
bili过滤测试 <链接|ID>            给一条动态，看它会不会被推（只判断不发送）
bili白名单 <关键词|取消 关键词>   额外放行关键词：命中就直接推（对所有规则生效）
（规则是插件目录 game_rules/ 里的 JSON，一个游戏一个文件；删掉即停用）
待定队列（默认关，开了才有人工这一步）：
bili待定                          看待定列表
可以推送 <编号|全部>              推送到订阅群      bili跳过 <编号|全部>  过滤掉
bili待定 在这里                   把当前会话设为待定通知接收处
bili待定 目标 [clear]             查看 / 清空待定通知会话
bili待定 记录                     最近的拍板记录

配置项：间隔(秒) / 上限(单轮UP数) / 动态上限 / 视频 / 动态 / 直播 / 封面 / 卡片 / 短链 / at全体 /
投稿列表 / 全文 / 待定 / 休眠 / 休眠时段 / 缓存 / 广告
（例：bili修改配置 休眠 开、bili修改配置 休眠时段 23:30-07:00、bili修改配置 直播 开）
每条订阅的开关（哪个 UP 推什么、哪类 @全体）用 bili设置。
开关值可以写：开/关、true/false、1/0
也可以直接在 Web 控制台的「插件 → BiliSubscribe → 配置」里改，改完立即生效
"""

# 帮助图路径集中在 paths.py（跟着插件目录走，重装/挪目录都不用改路径）


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _help_image(path: Path) -> Optional[object]:
    """帮助图的图片段；图不在（或读不了）就返回 None，让调用方退回文字帮助。"""
    try:
        if path.is_file():
            # 传 Path：核心会自己读文件并转 base64，不用插件侧再编码
            return MessageSegment.image(path)
    except Exception as e:
        logger.warning(f"B站订阅：帮助图 {path.name} 读不了（{e}），这条退回文字帮助")
    return None


async def _send_help(bot: Bot, ev: Event) -> None:
    """群聊帮助：有 bilihelp.jpg 就发图（图已含全部群命令），否则发文字。"""
    img = _help_image(HELP_IMAGE)
    if img is not None:
        await reply(bot, ev, [img, MessageSegment.text("管理员命令：bili管理帮助")])
        return
    await reply(bot, ev, HELP)


async def _send_admin_help(bot: Bot, ev: Event) -> None:
    """管理帮助：有 biliadminhelp.jpg 就发图，否则发文字（图里已写"见 bili帮助"）。"""
    img = _help_image(HELP_ADMIN_IMAGE)
    if img is not None:
        await reply(bot, ev, [img, MessageSegment.text("群聊命令：bili帮助")])
        return
    await reply(bot, ev, HELP_ADMIN)


def _args(ev: Event) -> str:
    """取命令关键词之后的参数。

    正常情况下 ``ev.text`` 已经是关键词后的内容；个别版本会保留关键词或前缀，
    这里做一次兜底剥离，避免把 "bili订阅" 当成 UP 名去搜。
    """
    text = str(getattr(ev, "text", "") or "").strip()
    cmd = str(getattr(ev, "command", "") or "").strip()
    if cmd and text.startswith(cmd):
        text = text[len(cmd):].strip()
    if not text:
        raw = str(getattr(ev, "raw_text", "") or "").strip()
        if cmd and raw.startswith(cmd):
            text = raw[len(cmd):].strip()
        elif raw and raw != cmd:
            text = raw
    return text


def _mask_cookie(cookie: str) -> str:
    ck = str(cookie or "")
    if not ck:
        return "未配置"
    if len(ck) <= 12:
        return ck[:2] + "***"
    return f"{ck[:6]}***{ck[-4:]}（{len(ck)} 字符）"


def _split_flags(arg: str) -> "tuple[str, bool]":
    """把 ``<UID> at`` 这类参数拆成 (目标, 是否@全体)。"""
    at_all = False
    rest: List[str] = []
    for token in str(arg or "").replace("　", " ").split():
        if token.lower() in ("at", "atall", "@all", "全体", "at全体", "@全体"):
            at_all = True
        else:
            rest.append(token)
    return "".join(rest), at_all


async def _list_text(gid: str) -> str:
    gid = str(gid)
    subs = await storage.load_subs()
    uids = storage.uids_in_group(subs, gid)
    if not uids:
        # 空列表最容易看不出原因（群号对不对？数据源读到没？），所以把排查信息一起打出来
        lines = [
            f"本群还没有订阅任何 UP（本群群号 {gid}）",
            "用法：bili订阅 <UID 或 UP名>",
            f"数据源：{_storage_text()}",
            _storage_counts(await storage.stats()),
        ]
        pairs = [
            (u, str(g))
            for u, info in subs.items()
            for g in (info.get("groups") or {})
        ]
        if pairs:
            lines.append(
                f"注意：本次读到的订阅里，其它群共 {len(pairs)} 条 / "
                f"{len({g for _, g in pairs})} 个群 —— 如果这个群确实订过，"
                "多半是数据源没读全（把这条发给我）"
            )
        return "\n".join(lines)
    lines = [f"本群订阅（{len(uids)} 个 UP）："]
    for i, uid in enumerate(uids, 1):
        info = subs.get(uid) or {}
        name = info.get("name") or "未知UP"
        # 每条订阅自己的开关：全开且不@时 describe_flags 返回空串，列表就不啰嗦
        entry = storage.group_entry(info, gid)
        flags = storage.describe_flags(entry)
        preset = games.find_by_uid(uid)  # 官方号：动态要先过审
        tag = f"〔{preset.name}·动态走白名单〕" if preset else ""
        lines.append(
            f"{i}. {name}（{uid}）{tag}" + (f"  {flags}" if flags else "")
        )
    lines.append("\n改某条怎么推：bili设置 <UID|UP名> [设置项]")
    lines.append("取消订阅：bili取消订阅 <UID>")
    return "\n".join(lines)


def _storage_text() -> str:
    """订阅数据存在哪 —— 排查「控制台数据库管理页怎么没有这张表」时看这一行。

    走后端是核心数据库时，表会出现在控制台的「数据库管理」里（前提是
    ``models.py`` 里的 Admin 注册成功，见那里的注释）；否则是本地 JSON。
    """
    if str(getattr(storage, "BACKEND", "")) != "database":
        return "本地 JSON（data/BiliSubscribe/subscribe.json）"
    from .core import models

    if getattr(models, "ADMIN_REGISTERED", False):
        return "GsCore 数据库，表 BiliSubscribeSub 已挂到控制台「数据库管理」"
    return "GsCore 数据库，但表 BiliSubscribeSub **没**挂上控制台（核心缺 webconsole.mount_app）"


def _storage_counts(stat: Dict) -> str:
    """两个后端各有几个订阅 —— 出现"刚订阅成功、列表却是空的"时看这一行。"""
    db = int(stat.get("db", -1))
    js = int(stat.get("json", 0))
    err = str(stat.get("db_error") or "")
    db_text = "读不到" if db < 0 else f"{db} 个 UP"
    if db < 0 and err:
        db_text = f"读不到（{err}）"
    line = f"  · 数据库 {db_text} / 本地 JSON {js} 个 UP"
    if stat.get("pending"):
        line += (
            "\n  ⚠️ 上次写数据库失败过，有订阅兜底写进了本地 JSON"
            "（读取时会并进来，并自动补写回数据库）"
        )
    elif db == 0 and js:
        line += (
            "\n  ⚠️ 数据库是空的但本地 JSON 有数据（上次写库失败兜底过）；"
            "列表已按 JSON 显示，并会把数据补写回数据库"
        )
    return line


async def _config_text() -> str:
    conf = cfg.load_config()
    stat = await storage.stats()
    return "\n".join(
        [
            "【B站订阅配置】",
            f"Cookie：{_mask_cookie(conf.get('bilibili_cookie'))}",
            f"轮询间隔：{conf.get('check_interval')} 秒",
            f"单轮检查UP上限：{conf.get('max_uid_per_round')}",
            f"单轮补推动态上限：{conf.get('max_push_per_round')}",
            f"推送视频：{'开' if conf.get('notify_video') else '关'}"
            f" / 动态：{'开' if conf.get('notify_dynamic') else '关'}"
            f" / 直播：{'开' if conf.get('notify_live') else '关'}",
            f"带封面图：{'开' if conf.get('push_cover') else '关'}"
            f" / 推送卡片：{'开' if conf.get('push_card', True) else '关'}"
            f" / 补发其余图：{'开' if conf.get('push_extra_images', True) else '关'}"
            "（一律原链接原样发：不拼图、不转码）"
            f" / 全局@全体：{'开' if conf.get('push_at_all') else '关'}",
            f"额外检查投稿列表：{'开' if conf.get('check_video_list') else '关'}"
            f" / 长文补发全文：{'开' if conf.get('push_full_text') else '关'}"
            f"（上限 {conf.get('full_text_limit')} 字）",
            f"定时休眠：{'开' if conf.get('sleep_mode', True) else '关'}"
            f"（{conf.get('sleep_range') or '01:00-07:30'}）"
            f" / 图片缓存：{conf.get('cache_ttl_days') or 15} 天",
            f"带货/广告过滤：{'开' if conf.get('ad_filter', True) else '关'}"
            "（只对非官方号，改单条用 bili设置）",
            f"订阅数据：{_storage_text()}",
            _storage_counts(stat),
            f"\n游戏规则：{games.status_line()}"
            f"（外挂 JSON：{games.rules_dir()}）"
            f" / 额外放行关键词：{len(cfg.get_list('auto_push_keywords'))} 个"
            f"\n白名单口径的「其余」动态"
            f"{'进待定队列' if conf.get('queue_unmatched') else '直接过滤'}"
            f" / 待定通知会话：{len(cfg.get_list('review_targets'))} 处"
            "\n（哪些 UP 走哪套规则由 game_rules/*.json 决定，见 bili过滤规则）",
            f"配置来源：{cfg.config_source()}",
            "修改：bili修改配置 <项> <值>",
        ]
    )


# ---------------------------------------------------------------------------
# 命令：订阅 / 取消 / 列表 / 查看动态
# ---------------------------------------------------------------------------
@sv_bili.on_command(("bili订阅", "B站订阅"), block=True)
async def _cmd_subscribe(bot: Bot, ev: Event):
    arg = _args(ev)
    gid = str(ev.group_id or "")
    low = arg.lower().replace(" ", "")

    if low in ("", "帮助", "help", "?"):
        await _send_help(bot, ev)
        return
    if low in ("列表", "list", "ls"):
        if not gid:
            await reply(bot, ev, "请在群聊里查看订阅列表")
            return
        await reply(bot, ev, await _list_text(gid))
        return
    if low in ("配置", "config"):
        if getattr(ev, "user_pm", 6) > 1:
            await reply(bot, ev, "只有超级用户以上可以查看配置")
            return
        await reply(bot, ev, await _config_text())
        return
    if not gid:
        await reply(bot, ev, "请在群聊里使用订阅命令（订阅结果会推送到群）")
        return

    target, at_all = _split_flags(arg)
    if not target:
        await _send_help(bot, ev)
        return

    uid = storage.parse_uid_arg(target)
    matched_by_name = False
    search_name = ""
    if not uid:
        # 不是 UID / 链接，就按关键词搜 UP
        users = await api.search_user(target)
        if users is None:
            await reply(bot, ev, f"搜索「{target}」失败（可能被风控），请直接使用数字 UID 订阅")
            return
        if not users:
            await reply(bot, ev, f"没搜到叫「{target}」的 UP，请换个关键词或直接用 UID")
            return
        # 名字完全对得上、且只有一条 → 直接订阅，不用再让用户抄一遍 UID
        hit = storage.match_by_name(users, target)
        if hit and str(hit.get("uid") or ""):
            uid = str(hit["uid"])
            search_name = str(hit.get("name") or "")
            matched_by_name = True
            logger.info(f"B站订阅：按名字精确匹配到「{search_name}」（UID {uid}），直接订阅")
        else:
            lines = [f"搜索「{target}」的结果，请用 UID 订阅："]
            for u in users:
                fans = f"，{u['fans']} 粉丝" if u.get("fans") else ""
                lines.append(f"{u['name']}（UID {u['uid']}{fans}）")
            lines.append("\n例：bili订阅 " + users[0]["uid"])
            await reply(bot, ev, "\n".join(lines))
            return

    name = ""
    card = await api.get_user_card(uid)
    if card:
        name = str(card.get("name") or "")
    if not name:
        name = search_name  # 用户卡拿不到时用搜索结果里的名字兜着

    changed = await storage.add_sub(
        uid,
        gid,
        bot_id=str(getattr(ev, "bot_id", "") or ""),
        bot_self_id=str(getattr(ev, "bot_self_id", "") or ""),
        at_all=at_all,
    )
    # 先落订阅再写名字：merge_state 只更新已存在的 UID
    if name:
        await storage.merge_state({uid: {"name": name}})
    label = f"{name}（{uid}）" if name else f"UID {uid}"
    if changed:
        msg = [f"已订阅 {label}，该 UP 的视频与动态更新会推送到本群"]
        if matched_by_name:
            msg.append(f"（名字完全匹配，直接按 UID {uid} 订上了）")
        if at_all:
            msg.append("该订阅会 @全体成员")
        if not card:
            msg.append("（暂时没取到 UP 名字，稍后轮询到会自动补上）")
        # 官方号（异环这类）：说清楚它的动态要过白名单，免得以为插件没推
        notice = games.filter_notice(uid)
        if notice:
            msg.append(notice)
        await reply(bot, ev, "\n".join(msg))
    else:
        await reply(bot, ev, f"本群已经订阅过 {label} 了\n查看列表：bili列表")


@sv_bili.on_command(("bili取消订阅", "B站取消订阅"), block=True)
async def _cmd_unsubscribe(bot: Bot, ev: Event):
    arg = _args(ev).strip()
    gid = str(ev.group_id or "")
    if not gid:
        await reply(bot, ev, "请在群聊里使用")
        return

    if arg.lower() in ("all", "全部", "清空"):
        if getattr(ev, "user_pm", 6) > 3:
            await reply(bot, ev, "只有群主/管理员可以清空本群订阅")
            return
        n = await storage.clear_group(gid)
        await reply(bot, ev, f"已清空本群订阅（{n} 个 UP）" if n else "本群本来就没有订阅")
        return

    if not arg:
        await reply(bot, ev, "用法：bili取消订阅 <UID|UP名>\n" + await _list_text(gid))
        return

    uid = storage.parse_uid_arg(arg)
    matched_by_name = False
    if not uid:
        # 不是 UID/链接：在**本群已订阅的**里按名字找（不用再打 B站接口，也不会被风控）。
        # 名字完全一样且只有一条就直接取消，否则把本群订阅列出来让用户挑 UID。
        subs = await storage.load_subs()
        rows = [
            {"uid": u, "name": str(info.get("name") or "")}
            for u, info in subs.items()
            if storage.group_entry(info, gid)
        ]
        hit = storage.match_by_name(rows, arg)
        if hit and str(hit.get("uid") or ""):
            uid = str(hit["uid"])
            matched_by_name = True
            logger.info(
                f"B站订阅：群 {gid} 取消订阅按名字精确匹配到「{hit.get('name')}」（UID {uid}）"
            )
        else:
            hint = (
                "本群没有叫「" + arg + "」的订阅（按名字要**完全一样**）"
                if rows
                else "本群还没有订阅"
            )
            await reply(bot, ev, f"{hint}\n\n" + await _list_text(gid))
            return

    ok = await storage.remove_sub(uid, gid)
    if ok:
        extra = "（名字完全匹配）" if matched_by_name else ""
        await reply(bot, ev, f"已取消订阅 UID {uid}{extra}")
    else:
        await reply(bot, ev, f"本群没有订阅 UID {uid}")


SET_HELP = """改某条订阅推什么（只对本群这条订阅生效）：
bili设置 <UID|UP名> [设置项...]
  设置项：开动态 / 关动态、开视频 / 关视频、开直播 / 关直播、全开 / 全关
  @全体：动态@全体、视频@全体、直播@全体（取消就加"取消"前缀）
  不带设置项 = 看这条订阅现在怎么推
例：bili设置 异环 关视频 开直播
    bili设置 3546636978489848 全开 动态@全体"""

ALL_ON_TEXT = "动态/视频/直播全开、不@全体"


def _find_in_group(subs: Dict, gid: str, arg: str) -> "tuple[str, str]":
    """在本群已订阅里找一条：先当 UID 认，再按名字**完全匹配**。

    返回 ``(uid, "")`` 或 ``("", 失败原因)``。名字规则和 ``bili取消订阅`` 一致：
    完全同名且只有一条才算命中（重名让人来挑，不替用户猜）。
    """
    uid = storage.parse_uid_arg(arg)
    if uid:
        if uid in ((subs.get(uid) or {}).get("groups") or {}):
            return uid, ""
        return "", f"本群没有订阅 UID {uid}"
    rows = [
        {"uid": u, "name": str(info.get("name") or "")}
        for u, info in subs.items()
        if storage.group_entry(info, gid)
    ]
    hit = storage.match_by_name(rows, arg)
    if hit and str(hit.get("uid") or ""):
        return str(hit["uid"]), ""
    if not rows:
        return "", "本群还没有订阅任何 UP"
    return "", f"本群没有叫「{arg}」的订阅（按名字要完全一样，也可以用 UID）"


@sv_bili.on_command(("bili设置", "bili订阅设置"), block=True)
async def _cmd_sub_set(bot: Bot, ev: Event):
    """按 UP 改推送开关与 @全体：同一个 UP 在不同群可以配得不一样。"""
    gid = str(ev.group_id or "")
    if not gid:
        await reply(bot, ev, "请在群聊里使用（开关是按群存的）")
        return
    parts = _args(ev).replace("　", " ").split()
    if not parts:
        await reply(bot, ev, SET_HELP)
        return

    subs = await storage.load_subs()
    uid, err = _find_in_group(subs, gid, parts[0])
    if not uid:
        await reply(bot, ev, err)
        return
    info = subs.get(uid) or {}
    name = str(info.get("name") or uid)
    entry = storage.group_entry(info, gid)

    words = parts[1:]
    if not words:
        await reply(
            bot,
            ev,
            f"{name}（{uid}）在本群：{storage.describe_flags(entry) or ALL_ON_TEXT}\n\n"
            + SET_HELP,
        )
        return

    flags = storage.parse_flags(words)
    if not flags:
        await reply(bot, ev, f"认不出的设置项：{' '.join(words)}\n\n{SET_HELP}")
        return
    if not await storage.set_flags(uid, gid, flags):
        await reply(bot, ev, f"没能改到 {name}（{uid}）的订阅，可能刚被取消了")
        return
    after = dict(entry)
    after.update({key: bool(value) for key, value in flags.items()})
    await reply(
        bot,
        ev,
        f"已设置 {name}（{uid}）：{storage.describe_flags(after) or ALL_ON_TEXT}",
    )


# 注意：不要在这里注册 "bili订阅列表" / "bili订阅帮助" 这类关键词，
# 它们会同时命中 on_command("bili订阅")，导致重复回复；
# 这类写法由上面对命令参数的解析统一处理。
@sv_bili.on_fullmatch(("bili列表",), block=True)
async def _cmd_list(bot: Bot, ev: Event):
    gid = str(ev.group_id or "")
    if not gid:
        await reply(bot, ev, "请在群聊里查看订阅列表")
        return
    await reply(bot, ev, await _list_text(gid))


@sv_bili.on_command(("bili动态", "bili查动态"), block=True)
async def _cmd_check_dynamic(bot: Bot, ev: Event):
    uid = storage.parse_uid_arg(_args(ev))
    if not uid:
        await reply(bot, ev, "用法：bili动态 <UID>")
        return
    items = await api.get_dynamic(uid, 5)
    if items is None:
        await reply(
            bot,
            ev,
            "拉取动态失败：可能被风控或需要登录 Cookie\n"
            "管理员可用 bili设置ck <Cookie> 配置后再试",
        )
        return
    if not items:
        await reply(bot, ev, f"UID {uid} 没有拉到动态（可能是空号或动态不可见）")
        return
    for item in items[:2]:
        parsed = extract_dynamic(item)
        out = format_dynamic(parsed)
        await reply(bot, ev, out["text"])


@sv_bili.on_fullmatch(("bili帮助",), block=True)
async def _cmd_help(bot: Bot, ev: Event):
    await _send_help(bot, ev)


# 管理帮助挂在 sv_bili_admin（pm=1）上：普通成员发了不会回，
# 免得群里刷一屏用不上的命令；用户帮助里留了一行指路。
@sv_bili_admin.on_fullmatch(("bili管理帮助", "bili管理员帮助"), block=True)
async def _cmd_help_admin(bot: Bot, ev: Event):
    await _send_admin_help(bot, ev)


# ---------------------------------------------------------------------------
# 命令：管理员（Cookie / 配置 / 手动检查）
# ---------------------------------------------------------------------------
_SET_KEYS = {
    "间隔": "check_interval",
    "interval": "check_interval",
    "上限": "max_uid_per_round",
    "轮询上限": "max_uid_per_round",
    "maxuid": "max_uid_per_round",
    "动态上限": "max_push_per_round",
    "单轮动态": "max_push_per_round",
    "limit": "max_push_per_round",
    "视频": "notify_video",
    "video": "notify_video",
    "动态": "notify_dynamic",
    "dynamic": "notify_dynamic",
    "直播": "notify_live",
    "live": "notify_live",
    "封面": "push_cover",
    "cover": "push_cover",
    "at全体": "push_at_all",
    "atall": "push_at_all",
    "投稿列表": "check_video_list",
    "视频列表": "check_video_list",
    "videolist": "check_video_list",
    "全文": "push_full_text",
    "fulltext": "push_full_text",
    "多图": "push_extra_images",
    "extraimages": "push_extra_images",
    "全文字数": "full_text_limit",
    "fulltextlimit": "full_text_limit",
    "转发": "full_text_node",
    "合并转发": "full_text_node",
    "forward": "full_text_node",
    "动图": "gif_shrink",
    "gif": "gif_shrink",
    "卡片": "push_card",
    "card": "push_card",
    "短链": "push_short_link",
    "shortlink": "push_short_link",
    "待定": "queue_unmatched",
    "待定队列": "queue_unmatched",
    "queue": "queue_unmatched",
    "queueunmatched": "queue_unmatched",
    "休眠": "sleep_mode",
    "sleep": "sleep_mode",
    "休眠时段": "sleep_range",
    "sleeprange": "sleep_range",
    "缓存": "cache_ttl_days",
    "cache": "cache_ttl_days",
    "广告": "ad_filter",
    "广告过滤": "ad_filter",
    "带货": "ad_filter",
}
_INT_KEYS = {
    "check_interval",
    "max_uid_per_round",
    "max_push_per_round",
    "full_text_limit",
    "cache_ttl_days",
}


def _valid_sleep_range(text: str) -> bool:
    """休眠时段格式校验（实现在 config.py，方便单独测）。"""
    return cfg.valid_sleep_range(text)


def _in_sleep(range_str: str) -> bool:
    """现在是否处于休眠时段（实现在 config.py，方便单独测）。"""
    return cfg.in_sleep_range(range_str)


@sv_bili_admin.on_fullmatch(("bili登录", "B站登录"), block=True)
async def _cmd_login(bot: Bot, ev: Event):
    """扫码登录：发二维码，扫完自动把 Cookie 写进配置。"""
    await bili_login.start_login(bot, ev)


@sv_bili_admin.on_command(("bili设置ck", "bili设置cookie"), block=True)
async def _cmd_set_cookie(bot: Bot, ev: Event):
    arg = _args(ev).strip()
    if not arg:
        cur = _mask_cookie(cfg.get("bilibili_cookie"))
        await reply(
            bot,
            ev,
            f"当前 Cookie：{cur}\n用法：bili设置ck <完整Cookie>\n清空：bili设置ck clear",
        )
        return
    if arg.lower() in ("clear", "清空", "删除", "none", "reset"):
        cfg.set_value("bilibili_cookie", "")
        await reply(bot, ev, "已清空 Cookie")
        return
    cfg.set_value("bilibili_cookie", arg)
    await reply(bot, ev, f"已保存 Cookie（{len(arg)} 字符），动态接口会立刻用上")


@sv_bili_admin.on_fullmatch(("bili配置",), block=True)
async def _cmd_show_config(bot: Bot, ev: Event):
    await reply(bot, ev, await _config_text())


@sv_bili_admin.on_command(("bili修改配置",), block=True)
async def _cmd_set_config(bot: Bot, ev: Event):
    parts = _args(ev).split()
    if len(parts) < 2:
        await reply(bot, ev, "用法：bili修改配置 <项> <值>\n项见 bili配置")
        return
    key_raw, value_raw = parts[0], parts[1]
    key = _SET_KEYS.get(key_raw.lower()) or _SET_KEYS.get(key_raw)
    if key_raw.lower() in ("拼图", "网格", "grid", "imagegrid"):
        # 拼图功能已删除：留一句说明，比回"不认识的配置项"清楚
        await reply(
            bot,
            ev,
            "拼图功能已经删掉了：配图一律按 B站原链接一张张发（同一条消息里，"
            "平台自己会排版），不再合成网格图",
        )
        return
    if not key:
        await reply(bot, ev, f"不认识的配置项：{key_raw}\n可用：{'、'.join(sorted(set(_SET_KEYS)))}")
        return

    if key == "sleep_range":
        if not _valid_sleep_range(value_raw):
            await reply(
                bot,
                ev,
                "休眠时段格式：HH:MM-HH:MM，例如 01:00-07:30（跨天写 23:30-07:00）",
            )
            return
        cfg.set_value("sleep_range", value_raw.strip())
        await reply(bot, ev, f"已设置 休眠时段 = {value_raw.strip()}")
        return

    if key in _INT_KEYS:
        if not value_raw.isdigit():
            await reply(bot, ev, f"{key_raw} 需要是数字（秒/条数）")
            return
        value = int(value_raw)
        if key == "check_interval":
            value = max(60, value)
        else:
            value = max(1, value)
    else:
        value = cfg.parse_bool(value_raw)
        if value is None:
            await reply(bot, ev, "开关值请用：开/关、true/false、1/0")
            return

    cfg.set_value(key, value)
    await reply(bot, ev, f"已设置 {key} = {value}")


@sv_bili_admin.on_fullmatch(("bili检查更新", "bili更新检查"), block=True)
async def _cmd_check_now(bot: Bot, ev: Event):
    await reply(bot, ev, "开始检查更新…")
    n = await check_all()
    await reply(bot, ev, f"检查完成，更新了 {n} 个 UP 的记录" if n else "检查完成，没有更新")


# ---------------------------------------------------------------------------
# 命令：动态过滤（官方号白名单）与待定队列
# ---------------------------------------------------------------------------
def _decide_how(preset: Optional[games.GamePreset], kind: str, parsed: Dict) -> str:
    """这条动态怎么处理：视频永远直接推，其它交给所在游戏的规则判断。

    规则是**外挂 JSON**（插件目录的 ``game_rules/``，每个游戏一个文件：官方号 +
    口径 + 放行栏目 + 屏蔽关键词），所以**订阅了官方号就自动按该游戏的口径过滤**，
    不需要管理员手动维护名单。
    """
    if kind == "video" or preset is None:
        return games.PUSH
    return preset.decide(parsed, tuple(cfg.get_list("auto_push_keywords")))


def _short_desc(parsed: Dict) -> str:
    text = " ".join(str(parsed.get("title") or parsed.get("text") or "").split())
    return text if len(text) <= 30 else text[:30] + "…"


async def _resolve_seq(arg: str):
    """把命令参数解析成待定编号；返回 ``(seq, 提示文案)``，解析不了时 seq 为 None。"""
    items = await pending.pending_items()
    if not items:
        return None, _pending_hint()
    if arg.isdigit():
        if any(str(i.get("seq")) == arg for i in items):
            return arg, ""
        return None, f"没找到待定项 #{arg}"
    if len(items) == 1:
        return str(items[0].get("seq")), ""
    lines = [f"有 {len(items)} 条待定，请指定编号（也可以写 全部）："]
    for item in items[:10]:
        lines.append(
            f"#{item.get('seq')} {item.get('name') or ''}｜{_short_desc(item.get('parsed') or {})}"
        )
    return None, "\n".join(lines)


async def _do_approve(bot: Bot, ev: Event):
    arg = _args(ev).strip().lower()
    if arg in ("all", "全部", "所有", "all_all"):
        _, _, msg = await pending.approve_all()
        await reply(bot, ev, msg)
        return
    seq, hint = await _resolve_seq(arg)
    if seq is None:
        await reply(bot, ev, hint)
        return
    _, msg = await pending.approve(seq)
    await reply(bot, ev, msg)


async def _do_skip(bot: Bot, ev: Event):
    arg = _args(ev).strip().lower()
    if arg in ("all", "全部", "所有"):
        _, _, msg = await pending.skip_all()
        await reply(bot, ev, msg)
        return
    seq, hint = await _resolve_seq(arg)
    if seq is None:
        await reply(bot, ev, hint)
        return
    _, msg = await pending.skip(seq)
    await reply(bot, ev, msg)


def _pending_hint() -> str:
    """待定队列空着 / 压根没开时的提示，顺便说清现在的口径。"""
    if not cfg.get("queue_unmatched"):
        return (
            "待定队列是关着的：没命中白名单的动态已经直接过滤，不推群也不通知。\n"
            "想把它们捞回来看看：把「未命中的动态进待定队列」打开"
            "（bili修改配置 待定 开，或到 Web 控制台），再说一次「bili待定 在这里」指定通知会话"
        )
    return "当前没有待定的动态\n（游戏规则见 bili过滤规则）"


@sv_bili_admin.on_command(("bili待定", "bili审核", "bili待审核"), block=True)
async def _cmd_pending(bot: Bot, ev: Event):
    """待定队列：没命中白名单的动态（开了「未命中的动态进待定队列」才有）。

    一个入口带子命令，省得再注册一堆互相包含的关键词（那种会踩到同一条消息命中
    两个触发器、回两遍的坑）：

    - ``bili待定``：看待定列表
    - ``bili待定 在这里``：把当前会话设为通知接收处
    - ``bili待定 目标 [clear]``：查看 / 清空通知会话
    - ``bili待定 记录``：最近的拍板记录
    """
    arg = _args(ev).strip()
    low = arg.lower().replace(" ", "")

    if low in ("", "列表", "list", "ls"):
        items = await pending.pending_items()
        if not items:
            await reply(bot, ev, _pending_hint())
            return
        lines = [f"待定动态 {len(items)} 条（白名单没命中）："]
        for item in items[:20]:
            parsed = item.get("parsed") or {}
            lines.append(
                f"#{item.get('seq')} {item.get('name') or ''} "
                f"{parsed.get('action') or ''}｜{_short_desc(parsed)}"
            )
        lines.append("\n推送：可以推送 <编号|全部>    过滤：bili跳过 <编号|全部>")
        await reply(bot, ev, "\n".join(lines))
        return

    if low in ("在这里", "here", "这里"):
        target = pending.format_target(
            str(getattr(ev, "bot_id", "") or ""),
            str(getattr(ev, "user_type", "") or "group"),
            str(ev.group_id or ev.user_id or ""),
        )
        targets = cfg.get_list("review_targets")
        if target in targets:
            await reply(bot, ev, f"这个会话已经在收待定通知了：{target}")
            return
        targets.append(target)
        cfg.set_value("review_targets", targets)
        extra = "" if cfg.get("queue_unmatched") else "\n（提醒：待定队列现在是关的，白名单外的动态会直接过滤）"
        await reply(bot, ev, f"已设置：待定通知会发到本会话（{target}）{extra}")
        return

    if low.startswith("目标"):
        if any(k in low for k in ("clear", "清空", "重置", "none")):
            cfg.set_value("review_targets", [])
            await reply(bot, ev, "已清空待定通知会话")
            return
        targets = cfg.get_list("review_targets")
        body = "\n".join(f"- {t}" for t in targets) if targets else "（空）"
        await reply(
            bot,
            ev,
            f"待定通知会话：\n{body}\n\n"
            "在目标会话里发「bili待定 在这里」添加；「bili待定 目标 clear」清空",
        )
        return

    if low in ("记录", "history", "log"):
        rows = await pending.history(10)
        if not rows:
            await reply(bot, ev, "还没有拍板记录")
            return
        names = {"pushed": "已推送", "skipped": "已过滤", "expired": "已过期"}
        lines = ["最近的待定处理（新→旧）："]
        for row in rows:
            lines.append(
                f"{names.get(str(row.get('decision')), row.get('decision'))} "
                f"#{row.get('seq')} {row.get('name') or ''}｜{row.get('title') or ''}"
            )
        await reply(bot, ev, "\n".join(lines))
        return

    await reply(
        bot,
        ev,
        "用法：\n"
        "bili待定                     看待定列表\n"
        "bili待定 在这里              把当前会话设为待定通知接收处\n"
        "bili待定 目标 [clear]        查看 / 清空待定通知会话\n"
        "bili待定 记录                最近的拍板记录\n"
        "可以推送 <编号|全部> / bili跳过 <编号|全部>   推送 / 过滤",
    )


@sv_bili_admin.on_fullmatch(("可以推送", "bili可以推送"), block=True)
async def _cmd_approve_now(bot: Bot, ev: Event):
    await _do_approve(bot, ev)


@sv_bili_admin.on_command(("bili可以推送",), block=True)
async def _cmd_approve_seq(bot: Bot, ev: Event):
    await _do_approve(bot, ev)


@sv_bili_admin.on_fullmatch(("bili不推送", "不推送"), block=True)
async def _cmd_skip_now(bot: Bot, ev: Event):
    await _do_skip(bot, ev)


@sv_bili_admin.on_command(("bili跳过", "bili不推送"), block=True)
async def _cmd_skip_seq(bot: Bot, ev: Event):
    await _do_skip(bot, ev)


@sv_bili_admin.on_command(("bili白名单",), block=True)
async def _cmd_whitelist(bot: Bot, ev: Event):
    """额外放行关键词：命中就直接推（在游戏规则自己的栏目之外再补一层）。"""
    arg = _args(ev).strip()
    keywords = cfg.get_list("auto_push_keywords")
    if not arg:
        body = "\n".join(f"- {k}" for k in keywords) if keywords else "（空）"
        await reply(
            bot,
            ev,
            f"额外放行关键词：\n{body}\n\n"
            "添加：bili白名单 <关键词>    移除：bili白名单 取消 <关键词>\n"
            "标题或正文命中就直接推送（对所有规则生效，含黑名单口径；"
            "但挡不住游戏规则里的屏蔽词）；各游戏的栏目见 bili过滤规则",
        )
        return

    removing = any(k in arg for k in ("取消", "删除", "移除", "remove")) or arg.startswith("-")
    keyword = arg
    for token in ("取消", "删除", "移除", "remove", "-"):
        keyword = keyword.replace(token, "")
    keyword = keyword.strip()
    if not keyword:
        await reply(bot, ev, "关键词不能为空")
        return

    if removing:
        if keyword in keywords:
            keywords.remove(keyword)
            cfg.set_value("auto_push_keywords", keywords)
            await reply(bot, ev, f"已移除放行关键词「{keyword}」")
        else:
            await reply(bot, ev, f"「{keyword}」本来就不在放行关键词里")
        return

    if keyword in keywords:
        await reply(bot, ev, f"「{keyword}」已经在白名单里了")
        return
    keywords.append(keyword)
    cfg.set_value("auto_push_keywords", keywords)
    await reply(bot, ev, f"已添加放行关键词「{keyword}」：命中它的动态会直接推送")


@sv_bili_admin.on_command(("bili过滤测试", "bili过滤查询"), block=True)
async def _cmd_filter_check(bot: Bot, ev: Event):
    """过滤自检：给一条动态，告诉你它会不会被推（**不发送、不动游标**）。

    排查"这条明明该推怎么没推"用的：会打出标题/正文、规则的**口径**（白名单/黑名单）、
    命中哪个词、以及最终判定。判定为"过滤/丢弃"就说明要加词
    （游戏规则 JSON 或 ``bili白名单``）。
    """
    arg = _args(ev).strip()
    if not arg:
        await reply(bot, ev, "用法：bili过滤测试 <动态链接|动态ID|cv号|b23.tv短链>\n只判断，不发送")
        return

    ids = await _parse_dynamic_arg(arg)
    parsed: Optional[Dict] = None
    for did in (ids["id_str"], ids["opus_id"]):
        if not did:
            continue
        item = await api.get_detail_item(did)
        if item:
            parsed = extract_dynamic(item)
            break
    if not parsed:
        await reply(
            bot,
            ev,
            "没取到这条动态：可能被风控（bili设置ck 配 Cookie）、链接/ID 不对，或者这条已删",
        )
        return

    uid = str(parsed.get("mid") or "")
    preset = games.find_by_uid(uid)
    images = [u for u in (parsed.get("images") or []) if str(u or "").strip()]
    animated = [u for u in images if is_animated_url(u)]
    lines = [
        "【B站订阅·过滤自检】",
        f"作者：{parsed.get('uname') or '未知UP'}" + (f"（{uid}）" if uid else ""),
        f"类型：{parsed.get('action') or '未知'}",
        f"标题：{parsed.get('title') or '（这条没有标题）'}",
        f"正文：{_short_desc(parsed)}",
        f"图片：{len(images)} 张（动图 {len(animated)} 张）",
    ]
    if preset is None:
        lines.append("规则：这个 UP 不在任何启用中的游戏规则里 → 不受过滤")
        lines.append("判定：✅ 会直接推送")
    else:
        hit_block = preset.match_block(parsed)
        hit = preset.match_keyword(parsed, tuple(cfg.get_list("auto_push_keywords")))
        rule_file = preset.source or preset.name + ".json"
        lines.append(f"规则：《{preset.name}》［{preset.mode_text}］→ {rule_file}")
        if preset.pass_keywords:
            lines.append(f"　放行栏目：{'、'.join(preset.pass_keywords)}")
        if preset.block_keywords:
            lines.append(f"　屏蔽关键词：{'、'.join(preset.block_keywords)}")
        if hit_block:
            lines.append(f"命中「屏蔽」：{hit_block}")
            lines.append("判定：❌ 会被丢弃（连待定队列都不进）")
        elif hit:
            lines.append(f"命中「放行」：{hit}")
            lines.append("判定：✅ 会直接推送")
        elif preset.is_blacklist:
            lines.append("命中：（屏蔽关键词一个都没命中）")
            lines.append("判定：✅ 会直接推送（黑名单口径：默认全推）")
        else:
            lines.append("命中：（一个栏目都没命中）")
            lines.append("判定：❌ 会被过滤（不开待定队列就不推、不通知）")
            lines.append(
                f"要放行：栏目名加进规则文件（{games.rules_dir()} 里的 {rule_file}），"
                "或 bili白名单 <关键词>"
            )
    lines.append("")
    lines.append("（只判断，不发送、不动游标；想真发一条用 bili推这条）")
    await reply(bot, ev, "\n".join(lines))


def _rules_text() -> str:
    """``bili过滤规则`` / ``bili游戏规则`` 的正文（规则目录 + 每套规则 + 当前口径）。"""
    queue_on = bool(cfg.get("queue_unmatched"))
    enabled = len(cfg.get_list("game_rules_enabled"))
    return (
        "【B站订阅·动态过滤（游戏规则）】\n"
        f"规则目录：{games.rules_dir()}\n"
        "（一个游戏一个 JSON，文件名就是规则名；改完不用重启。"
        "新增游戏加个文件，游戏没了直接删文件）\n"
        f"启用中的规则：{enabled} 条（控制台「启用的游戏规则」里增删）\n"
        "规则内容也可以直接改 JSON，或在控制台「数据库管理 → 游戏过滤规则」里改\n\n"
        + "\n".join(games.summary_lines())
        + "\n白名单口径的规则：「其余」动态"
        + ("进待定队列（可以推送 <编号> 捞回来）" if queue_on else "直接过滤，不推群也不通知")
        + "\n　黑名单口径的规则不受它影响：默认全推，只丢命中的屏蔽词"
        + "\n额外放行关键词：bili白名单 <关键词>（对所有规则生效）"
        + "\n启停某套规则：bili游戏规则 停用/启用 <规则名>"
    )


@sv_bili_admin.on_fullmatch(("bili过滤规则", "bili游戏"), block=True)
async def _cmd_filter_rules(bot: Bot, ev: Event):
    """动态过滤规则：有哪些游戏规则、口径与栏目、规则文件在哪。

    规则本身是**外挂 JSON**（插件目录的 ``game_rules/``，一个游戏一个文件），
    订阅了官方号就自动套用，不需要管理员手工维护名单。
    """
    await game_rules_db.sync_now()
    games.reload()
    await reply(bot, ev, _rules_text())


@sv_bili_admin.on_fullmatch(("bili游戏规则", "bili规则"), block=True)
async def _cmd_game_rules_view(bot: Bot, ev: Event):
    """``bili游戏规则``（不带参数）：看有哪些规则、文件在哪、怎么启停。"""
    await game_rules_db.sync_now()
    games.reload()
    await reply(bot, ev, _rules_text())


@sv_bili_admin.on_command(("bili游戏规则",), block=True)
async def _cmd_game_rules(bot: Bot, ev: Event):
    """游戏规则：停用 / 启用 / 重载 / 同步 / 诊断（规则内容请改 JSON 或控制台的数据库页）。

    这里只改配置项「启用的游戏规则」，**不动 JSON** —— 两种方式等价，
    哪个顺手用哪个；游戏彻底没了就直接把那个 JSON 删掉。
    """
    parts = _args(ev).split()
    if not parts:
        await reply(bot, ev, _rules_text())
        return

    action = parts[0].lower()
    target = parts[1].strip() if len(parts) > 1 else ""

    if action in ("重载", "reload", "刷新"):
        games.reload()
        await reply(bot, ev, "已重新读取规则目录。\n\n" + _rules_text())
        return

    if action in ("同步", "sync"):
        did = await game_rules_db.sync_now()
        games.reload()
        await reply(
            bot,
            ev,
            ("已同步一轮（有改动，已落盘）\n\n" if did else "已同步一轮（两边一致，没改动）\n\n")
            + _rules_text(),
        )
        return

    if action in ("诊断", "diagnose", "自检", "状态"):
        await reply(bot, ev, await game_rules_db.diagnose())
        return

    if action not in ("停用", "启用", "disable", "enable", "关", "开"):
        await reply(
            bot,
            ev,
            "用法：\n"
            "· bili游戏规则                      查看规则\n"
            "· bili游戏规则 停用 <规则名>        停用一套规则\n"
            "· bili游戏规则 启用 <规则名>        重新启用\n"
            "· bili游戏规则 重载                 重新读 game_rules/ 里的 JSON\n"
            "· bili游戏规则 同步                 和数据库管理页对齐一轮\n"
            "· bili游戏规则 诊断                 看同步/数据库状态（控制台没数据时用）",
        )
        return

    if not target:
        await reply(bot, ev, "要给个规则名，例如：bili游戏规则 停用 异环")
        return

    preset = games.find_by_name(target)
    if preset is None:
        names = "、".join(games.all_presets()) or "（一个都没有）"
        await reply(bot, ev, f"找不到规则「{target}」\n目录里的规则：{names}")
        return

    # 规则名 = JSON 文件名（不带扩展名）：配置项「启用的游戏规则」就是按它来认的
    rule_name = preset.source[:-5] if preset.source.lower().endswith(".json") else preset.source
    rule_name = rule_name or preset.name
    turn_off = action in ("停用", "disable", "关")

    enabled = list(cfg.get_list("game_rules_enabled"))
    if turn_off:
        enabled = [x for x in enabled if x not in (rule_name, preset.name)]
    elif rule_name not in enabled:
        enabled.append(rule_name)
    if not cfg.set_value("game_rules_enabled", enabled):
        await reply(bot, ev, "写入配置失败：请到 Web 控制台改「启用的游戏规则」")
        return

    games.reload()

    await reply(
        bot,
        ev,
        f"已{'停用' if turn_off else '启用'}《{preset.name}》（规则 {rule_name}）\n"
        + (
            "停用后这个官方号的动态不再走规则过滤，和普通 UP 一样照单全推"
            if turn_off
            else f"启用后按{preset.mode_text}口径过滤：" + (
                "默认全推，命中屏蔽词才不推" if preset.is_blacklist else "命中放行栏目才推群"
            )
        )
        + "\n\n"
        + _rules_text(),
    )


async def _resolve_b23(text: str) -> str:
    """把 b23.tv 短链换成真实链接（手机分享复制的链接就是这种）。

    b23.tv 是 B 站的短链服务，会 302 跳到 t.bilibili.com / opus / video 等真实地址，
    这里跟一遍跳转拿到最终 URL，追加到原文后面交给正常解析。
    跳转失败就原样返回，不影响其它命令。
    """
    m = re.search(r"(?:https?://)?b23\.tv/[\w\-]+", text, re.I)
    if not m:
        return text
    short = m.group(0)
    url = short if short.lower().startswith("http") else "https://" + short
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                allow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                    )
                },
            ) as resp:
                real = str(resp.url)
        if real and "b23.tv" not in real:
            logger.info(f"[BiliSubscribe] 短链解析：{url} -> {real}")
            return f"{text} {real}"
        logger.warning(f"[BiliSubscribe] 短链 {url} 跳转结果异常：{real}")
    except Exception as e:  # 网络失败不致命，按原文继续解析
        logger.warning(f"[BiliSubscribe] 短链 {url} 解析失败：{e}")
    return text


async def _parse_dynamic_arg(arg: str) -> Dict:
    """从参数里认出动态或视频，五种形式都支持：

    - 动态链接 / 动态ID：``https://t.bilibili.com/123…``、``123…``
    - opus 链接：``https://www.bilibili.com/opus/123…``（**opus id 就是动态 id**）
    - 专栏：``https://www.bilibili.com/read/cv53144750``、``cv53144750``
    - 视频：``BV19tej6oE3H``、``av123456``、``https://www.bilibili.com/video/BV19tej6oE3H``
    - 短链：``https://b23.tv/0wyDLI4``（手机分享复制的那种，自动换成真实链接再认）

    返回 ``{"id_str": …, "cv_id": …, "opus_id": …, "bvid": …, "aid": …}``，
    认不出的字段是空串。
    """
    text = str(arg or "").strip()
    if re.search(r"b23\.tv/", text, re.I):
        text = await _resolve_b23(text)
    out = {"id_str": "", "cv_id": "", "opus_id": "", "bvid": "", "aid": ""}
    m = re.search(r"(?:t\.bilibili\.com|dynamic)/(\d{10,})", text) or re.search(r"^(\d{10,})$", text)
    if m:
        out["id_str"] = m.group(1)
    m = re.search(r"(?:read/cv|/cv)(\d+)", text) or re.search(r"^cv(\d+)$", text, re.I)
    if m:
        out["cv_id"] = m.group(1)
    m = re.search(r"/opus/(\d+)", text)
    if m:
        out["opus_id"] = m.group(1)
        out["id_str"] = out["id_str"] or m.group(1)
    # 视频：BV 号（12 位，区分大小写）或 av 号（老链接）
    m = re.search(r"(BV[0-9A-Za-z]{10})", text)
    if m:
        out["bvid"] = m.group(1)
    m = re.search(r"(?:^|[^0-9A-Za-z])av(\d+)", text, re.I)
    if m:
        out["aid"] = m.group(1)
    return out


@sv_bili_admin.on_command(("bili测试全文", "bili全文"), block=True)
async def _cmd_test_fulltext(bot: Bot, ev: Event):
    """调试：给一个动态链接 / 动态ID / cv号，看全文能抓到多少。"""
    arg = _args(ev).strip()
    if not arg:
        await reply(
            bot,
            ev,
            "用法：bili测试全文 <动态链接|动态ID|cv号|b23.tv短链>\n"
            "例：bili测试全文 https://t.bilibili.com/1150000000000000002",
        )
        return

    ids = await _parse_dynamic_arg(arg)
    parsed: Dict = {"title": "", "text": "", "kind": "article", **ids}
    if not any(ids.values()):
        await reply(
            bot,
            ev,
            "认不出参数，请给动态链接、动态ID（t.bilibili.com/xxx）、opus 链接、cv 号或 b23.tv 短链",
        )
        return

    parts, source = await fulltext.fetch(parsed, force=True)
    if not parts:
        await reply(
            bot,
            ev,
            "没抓到正文：可能这条不是长文、接口要 Cookie（bili设置ck）或接口被风控",
        )
        return
    text = "\n".join(str(p.get("text") or "") for p in parts if p.get("type") == "text")
    images = [p.get("url") for p in parts if p.get("type") == "image"]
    nodes = fulltext.build_nodes(parsed.get("title") or "", parts, parsed.get("url") or "")
    await reply(
        bot,
        ev,
        f"全文抓取成功\n来源：{source}\n正文 {len(text)} 字"
        f"（上限 {cfg.get('full_text_limit')} 字），配图 {len(images)} 张\n"
        f"合并转发会切成 {len(nodes)} 段"
        + (
            "（超过平台常见的转发上限，直接发时会自动改成按段并块发）"
            if len(nodes) > fulltext.MAX_NODES + 2
            else ""
        )
        + f"\n\n开头预览：\n{text[:200]}…",
    )


@sv_bili_admin.on_command(("bili推这条",), block=True)
async def _cmd_push_one(bot: Bot, ev: Event):
    """手动补推一条动态：订阅生效前的历史动态，或者想单独发一次的公告。"""
    parts = _args(ev).split()
    if not parts:
        await reply(
            bot,
            ev,
            "用法：bili推这条 <动态链接|动态ID|cv号|视频BV号|b23.tv短链> [订阅群]\n"
            "· 默认推到本会话\n"
            "· 加「订阅群」则推给订阅了这个 UP 的所有群\n"
            "例：bili推这条 https://www.bilibili.com/opus/1251145718800842753 订阅群\n"
            "    bili推这条 BV19tej6oE3H\n"
            "    bili推这条 https://b23.tv/0wyDLI4",
        )
        return

    # 手机分享文本是「【标题】 https://b23.tv/xxx」这种多段文字，
    # 挑出真正像链接 / ID 的那段当参数，别把标题当成参数。
    arg = next(
        (
            p
            for p in parts
            if re.search(
                r"(?:https?://)?(?:b23\.tv|bilibili\.com)/|BV[0-9A-Za-z]{10}|cv\d+|av\d+|^\d{10,}$",
                p,
                re.I,
            )
        ),
        parts[0],
    )
    to_subs = any(p.lower() in ("订阅群", "群", "群聊", "all") for p in parts)
    ids = await _parse_dynamic_arg(arg)
    if not any(ids.values()):
        await reply(
            bot,
            ev,
            "认不出参数。支持这几种：\n"
            "· 动态链接 / 动态ID（t.bilibili.com/xxx）、opus 链接（/opus/xxx）\n"
            "· 专栏（read/cv<号> 或 cv<号>）\n"
            "· 视频（BV号 / av号 / www.bilibili.com/video/BV…）\n"
            "· 短链（b23.tv/xxx，手机分享复制的那种）",
        )
        return

    # 视频（BV / av / 视频链接）：走投稿那条路，跟动态不是一回事
    if ids["bvid"] or ids["aid"]:
        video = await api.get_video_info(ids["bvid"], ids["aid"])
        if not video:
            await reply(
                bot,
                ev,
                "取不到这个视频：可能被删除、仅粉丝可见，或接口被风控（配置 Cookie 更稳）",
            )
            return
        vname = str(video.get("author") or "")
        msgs, _ = await build_video_msgs(video, vname)
        if to_subs:
            uid = str(video.get("mid") or "")
            subs = await storage.load_subs()
            groups = ((subs.get(uid) or {}).get("groups") or {}) if uid else {}
            if not groups:
                await reply(
                    bot,
                    ev,
                    f"找不到 UID {uid or '（未知，视频接口没给作者）'} 的订阅群，"
                    "去掉「订阅群」可以推到本会话",
                )
                return
            sent = await push_to_groups(
                groups, msgs, None, fallback_text=format_video(video, vname)["text"]
            )
            logger.info(
                f"B站订阅：手动补推视频 {video.get('bvid') or arg} 到 {sent}/{len(groups)} 个订阅群"
            )
            await reply(bot, ev, f"已推送到 {sent}/{len(groups)} 个订阅群")
            return
        await reply(bot, ev, msgs)
        logger.info(f"B站订阅：手动补推视频 {video.get('bvid') or arg} 到本会话")
        return

    # ① 先按动态详情取（opus id 就是动态 id，两种链接都走这里）
    parsed: Optional[Dict] = None
    for did in (ids["id_str"], ids["opus_id"]):
        if not did:
            continue
        item = await api.get_detail_item(did)
        if item:
            parsed = extract_dynamic(item)
            break

    # ② 详情接口拿不到（最常见是被 -352 风控）：改从 opus 页面解析。
    # 页面是服务端渲染、正文直出，比接口稳得多，标题/作者/正文/图片都能拿到
    if not parsed:
        for oid in (ids["opus_id"], ids["id_str"]):
            if not oid:
                continue
            parsed = await fulltext.parse_opus_page(oid)
            if parsed:
                logger.info(f"B站订阅：动态详情没取到，改从 opus 页面解析 {arg}")
                break

    # ③ 还是不行（比如只给了 cv 号）：退化成"文章"形态，正文交给 fulltext 抓
    if not parsed:
        fallback_url = (
            f"https://www.bilibili.com/read/cv{ids['cv_id']}"
            if ids["cv_id"]
            else f"https://www.bilibili.com/opus/{ids['opus_id']}" if ids["opus_id"] else arg
        )
        parsed = {
            "id_str": ids["id_str"],
            "kind": "article",
            "uname": "",
            "mid": "",
            "action": "投稿了文章",
            "title": "",
            "text": "",
            "images": [],
            "url": fallback_url,
            "pub_ts": 0,
            "bvid": "",
            "cv_id": ids["cv_id"],
            "opus_id": ids["opus_id"],
        }
        logger.info(f"B站订阅：动态详情没取到 {arg}，改用文章形态抓正文")
    elif ids["cv_id"] and not parsed.get("cv_id"):
        parsed["cv_id"] = ids["cv_id"]

    name = str(parsed.get("uname") or "")
    # 先抓正文（fetch 会顺手把标题/作者/正文回填到 parsed），**再**拼卡片：
    # 卡片是拿 parsed 现画的，顺序反了就是"未知UP · 投稿了文章"那种空壳。
    # 手动补推等于调试，强制重抓一次，不看 short 门槛。
    await fulltext.fill(parsed, force=True)
    if not str(parsed.get("text") or "").strip():
        head = "\n".join(
            str(p.get("text") or "")
            for p in (parsed.get("full_parts") or [])
            if p.get("type") == "text"
        )
        if head.strip():
            parsed["text"] = " ".join(head.split())[:200]
    if not str(parsed.get("text") or "").strip() and not parsed.get("title"):
        # 什么都抓不到时别发一张空白卡：给一句实话，链接在卡片下面单独发
        parsed["text"] = "（没能取到正文，点下面的链接看原文）"

    # 推到订阅群
    if to_subs:
        uid = str(parsed.get("mid") or "")
        subs = await storage.load_subs()
        groups = ((subs.get(uid) or {}).get("groups") or {}) if uid else {}
        if not groups:
            await reply(
                bot,
                ev,
                f"找不到 UID {uid or '（未知，详情接口没给作者）'} 的订阅群，"
                "去掉「订阅群」可以推到本会话",
            )
            return
        msgs, forward = await build_dynamic_msgs(parsed, name)
        sent = await push_to_groups(
            groups, msgs, forward, fallback_text=format_dynamic(parsed, name)["text"]
        )
        logger.info(f"B站订阅：手动补推 {parsed.get('id_str') or arg} 到 {sent}/{len(groups)} 个订阅群")
        await reply(bot, ev, f"已推送到 {sent}/{len(groups)} 个订阅群")
        return

    # 推到本会话：卡片 + 其余图先发，长文全文**另起一帧**（混在同一帧里 node 会跑到前面）
    msgs, forward = await build_dynamic_msgs(parsed, name)
    await reply(bot, ev, msgs)
    for batch in forward_batches(forward):
        await asyncio.sleep(1.0)
        await reply(bot, ev, batch)
    logger.info(f"B站订阅：手动补推 {parsed.get('id_str') or arg} 到本会话")


# ---------------------------------------------------------------------------
# 轮询引擎
# ---------------------------------------------------------------------------
_last_check = time.time()
_running = False
_cursor = 0  # 订阅很多时轮着查，避免一轮打太多请求


def _targets_for(info: Dict, kind: str, notify: Dict[str, bool]) -> Dict[str, Dict]:
    """这类内容该推给哪些群：全局开关关着就没人要，否则按每条订阅自己的开关筛。"""
    key = "dynamic" if kind not in ("video", "live") else kind
    if not notify.get(key):
        return {}
    return storage.groups_for(info, key)


async def _check_one(uid: str, info: Dict) -> Optional[Dict]:
    """检查一个 UP。返回需要写回的字段（无变化返回 None）。

    - 首次检查只建立游标基线，不推历史内容；
    - 动态里的投稿视频与「投稿列表」接口查到的视频通过 ``pushed_av`` 去重；
    - **每条订阅自己的开关按群生效**：同一个 UP，A 群只想收视频、B 群只想收动态
      都是合法需求，所以推之前要按"这类内容"把群筛一遍；
    - 带货/广告过滤只对**非官方号**做（官方号有白名单，不重复过滤）；
    - 直播开播只在真有群要直播时才查（默认全局 `notify_live` 是关的，等于零额外请求）。
    """
    groups = info.get("groups") or {}
    notify = {
        "dynamic": bool(cfg.get("notify_dynamic")),
        "video": bool(cfg.get("notify_video")),
        "live": bool(cfg.get("notify_live")),
    }
    notify_video = notify["video"]
    notify_dynamic = notify["dynamic"]
    notify_live = notify["live"]
    ad_filter = bool(cfg.get("ad_filter", True))
    limit = max(1, int(cfg.get("max_push_per_round") or 5))
    name = str(info.get("name") or "")
    update: Dict = {}

    # 官方号（命中某个启用中的游戏规则）：非视频动态按该游戏的白名单过滤
    preset = games.find_by_uid(uid)

    # ---------- 动态 ----------
    if notify_video or notify_dynamic or notify_live:
        items = await api.get_dynamic(uid)
        if items is not None:
            parsed = [extract_dynamic(it) for it in items]
            parsed = [p for p in parsed if p.get("id_str")]
            if not name and parsed:
                name = str(parsed[0].get("uname") or "")
                if name:
                    update["name"] = name

            last = str(info.get("last_dynamic") or "")
            if not last:
                # 首轮只建基线：同时把已出现的投稿视频标成"已见"，
                # 否则下一轮投稿列表检查会把这几个存量视频全部当新视频补推。
                newest = latest_id(parsed)
                if newest:
                    update["last_dynamic"] = newest
                    logger.info(f"B站订阅：UID {uid} 建立动态基线 {newest}")
                for p in parsed:
                    if p.get("kind") == "video" and p.get("bvid"):
                        storage.remember_av(info, str(p["bvid"]))
                if info.get("pushed_av"):
                    update["pushed_av"] = list(info["pushed_av"])
            else:
                # 注意：这里必须从**解析后**的列表里挑新动态，直接用接口原始
                # item 的话 kind / action / bvid 都是空的，推送内容会全丢。
                for p in select_new_dynamics(parsed, last, limit=limit):
                    kind = str(p.get("kind") or "other")
                    if kind == "video" and p.get("bvid"):
                        storage.remember_av(info, str(p["bvid"]))
                        update["pushed_av"] = list(info.get("pushed_av") or [])
                    # 这类内容该推给哪些群（全局 + 每条订阅自己的开关）
                    targets = _targets_for(info, kind, notify)
                    enabled = bool(targets)
                    # 非官方号先过一遍带货/广告规则；判为广告就整条不推（游标照走）
                    if enabled and ad_filter and preset is None:
                        why = adfilter.looks_like_ad(p)
                        if why:
                            runtime.STATE["filtered"] = (
                                int(runtime.STATE.get("filtered") or 0) + 1
                            )
                            logger.info(
                                f"B站订阅：{name or uid} 的动态 {p.get('id_str')} "
                                f"判定为带货/广告（{why}），这条不推"
                            )
                            enabled = False
                    # enabled 为假表示这类动态整体关了（或判成广告），游标照走。
                    # 注意 FILTER / PENDING 只会出现在有游戏预设（官方号）的分支里，
                    # 普通 UP 一律 PUSH，所以下面两个分支里 preset 一定不是 None。
                    decision = _decide_how(preset, kind, p) if enabled else None
                    if decision == games.FILTER:
                        # FILTER 只有一个来源：命中了 block_keywords（两种口径都一样）
                        runtime.STATE["filtered"] = int(runtime.STATE.get("filtered") or 0) + 1
                        logger.info(
                            f"B站订阅：{name or uid} 的动态 {p.get('id_str')} "
                            f"命中《{preset.name}》的屏蔽关键词"
                            f"（{preset.match_block(p) or ''}），这条不推"
                        )
                    elif decision == games.PENDING:
                        if cfg.get("queue_unmatched"):
                            # 待定队列也要带全文，否则捞回来时还得再抓一次
                            await fulltext.nodes_for(p)
                            item = await pending.enqueue(uid, name, p, game=preset.name)
                            await pending.notify_pending(item)
                            logger.info(
                                f"B站订阅：{name or uid} 的动态 {p.get('id_str')} 没命中"
                                f"《{preset.name}》的放行栏目，进待定队列 #{item.get('seq')}"
                            )
                        else:
                            runtime.STATE["filtered"] = (
                                int(runtime.STATE.get("filtered") or 0) + 1
                            )
                            logger.info(
                                f"B站订阅：{name or uid} 的动态 {p.get('id_str')} 没命中"
                                f"《{preset.name}》的放行栏目，已过滤（不推群）"
                            )
                    elif decision == games.PUSH and targets:
                        # 卡片（头部+标题+正文+首图）+ 其余图单独补发 + 长文合并转发
                        msgs, forward = await build_dynamic_msgs(p, name)
                        await push_to_groups(
                            targets,
                            msgs,
                            forward,
                            fallback_text=format_dynamic(p, name)["text"],
                            kind=kind if kind in ("video", "live") else "dynamic",
                        )
                        logger.info(f"B站订阅：推送 {name or uid} 的动态 {p.get('id_str')}")
                    update["last_dynamic"] = str(p.get("id_str") or last)

    # ---------- 投稿列表（补动态接口的漏） ----------
    video_targets = (
        _targets_for(info, "video", notify)
        if (notify_video and cfg.get("check_video_list"))
        else {}
    )
    if video_targets:
        vlist = await api.get_latest_videos(uid, 5)
        if vlist:
            pushed = [str(b) for b in (info.get("pushed_av") or [])]
            newest_bvid = str(vlist[0].get("bvid") or "")
            if not info.get("last_video") and not pushed:
                # 首次只建基线：把当前列表里的视频全部标记为已见，避免下一轮当成新视频补推
                for v in vlist:
                    storage.remember_av(info, str(v.get("bvid") or ""))
                if newest_bvid:
                    update["last_video"] = newest_bvid
                    logger.info(f"B站订阅：UID {uid} 建立投稿基线 {newest_bvid}")
                if info.get("pushed_av"):
                    update["pushed_av"] = list(info["pushed_av"])
            else:
                ordered = sorted(vlist, key=lambda v: int(v.get("created") or 0))
                for v in ordered[-limit:]:
                    bvid = str(v.get("bvid") or "")
                    if not bvid or bvid in pushed:
                        continue
                    if video_targets:
                        msgs, _ = await build_video_msgs(v, name)
                        await push_to_groups(
                            video_targets,
                            msgs,
                            fallback_text=format_video(v, name)["text"],
                            kind="video",
                        )
                        logger.info(f"B站订阅：推送 {name or uid} 的视频 {bvid}")
                    storage.remember_av(info, bvid)
                    pushed.append(bvid)
                update["pushed_av"] = list(info.get("pushed_av") or [])
                if newest_bvid:
                    update["last_video"] = newest_bvid

    # ---------- 直播开播 ----------
    # 只在真有群要直播时才查：全局 notify_live 默认是关的，等于零额外请求
    live_targets = _targets_for(info, "live", notify)
    if live_targets:
        room = await api.get_live_room(uid)
        if room:
            room_id = str(room.get("room_id") or "")
            if room_id and str(info.get("room_id") or "") != room_id:
                update["room_id"] = room_id
                logger.info(f"B站订阅：{name or uid} 的直播间是 {room_id}")
            status = room.get("live_status")
            try:
                status = None if status is None else int(status)
            except (TypeError, ValueError):
                status = None
            raw_old = info.get("live_status")
            try:
                old = None if raw_old in (None, "") else int(raw_old)
            except (TypeError, ValueError):
                old = None
            if status is not None and room_id:
                if old is None:
                    # 首轮只建基线：订阅时他正好在播也不推，免得刚订阅就砸一条
                    update["live_status"] = status
                    logger.info(f"B站订阅：UID {uid} 建立直播基线 {status}")
                elif old in (0, 2) and status == 1:
                    msgs, _ = await build_live_msgs(room, name, uid)
                    await push_to_groups(
                        live_targets,
                        msgs,
                        fallback_text=format_live(room, name)["text"],
                        kind="live",
                    )
                    update["live_status"] = 1
                    logger.info(
                        f"B站订阅：{name or uid} 开播了（{room.get('title')}），已推送"
                    )
                elif old != status:
                    update["live_status"] = status

    return update or None


async def check_all() -> int:
    """跑一轮检查，返回本轮有记录的 UP 数。"""
    # 游戏规则的双向同步（文件 ↔ 数据库）。放在轮询里顺带跑：控制台里改完，
    # 最多等到下一轮就落到 JSON 上；后台任务（ensure_loop）会跑得更勤。
    try:
        await game_rules_db.sync_now()
    except Exception as e:
        logger.warning(f"B站订阅：游戏规则同步出错（{type(e).__name__}: {e}）")

    if api.rate_limited:
        logger.warning("B站订阅：接口正在风控静默期，跳过本轮检查")
        return 0

    subs = await storage.load_subs()
    if not subs:
        runtime.STATE["last_check_ts"] = time.time()
        return 0

    global _cursor
    try:
        max_uid = max(1, int(cfg.get("max_uid_per_round") or 20))
    except (TypeError, ValueError):
        max_uid = 20

    uids = list(subs.keys())
    if len(uids) > max_uid:
        start = _cursor % len(uids)
        uids = (uids + uids)[start:start + max_uid]
        _cursor = start + max_uid
    else:
        _cursor = 0

    updates: Dict[str, Dict] = {}
    runtime.STATE["running"] = True
    try:
        for uid in uids:
            info = subs.get(uid) or {}
            if not info.get("groups"):
                continue
            try:
                up = await _check_one(uid, info)
            except Exception as e:
                # 光打一句 msg 定位不了（"name 'x' is not defined" 这种要栈才知道在哪），
                # 所以异常连栈一起进日志；日志级别仍是 warning，不刷屏
                logger.warning(f"B站订阅：检查 UID {uid} 出错：{type(e).__name__}: {e}")
                try:  # loguru：opt(exception=True) 才是"带栈"
                    logger.opt(exception=True).debug(f"B站订阅：UID {uid} 出错详情")
                except Exception:
                    pass
                up = None
            if up:
                updates[uid] = up
            # 单 UP 之间留点间隔，降低被风控的概率
            await asyncio.sleep(2)

        if updates:
            await storage.merge_state(updates)
    finally:
        runtime.STATE["running"] = False
        runtime.STATE["checks"] = int(runtime.STATE.get("checks") or 0) + 1
        runtime.STATE["last_updates"] = len(updates)
        runtime.STATE["last_check_ts"] = time.time()
    return len(updates)


if scheduler is not None:

    @scheduler.scheduled_job(
        "interval",
        seconds=60,
        id="BiliSubscribe_check",
        name="B站订阅-更新检查",
    )
    async def _tick():
        """每分钟醒来一次，按配置的间隔真正执行检查。"""
        global _last_check, _running
        if _running:
            return
        try:
            interval = max(60, int(cfg.get("check_interval") or 300))
        except (TypeError, ValueError):
            interval = 300
        # 休眠时段内不轮询（省风控、夜里不打扰）；手动 bili检查更新 不受影响
        if cfg.get("sleep_mode", True) and _in_sleep(str(cfg.get("sleep_range") or "")):
            logger.debug("B站订阅：休眠时段，本轮跳过")
            return
        now = time.time()
        if now - _last_check < interval:
            return
        _last_check = now
        _running = True
        try:
            n = await check_all()
            if n:
                logger.info(f"B站订阅：本轮更新了 {n} 个 UP")
        except Exception as e:
            logger.error(f"B站订阅：轮询出错：{e}")
        finally:
            _running = False


# 注册核心状态图（控制台状态页）里的插件状态卡片；
# 核心没有这套 API、或者没有 PIL 时，register() 会自己跳过，不影响插件加载。
status_card.register()
