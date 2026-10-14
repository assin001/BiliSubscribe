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

"""消息格式化 + 主动推送。

主动推送不经过用户消息事件，走 ``gss.active_bot[bot_id].target_send(...)``
（与核心自带的 ``utils/boardcast/send_msg.py`` 同一套机制）。

一条更新是这么发的（``build_*_msgs`` 只负责拼，``push_to_groups`` 负责发）：

1. **卡片图**：头部 + 标题 + 正文 + 首图（``card.py`` 画出来，见那里注释）；
2. **其余配图**：按顺序单独补发，``.gif`` 原样发所以动图还会动；
3. **长文全文**：合并转发补一条（只有长文/公告才有）。

卡片画不出来（没 PIL / 图挂了）时自动退回"文字 + 图片"的老发法，推送不中断。
"""
import asyncio
import time
from typing import Dict, List, Optional, Tuple

from gsuid_core.bot import Bot
from gsuid_core.gss import gss
from gsuid_core.logger import logger
from gsuid_core.models import Event, Message
from gsuid_core.segment import MessageSegment

from ..bili import fulltext
from ..bili.api import api
from ..bili.dynamic import is_animated_url, normalize_image_url
from ..core import config as cfg
from ..core import runtime
from ..core import storage
from . import card as card_render

MAX_TEXT = 150
# 其余图片（合并转发）的"少发一点"规则：动图最多几张、单张动图最大多少，
# 触发时会在转发里（没有转发就单独发一条）说明一句，别让人以为图丢了
RULE_NOTE = "动态动图触发大小上限规则，部分发送"
EXTRA_GIF_MAX_BYTES = 10 * 1024 * 1024
MAX_EXTRA_GIFS = 4  # 其余图里的动图最多发这么多张，多出来的跳过
GIF_CONCURRENCY = 4  # 动图并发下载/压缩的并发数（一张张来太慢，会把适配器拖掉线）
# 卡片之外最多再补发几张图（防止一条动态把群刷屏）
MAX_EXTRA_IMAGES = 9
# 开「动图压缩」时，小于这个大小的动图不动它
GIF_SHRINK_MIN = 1_000_000
# 逐条发全文时，一条消息最多并这么多字（少了刷屏，多了容易发不出去）
MAX_FULL_MSG = 1500
# 每个目标群之间的间隔，避免短时间刷太多被平台限流
_PUSH_GAP = 1.5


async def reply(bot: Bot, ev: Event, msg):
    """回消息：兼容不同版本的 Bot.send 签名（``send(msg)`` / ``send(ev, msg)``）。"""
    try:
        return await bot.send(msg)
    except TypeError:
        return await bot.send(ev, msg)


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def _clean_url(url: str) -> str:
    """去掉链接后面的跟踪参数（``?spm_id_from=…`` 这种），只留干净的地址。"""
    return str(url or "").strip().split("?")[0].split("#")[0]


async def _link_for(url: str, **hints) -> str:
    """推送里用的原文链接：开了短链就换 b23.tv，拿不到再退回（去掉跟踪参数的）长链接。

    ``hints`` 把现成的线索（``bvid`` / ``cv_id`` / ``opus_id``）透传给
    :meth:`api.get_short_link`，省得再从链接里抠一遍。
    """
    target = _clean_url(url)
    if not target:
        return ""
    if not cfg.get("push_short_link", True):
        return target
    try:
        short = await api.get_short_link(target, **hints)
    except Exception as e:
        logger.warning(f"B站订阅：短链生成失败（{target[:80]}）：{e}")
        short = ""
    return short or target


def _fmt_ts(ts) -> str:
    try:
        t = int(ts)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    return time.strftime("%m-%d %H:%M", time.localtime(t))


def format_dynamic(item: Dict, uname: str = "") -> Dict[str, str]:
    """动态 → ``{"text": ..., "cover": ...}``。"""
    name = item.get("uname") or uname or "未知UP"
    lines = [f"【B站订阅】{name} {item.get('action') or '发布了新动态'}"]
    if item.get("title"):
        lines.append(f"标题：{_clip(item['title'], 60)}")
    if item.get("text"):
        lines.append(f"内容：{_clip(item['text'])}")
    if item.get("bvid"):
        lines.append(f"BV号：{item['bvid']}")
    pub = _fmt_ts(item.get("pub_ts"))
    if pub:
        lines.append(f"时间：{pub}")
    # short_url 是 build_dynamic_msgs 生成的短链（开了短链才有），没有就用原链接
    link = str(item.get("short_url") or item.get("url") or "").strip()
    if link:
        lines.append(f"链接：{link}")
    cover = (item.get("images") or [""])[0]
    return {"text": "\n".join(lines), "cover": str(cover or "")}


def _video_cover(video: Dict) -> str:
    """视频封面链接：统一走 ``normalize_image_url``（补协议 / 升 https / avif 换 jpg）。"""
    return normalize_image_url(str(video.get("pic") or "").strip())


def format_live(room: Dict, uname: str = "") -> Dict[str, str]:
    """开播 → ``{"text": ..., "cover": ...}``（卡片画不出来时的退化文案）。"""
    name = uname or "未知UP"
    room_id = str(room.get("room_id") or "")
    lines = [f"【B站订阅】{name} 开播了"]
    title = _clip(room.get("title"), 60)
    if title:
        lines.append(f"标题：{title}")
    if room_id:
        lines.append(f"链接：https://live.bilibili.com/{room_id}")
    cover = normalize_image_url(str(room.get("cover") or ""))
    return {"text": "\n".join(lines), "cover": cover}


def format_video(video: Dict, uname: str = "") -> Dict[str, str]:
    """投稿列表条目 → ``{"text": ..., "cover": ...}``。"""
    bvid = str(video.get("bvid") or "").strip()
    name = uname or str(video.get("author") or "") or "未知UP"
    lines = [f"【B站订阅】{name} 投稿了新视频"]
    title = _clip(video.get("title"), 60)
    if title:
        lines.append(f"标题：{title}")
    if bvid:
        lines.append(f"BV号：{bvid}")
    if video.get("length"):
        lines.append(f"时长：{video['length']}")
    desc = _clip(video.get("description"))
    if desc:
        lines.append(f"简介：{desc}")
    pub = _fmt_ts(video.get("created"))
    if pub:
        lines.append(f"时间：{pub}")
    # short_url 是 build_video_msgs 生成的短链（开了短链才有），没有就用原视频链接
    link = str(video.get("short_url") or "").strip() or (
        f"https://www.bilibili.com/video/{bvid}" if bvid else ""
    )
    if link:
        lines.append(f"链接：{link}")
    # 视频接口给的封面是 http://i0.hdslb.com/…，统一走 normalize_image_url
    # （补协议 / 升 https / avif 换 jpg），和动态那套口径一致
    return {"text": "\n".join(lines), "cover": _video_cover(video)}


async def send_to_target(
    target_type: str, target_id: str, msgs: List[Message], entry: Dict = None
) -> bool:
    """把消息发到某个会话（群或私聊）。

    优先用订阅/登录时记录的那个 bot；它不在线就依次尝试其它在线 bot，
    成功一次即返回，避免同一条内容被多个 bot 重复发。
    """
    entry = entry or {}
    want_bot = str(entry.get("bot_id") or "")
    bot_self_id = str(entry.get("bot_self_id") or "")
    kind = str(target_type or "group")
    if kind not in ("group", "direct", "channel", "sub_channel"):
        kind = "group"

    order: List[str] = []
    if want_bot and want_bot in gss.active_bot:
        order.append(want_bot)
    order.extend(b for b in list(gss.active_bot) if b not in order)

    if not order:
        logger.warning(f"B站订阅发送失败：当前没有在线 Bot（{kind} {target_id}）")
        return False

    for bid in order:
        inst = gss.active_bot.get(bid)
        if inst is None:
            continue
        # 传给核心的 bot_id 用**订阅时记下的那个真实 bot_id**：核心是按 bot_id 查这个
        # bot 的配置（图片 / 合并转发等）的。有些核心（如 SayuCore）``active_bot`` 的
        # key 是 WS_BOT_ID，直接拿 key 当 bot_id，核心就查不到配置、整条推送退回默认参数
        # （日志里成片的 "配置项 xxx 不存在也没有配置"），合并转发可能因此被拆成单条。
        bid_arg = want_bot or str(bid)
        try:
            await inst.target_send(
                msgs,
                kind,
                str(target_id),
                bid_arg,
                bot_self_id if bid_arg == want_bot else "",
                "",
            )
            return True
        except Exception as e:
            logger.warning(f"B站订阅发送失败（{kind} {target_id}，bot={bid}）：{e}")
    return False


async def send_group(gid: str, entry: Dict, msgs: List[Message]) -> bool:
    """把消息推到指定群（``send_to_target`` 的群聊封装）。"""
    return await send_to_target("group", str(gid), msgs, entry)


def _image_msgs(urls: List[str], limit: int = MAX_EXTRA_IMAGES) -> List[Message]:
    """一串图片 → 消息段（构造失败的跳过，不影响其它图）。"""
    out: List[Message] = []
    for url in urls[:limit]:
        target = str(url or "").strip()
        if not target:
            continue
        try:
            out.append(MessageSegment.image(target))
        except Exception as e:
            logger.warning(f"构造配图消息段失败（{target[:80]}）：{e}")
    return out


def _rule_text(notes: List[str]) -> str:
    """触发"少发"规则时的说明文案（放在转发最后一条，或者单独发一条）。"""
    return "\n".join([RULE_NOTE, *notes, "（完整内容点原文链接看）"])


async def _filter_extras(urls: List[str]) -> Tuple[List[str], List[str]]:
    """按规则筛一遍其余图片，返回 ``(留下的, 说明文案)``。

    规则（只针对**动图**，静态图一般几百 KB 就不查了）：

    - 一条动态里的动图最多 ``MAX_EXTRA_GIFS`` 张，多出来的直接跳过；
    - 单张超过 ``EXTRA_GIF_MAX_BYTES`` 的也不发。

    探测大小是**并发**做的（HEAD/Range 只拿头，很快），而且**探不到就不跳图**
    —— 不能因为一次探测失败把图吞了。
    """
    if not urls:
        return [], []
    notes: List[str] = []
    gifs = [u for u in urls if is_animated_url(u)]
    if len(gifs) > MAX_EXTRA_GIFS:
        notes.append(
            f"还有 {len(gifs) - MAX_EXTRA_GIFS} 张动图超过单条上限（{MAX_EXTRA_GIFS} 张）"
        )
        gifs = gifs[:MAX_EXTRA_GIFS]

    sizes = await asyncio.gather(*(api.head_size(u) for u in gifs)) if gifs else []
    keep_gifs: List[str] = []
    dropped_big = 0
    for url, size in zip(gifs, sizes):
        if size > EXTRA_GIF_MAX_BYTES:
            dropped_big += 1
            logger.info(
                f"B站订阅：动图 {size // 1024 // 1024}MB 超过上限"
                f"（{EXTRA_GIF_MAX_BYTES // 1024 // 1024}MB），这条不发：{url[:80]}"
            )
            continue
        keep_gifs.append(url)
    if dropped_big:
        notes.append(
            f"{dropped_big} 张动图超过 {EXTRA_GIF_MAX_BYTES // 1024 // 1024}MB 未发送"
        )

    allowed = set(keep_gifs)
    kept = [u for u in urls if (not is_animated_url(u)) or (u in allowed)]
    return kept, notes


def _split_extras(urls: List[str]) -> Tuple[List[str], List[str]]:
    """其余图片分两拨：**进合并转发的**（≥2 张）和**直接发的**（只有 1 张时）。

    这里**不再给动图单开一条路**：线上实测动图到群里本来也只是首帧静态图，
    那就没必要为它绕开转发，一视同仁更省事。
    """
    if not urls:
        return [], []
    if len(urls) >= 2:
        return urls, []
    return [], urls


async def _extra_images_forward(urls: List[str], notes: Optional[List[str]] = None) -> List[Message]:
    """其余图片 → **一条合并转发**（和全文那条走同一个出口，单独一帧发）。

    转发里的每张图各占一条（对方看到的是"群聊的聊天记录 / [图片] ×N"，点开看大图），
    不至于十来张图把群刷一屏。构造失败就退回一张张发。

    **开了「动图先压小再发」时，动图在这里也先压小**：核心（Sget）发图前会把远程图片
    抓下来，一条动态里好几张 7~8MB 的动图就是几十兆的下载，慢到把适配器拖掉线
    （线上日志：7 张动图 → 8.7s → NoneBot2 断开重连）。压小之后：
    本地只下一次、所有订阅群复用同一份字节。
    """
    if not urls:
        return []

    sem = asyncio.Semaphore(GIF_CONCURRENCY)

    async def _one(raw: str) -> Optional[Message]:
        target = str(raw or "").strip()
        if not target:
            return None
        if cfg.get("gif_shrink", False) and is_animated_url(target):
            # 下载 + 压缩很慢：并发跑（asyncio 里就是"多线程"那个意思，
            # 对 aiohttp 下载来说比真开线程更合适），但一张张来会拖到适配器掉线
            async with sem:
                msg = await _shrink_gif_msg(target)
            if msg is not None:
                return msg
        try:
            return MessageSegment.image(target)  # type: ignore[list-item]
        except Exception as e:
            logger.warning(f"B站订阅：合并转发里的配图构造失败（{target[:80]}）：{e}")
            return None

    results = await asyncio.gather(*(_one(u) for u in urls))
    items = [m for m in results if m is not None]
    if not items:
        return []
    if notes:  # 触发了"少发"规则：在转发最后补一句，免得以为图丢了
        try:
            items.append(MessageSegment.text(_rule_text(list(notes))))  # type: ignore[list-item]
        except Exception as e:
            logger.warning(f"B站订阅：规则说明塞进转发失败（{e}）")
    try:
        return [MessageSegment.node(items)]  # type: ignore[list-item]
    except Exception as e:
        logger.warning(f"B站订阅：其余图片的合并转发构造失败（{e}），退回一张张发")
        return items


async def _extra_image_msgs(urls: List[str]) -> List[Message]:
    """其余配图 → 消息段：**一张一张按原链接发**（同一条消息里），永不拼图。

    现在只用于"其余图只有 1 张"：≥2 张走 ``_extra_images_forward`` 合并转发。
    顺带说明为什么不拼图：下载再合成对画质没好处、格子也拼不齐，而各张图放同一条
    消息里 QQ 自己会排版。
    """
    if not urls:
        return []
    msgs: List[Message] = []
    for url in urls:
        target = str(url or "").strip()
        if not target:
            continue
        msg = await _shrink_gif_msg(target) if cfg.get("gif_shrink", False) else None
        if msg is None:
            msg = next(iter(_image_msgs([target], limit=1)), None)
        if msg is not None:
            msgs.append(msg)
    return msgs


async def _shrink_gif_msg(url: str) -> Optional[Message]:
    """可选（默认关）：把大动图压小再发。

    B站的动图单张能到 7.8MB，个别平台遇到这么大的会当静态图处理；压到几百 KB
    反而更容易动起来。压不出来就返回 None，调用方按原链接发。
    """
    if not is_animated_url(url):
        return None
    data = await api.fetch_bytes(url)
    if not data or len(data) < GIF_SHRINK_MIN:
        return None
    small = card_render.shrink_gif(data)
    if not small:
        return None
    logger.info(f"B站订阅：动图 {len(data) // 1024}KB → {len(small) // 1024}KB 再发")
    try:
        return MessageSegment.image(small)  # type: ignore[return-value]
    except Exception as e:
        logger.warning(f"B站订阅：压缩后的动图构造失败（{e}）")
        return None


async def build_dynamic_msgs(parsed: Dict, name: str = "") -> Tuple[List[Message], List[Message]]:
    """动态 → ``(要发的消息段, 长文合并转发节点)``。

    - 卡片放首图；**其余图按原链接一张张发**（不拼图、不转码，动图才还会动）；
    - ``push_cover`` 关掉时卡片不带图，``push_extra_images`` 关掉时不补发其余图；
    - 卡片画不出来就退回 ``format_dynamic`` 的文字 + 最多 3 张图。
    """
    up = str(parsed.get("uname") or name or "未知UP")
    images = [str(u) for u in (parsed.get("images") or []) if str(u or "").strip()]
    # 图 ≤1 张就补一次：**空间动态接口**有时只给一张封面（全图在详情接口 / opus 页面里），
    # 而"其余图走合并转发"要 ≥2 张 —— 不补的话同一条动态在订阅推送里只出单图，
    # 而 bili推这条（它先 fill 过）却走了合并转发，看着就是两条链路不一致。
    if len(images) <= 1 and str(parsed.get("kind") or "") in ("draw", "article"):
        try:
            # 必须 force=True：fetch 开头就 `if not force and not needs_full(): return`，
            # 图文动态根本过不了那个门槛，等于没补（bili推这条 也是 force=True 才拿得到图）
            await fulltext.fill(parsed)
        except Exception as e:
            logger.warning(f"B站订阅：动态 {parsed.get('id_str')} 补图失败（{e}）")
        images = [str(u) for u in (parsed.get("images") or []) if str(u or "").strip()]
    with_image = bool(cfg.get("push_cover", True))
    want_extras = bool(cfg.get("push_extra_images", True))
    extras = images[1 : 1 + MAX_EXTRA_IMAGES] if want_extras else []
    # 触发了"少发"规则就记一笔，最后在转发里 / 消息里说明（见 _rule_text）
    rule_notes: List[str] = []
    if want_extras and len(images) - 1 > len(extras):
        rule_notes.append(
            f"还有 {len(images) - 1 - len(extras)} 张图超过单条上限（{MAX_EXTRA_IMAGES} 张）"
        )

    card: Optional[bytes] = None
    if cfg.get("push_card", True):
        cover = await api.fetch_bytes(images[0]) if (images and with_image) else None
        avatar = await api.fetch_bytes(str(parsed.get("avatar") or "")) if parsed.get("avatar") else None
        card = card_render.render(
            up_name=up,
            action=str(parsed.get("action") or ""),
            title=str(parsed.get("title") or ""),
            body=str(parsed.get("text") or ""),
            time_text=_fmt_ts(parsed.get("pub_ts")),
            cover=cover,
            avatar=avatar,
            uid=str(parsed.get("mid") or ""),
            qr_text=_clean_url(str(parsed.get("url") or "")),  # 二维码 = 原文链接（去掉跟踪参数）
        )

    # 推送里发的链接：开了短链就发 b23.tv 短链，拿不到退回（去掉跟踪参数的）原链接。
    # 卡片里的二维码仍用原链接 —— 短链有时效，卡片被存下来以后再扫会失效。
    link = await _link_for(
        str(parsed.get("url") or ""),
        bvid=str(parsed.get("bvid") or ""),
        cv_id=str(parsed.get("cv_id") or ""),
        opus_id=str(parsed.get("opus_id") or ""),
    )
    parsed["short_url"] = link  # 卡片发不出去时，退化文案（format_dynamic）也发短链
    # 先按规则筛一遍（动图张数/大小），再分拨：≥2 张 → 合并转发（单独一帧）；
    # 只有 1 张 → 跟卡片同一条消息
    kept_extras, filter_notes = await _filter_extras(extras)
    rule_notes.extend(filter_notes)
    forward_extras, inline_extras = _split_extras(kept_extras)
    if card:
        msgs: List[Message] = [
            MessageSegment.image(card)  # type: ignore[list-item]
        ]
        if link:
            # 链接不画进卡片（画进去点不了），在卡片下面单独发一条
            msgs.append(MessageSegment.text(link))
        msgs.extend(await _extra_image_msgs(inline_extras))
    else:
        out = format_dynamic(parsed, name)
        msgs = [MessageSegment.text(out["text"])]
        if with_image:
            msgs.extend(_image_msgs(images[:3], limit=3))

    images_forward = await _extra_images_forward(forward_extras, rule_notes)
    if rule_notes and not images_forward:
        # 没有转发可挂（其余图 0/1 张）：单独发一句说明
        msgs.append(MessageSegment.text(_rule_text(rule_notes)))
    if images:
        # 排查"多图动态推出来只有单图"用：配图总数 / 其余图走了转发还是单发
        logger.info(
            f"B站订阅：动态 {parsed.get('id_str')} 配图 {len(images)} 张"
            f"（卡片首图 + 其余 {len(extras)} 张 → 转发 {len(images_forward)} 帧 / "
            f"跟卡片单发 {len(inline_extras)} 张）"
        )
    return msgs, images_forward + await fulltext.nodes_for(parsed)


async def build_video_msgs(video: Dict, name: str = "") -> Tuple[List[Message], List[Message]]:
    """投稿 → ``(要发的消息段, [])``：同样是一张卡片（标题 + 简介 + 封面）。"""
    up = str(video.get("author") or name or "未知UP")
    cover = _video_cover(video)
    bvid = str(video.get("bvid") or "").strip()
    body_parts = []
    if video.get("length"):
        body_parts.append(f"时长：{video['length']}")
    if video.get("description"):
        body_parts.append(str(video["description"]))
    body = "\n".join(body_parts)

    raw_link = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
    card: Optional[bytes] = None
    if cfg.get("push_card", True):
        cover_bytes = await api.fetch_bytes(cover) if (cover and cfg.get("push_cover", True)) else None
        face = str(video.get("face") or "").strip()
        avatar = await api.fetch_bytes(face) if face else None
        card = card_render.render(
            up_name=up,
            action="投稿了新视频",
            title=str(video.get("title") or ""),
            body=body,
            time_text=_fmt_ts(video.get("created")),
            cover=cover_bytes,
            avatar=avatar,
            uid=str(video.get("mid") or ""),
            qr_text=raw_link,  # 二维码 = 视频链接（原链接，不受短链时效影响）
        )
    # 文字那行发短链（视频短链就是 https://b23.tv/<BV号>，拼出来即可）
    link = await _link_for(raw_link, bvid=bvid)
    video["short_url"] = link  # 卡片发不出去时，退化文案（format_video）也发短链
    if card:
        msgs: List[Message] = [MessageSegment.image(card)]  # type: ignore[list-item]
        if link:
            msgs.append(MessageSegment.text(link))
        return msgs, []

    out = format_video(video, name)
    msgs: List[Message] = [MessageSegment.text(out["text"])]
    if cover and cfg.get("push_cover", True):
        msgs.extend(_image_msgs([cover], limit=1))
    return msgs, []


async def build_live_msgs(
    room: Dict, name: str = "", uid: str = ""
) -> Tuple[List[Message], List[Message]]:
    """开播 → ``(要发的消息段, [])``：一张卡片（封面 + 直播间标题 + 二维码指直播间）。

    头像多打一次名片接口拿（开播推送很少，值这个请求）；拿不到就画粉圆 + 名字首字。
    """
    up = str(name or "未知UP")
    room_id = str(room.get("room_id") or "")
    link = f"https://live.bilibili.com/{room_id}" if room_id else ""
    title = str(room.get("title") or "")
    cover = normalize_image_url(str(room.get("cover") or ""))

    card: Optional[bytes] = None
    if cfg.get("push_card", True):
        cover_bytes = (
            await api.fetch_bytes(cover)
            if (cover and cfg.get("push_cover", True))
            else None
        )
        avatar = None
        if str(uid or "").strip():
            info = await api.get_user_card(str(uid))
            if info:
                if info.get("name"):  # 用名片里的正式名字（订阅里存的名字可能是旧的）
                    up = str(info["name"])
                if info.get("face"):
                    avatar = await api.fetch_bytes(str(info["face"]))
        card = card_render.render(
            up_name=up,
            action="开播了",
            subtitle="开播了",
            title=title,
            body=f"直播间：{link}" if link else "",
            time_text=_fmt_ts(time.time()),
            cover=cover_bytes,
            avatar=avatar,
            uid=str(uid or ""),
            qr_text=link,  # 二维码 = 直播间链接
        )
    if card:
        msgs: List[Message] = [MessageSegment.image(card)]  # type: ignore[list-item]
        if link:
            msgs.append(MessageSegment.text(link))
        return msgs, []

    out = format_live(room, name)
    msgs = [MessageSegment.text(out["text"])]
    if out["cover"] and cfg.get("push_cover", True):
        msgs.extend(_image_msgs([out["cover"]], limit=1))
    return msgs, []


def forward_batches(forward: Optional[List[Message]]) -> List[List[Message]]:
    """长文全文 → 一批批发（**必须单独发，不能和卡片混在同一帧里**）。

    核心的发送逻辑里，同一帧内如果有 node 段，node 展开出来的内容会被排到
    该帧其它段**前面**（``EnableForwardMessage`` 设成数字/「全部拆成单独消息」时
    更明显），表现就是"转发跑到了卡片前头、还只有前几条"。所以这里统一拆成
    独立的批次，由调用方按顺序一帧一帧发（核心的发送队列是 FIFO，顺序就稳了）。

    默认**不**用合并转发：把正文按 ``MAX_FULL_MSG`` 字并成几条普通消息发（比一段一段发
    少刷屏，也不依赖平台支不支持转发）。开了 ``full_text_node`` 才发成一条合并转发 ——
    有些核心把 ``EnableForwardMessage`` 设成数字，转发只会发出前几段，
    看起来就是"转发里没有全文"。
    """
    if not forward:
        return []
    # 已经是 node 的（比如"其余图片"那条合并转发）原样单独成帧，别再套一层转发
    ready: List[List[Message]] = []
    rest: List[Message] = []
    for m in forward:
        if str(getattr(m, "type", "")) == "node":
            ready.append([m])
        else:
            rest.append(m)
    if not rest:
        return ready
    forward = rest
    if cfg.get("full_text_node", True):
        # 段数超过平台常见上限时，转发多半只会发出前几条（长文"转发里没全文"）。
        # 这种情况直接按段并块发：宁可不发转发卡片，也不能把正文丢掉。
        if len(forward) > fulltext.MAX_NODES + 2:
            logger.warning(
                f"B站订阅：全文 {len(forward)} 段，超过合并转发的常见上限"
                f"（{fulltext.MAX_NODES} 段左右），这次改成按段并块发"
            )
        else:
            try:
                return ready + [[MessageSegment.node(forward)]]
            except Exception as e:
                logger.warning(f"构造合并转发消息失败（{e}），改成按条发")

    batches: List[List[Message]] = []
    buf = ""
    for m in forward:
        if m.type == "text":
            for piece in str(m.data or "").split("\n"):
                if buf and len(buf) + len(piece) + 1 > MAX_FULL_MSG:
                    batches.append([MessageSegment.text(buf)])
                    buf = ""
                buf = f"{buf}\n{piece}" if buf else piece
        else:
            if buf:
                batches.append([MessageSegment.text(buf)])
                buf = ""
            batches.append([m])
    if buf:
        batches.append([MessageSegment.text(buf)])
    return ready + batches


async def push_to_groups(
    groups: Dict[str, Dict],
    msgs: List[Message],
    forward: Optional[List[Message]] = None,
    fallback_text: str = "",
    kind: str = "dynamic",
) -> int:
    """给一个 UP 的所有订阅群推同一条内容，返回成功群数。

    - ``msgs``：``build_*_msgs`` 拼好的消息段（卡片 + 其余图也算在里面）；
    - ``forward``：长文全文，会在正文之后**另起一帧**补发（避免 node 被提前展开）；
    - ``fallback_text``：``msgs`` 发失败时的退化文案（有些适配器不吃卡片这种大图）；
    - ``kind``：这批内容属于哪类（``dynamic`` / ``video`` / ``live``），
      用来按群判断"这条订阅的这类推送要不要 @"——每条订阅可以单独给某类开 @全体。
    """
    if not groups or not msgs:
        return 0
    at_all = bool(cfg.get("push_at_all"))
    batches = forward_batches(forward)

    sent = 0
    for gid, entry in groups.items():
        batch: List[Message] = []
        # @全体：全局开关 → 这条订阅的"哪类都@" → 这条订阅的"单独这类@"
        if at_all or storage.at_all_for(entry or {}, kind):
            try:
                batch.append(MessageSegment.at("all"))
                batch.append(MessageSegment.text("\n"))
            except Exception as e:
                logger.warning(f"构造 @全体成员 消息段失败：{e}")
        batch.extend(msgs)

        ok = await send_group(gid, entry or {}, batch)
        if not ok and fallback_text:
            # 复杂消息发失败时，退化成纯文本再试一次
            ok = await send_group(gid, entry or {}, [MessageSegment.text(fallback_text)])

        if ok:
            sent += 1
            for extra in batches:
                # 卡片发完再补全文（一次一帧，顺序由核心的发送队列保证）
                await asyncio.sleep(_PUSH_GAP)
                if not await send_group(gid, entry or {}, extra):
                    logger.warning(
                        f"B站订阅：给群 {gid} 补发（配图/全文合并转发，{len(extra)} 段）失败"
                    )
            if batches and cfg.get("full_text_node", True):
                logger.info(f"B站订阅：给群 {gid} 补发了全文（{len(forward or [])} 段）")
        await asyncio.sleep(_PUSH_GAP)

    if sent:
        # 推送量统一在这里记（内容推送的唯一出口），状态卡片会展示
        runtime.STATE["pushed"] = int(runtime.STATE.get("pushed") or 0) + sent
        runtime.STATE["last_push_ts"] = time.time()
    return sent
