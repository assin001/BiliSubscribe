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

"""待定队列（可选的兜底）：白名单**没命中**的动态，先搁这儿等人拍板。

默认是**关闭**的：官方号（见 ``games.py``）的动态只有命中该游戏的白名单栏目才推群，
其余（推广、联动、抽奖……）直接过滤掉，不打扰任何人。

如果想把"没命中"的也可能有用的动态捞回来，把配置
「未命中的动态进待定队列」（``queue_unmatched``）打开，这时没命中的动态会：

1. 进队列（落盘 ``data/BiliSubscribe/pending.json``，重启不丢）；
2. 发一条通知到 ``review_targets`` 里指定的会话（带图）；
3. 管理员拍板：``可以推送 <编号>`` 推给订阅群，``bili跳过 <编号>`` 丢弃；
4. **没拍板的一律不推**（"没有决定就是不推"），超过 ``PENDING_TTL`` 自动过期清理。

投稿视频不走这一套：视频本来就是官方号的主内容，照旧自动推送。
"""
import asyncio
import json
import time
from typing import Dict, List, Optional, Tuple

from gsuid_core.logger import logger

from ..render.push import build_dynamic_msgs, format_dynamic, push_to_groups, send_to_target
from . import config as cfg
from . import storage
from .config import DATA_DIR

PENDING_PATH = DATA_DIR / "pending.json"
LEGACY_PATH = DATA_DIR / "review.json"  # 老版本的文件名，加载时顺手搬过来

# 待定项保留多久（秒）；过期即视为"不推"，只留一条历史记录
PENDING_TTL = 7 * 24 * 3600
# 历史记录最多留多少条
MAX_HISTORY = 200

_lock = asyncio.Lock()


def _migrate_legacy() -> None:
    """老版本的 review.json 直接改名成 pending.json（只做一次）。"""
    try:
        if LEGACY_PATH.exists() and not PENDING_PATH.exists():
            LEGACY_PATH.replace(PENDING_PATH)
    except OSError:
        pass


_migrate_legacy()


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
def _load() -> Dict:
    if PENDING_PATH.exists():
        try:
            with open(PENDING_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
    else:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    pending = raw.get("pending")
    history = raw.get("history")
    return {
        "seq": int(raw.get("seq") or 0),
        "pending": pending if isinstance(pending, dict) else {},
        "history": history if isinstance(history, list) else [],
    }


def _save(data: Dict) -> None:
    tmp = PENDING_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(PENDING_PATH)


def _prune(data: Dict) -> None:
    """把过期、以及超出上限的待定项清掉（记为 expired）。"""
    now = time.time()
    for key in list(data["pending"].keys()):
        item = data["pending"][key] or {}
        if now - float(item.get("created_at") or 0) > PENDING_TTL:
            expired = data["pending"].pop(key)
            _push_history(data, expired, "expired")
    if len(data["history"]) > MAX_HISTORY:
        data["history"] = data["history"][-MAX_HISTORY:]


def _push_history(data: Dict, item: Dict, decision: str) -> None:
    parsed = item.get("parsed") or {}
    data["history"].append(
        {
            "seq": item.get("seq"),
            "uid": str(item.get("uid") or ""),
            "name": str(item.get("name") or ""),
            "game": str(item.get("game") or ""),
            "id_str": str(parsed.get("id_str") or ""),
            "title": str(parsed.get("title") or parsed.get("text") or "")[:40],
            "decision": decision,
            "ts": time.time(),
        }
    )


# ---------------------------------------------------------------------------
# 会话目标
# ---------------------------------------------------------------------------
def format_target(bot_id: str, kind: str, target_id: str) -> str:
    """拼成配置里存的字符串：``qq:group:123456``（bot_id 为空则 ``group:123456``）。"""
    parts = [p for p in (str(bot_id or "").strip(), str(kind or "group"), str(target_id)) if p]
    return ":".join(parts)


def parse_target(text: str) -> Optional[Tuple[str, str, str]]:
    """解析配置里的目标；返回 ``(bot_id, kind, target_id)``，不合法返回 None。"""
    parts = [p.strip() for p in str(text or "").split(":") if p.strip()]
    if len(parts) == 2:
        return "", parts[0], parts[1]
    if len(parts) >= 3:
        return parts[0], parts[-2], parts[-1]
    return None


# ---------------------------------------------------------------------------
# 队列操作
# ---------------------------------------------------------------------------
async def enqueue(uid: str, name: str, parsed: Dict, game: str = "") -> Dict:
    """把一个没命中白名单的动态放进待定队列，返回队列项（含编号 ``seq``）。

    ``game`` 是这条动态所属的游戏，只用来展示，让拍板的人一眼知道按哪套口径看。
    """
    async with _lock:
        data = _load()
        _prune(data)
        data["seq"] = int(data.get("seq") or 0) + 1
        seq = data["seq"]
        item = {
            "seq": seq,
            "uid": str(uid),
            "name": str(name or parsed.get("uname") or ""),
            "game": str(game or ""),
            "parsed": parsed,
            "created_at": time.time(),
        }
        data["pending"][str(seq)] = item
        # 同一个 UP 攒太多没意义，只留最近的 50 条
        same_uid = [
            k for k, v in data["pending"].items() if str((v or {}).get("uid")) == str(uid)
        ]
        if len(same_uid) > 50:
            for key in same_uid[: len(same_uid) - 50]:
                dropped = data["pending"].pop(key)
                _push_history(data, dropped, "expired")
        _save(data)
        return item


async def pending_items() -> List[Dict]:
    """待定列表（按编号升序）。"""
    async with _lock:
        data = _load()
        _prune(data)
        _save(data)
        return sorted(data["pending"].values(), key=lambda x: int(x.get("seq") or 0))


async def count() -> int:
    """待定条数（状态卡片用）。"""
    async with _lock:
        data = _load()
        _prune(data)
        _save(data)
        return len(data["pending"])


async def take(seq: str, decision: str) -> Optional[Dict]:
    """从待定队列取出编号 seq 的项并记一笔历史，返回该项。"""
    async with _lock:
        data = _load()
        item = data["pending"].pop(str(seq), None)
        if item is None:
            return None
        _push_history(data, item, decision)
        _prune(data)
        _save(data)
        return item


async def history(limit: int = 10) -> List[Dict]:
    """最近的决定记录（新的在前）。"""
    async with _lock:
        data = _load()
        return list(reversed(data["history"][-max(1, limit):]))


# ---------------------------------------------------------------------------
# 通知与推送
# ---------------------------------------------------------------------------
def _clip(text: str, limit: int = 120) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def format_notice(item: Dict) -> Tuple[str, List[str]]:
    """待定项的文案 + 配图。"""
    parsed = item.get("parsed") or {}
    game = str(item.get("game") or "")
    lines = [
        f"【B站订阅·待定 #{item.get('seq')}】" + (f"《{game}》" if game else ""),
        f"{item.get('name') or parsed.get('uname') or '未知UP'} {parsed.get('action') or '发布了动态'}",
    ]
    if parsed.get("title"):
        lines.append(f"标题：{_clip(parsed['title'], 60)}")
    if parsed.get("text"):
        lines.append(f"内容：{_clip(parsed['text'])}")
    if parsed.get("url"):
        lines.append(f"链接：{parsed['url']}")
    total_images = len([i for i in (parsed.get("images") or []) if i])
    if total_images > 1:
        lines.append(f"（共 {total_images} 张图/动图，推送时卡片放首图、其余单独发）")
    lines.append("")
    lines.append(f"推送：可以推送 {item.get('seq')}    过滤：bili跳过 {item.get('seq')}")
    lines.append("（不处理就是不推；也可以 可以推送 全部 / bili跳过 全部）")
    return "\n".join(lines), [i for i in (parsed.get("images") or []) if i][:3]


async def notify_pending(item: Dict) -> int:
    """把待定项通知到所有接收会话，返回发出去的会话数。"""
    text, images = format_notice(item)
    targets = cfg.get_list("review_targets")
    if not targets:
        logger.warning(
            f"B站订阅：UID {item.get('uid')} 的动态进了待定队列，但没配通知会话（bili待定 在这里）"
        )
        return 0

    from gsuid_core.segment import MessageSegment

    msgs = [MessageSegment.text(text)]
    if cfg.get("push_cover"):
        for url in images:
            try:
                msgs.append(MessageSegment.image(url))
            except Exception as e:
                logger.warning(f"B站订阅：待定通知附图失败（{url}）：{e}")

    sent = 0
    for raw in targets:
        parsed_target = parse_target(raw)
        if not parsed_target:
            continue
        bot_id, kind, target_id = parsed_target
        if await send_to_target(kind, target_id, msgs, {"bot_id": bot_id}):
            sent += 1
    return sent


async def approve(seq: str) -> Tuple[bool, str]:
    """推送待定项到订阅群。返回 ``(是否成功, 给管理员的回复)``。"""
    item = await take(seq, "pushed")
    if item is None:
        return False, f"没找到待定项 #{seq}（可能已经处理过或过期了）"

    uid = str(item.get("uid") or "")
    parsed = item.get("parsed") or {}
    name = str(item.get("name") or "")

    subs = await storage.load_subs()
    groups = (subs.get(uid) or {}).get("groups") or {}
    if not groups:
        return False, f"#{seq} 已通过，但 UID {uid} 现在没有任何群在订阅，没处可推"

    # 长文在入队时就抓好全文了（入队那边会先 fulltext.nodes_for），这里直接用
    msgs, forward = await build_dynamic_msgs(parsed, name)
    sent = await push_to_groups(
        groups, msgs, forward, fallback_text=format_dynamic(parsed, name)["text"]
    )
    if parsed.get("bvid"):
        # 视频类（理论上走不到这里）也记一下，避免投稿列表重复推
        pushed = list((subs.get(uid) or {}).get("pushed_av") or [])
        if str(parsed["bvid"]) not in pushed:
            pushed.append(str(parsed["bvid"]))
        await storage.merge_state({uid: {"pushed_av": pushed}})
    logger.info(f"B站订阅：待定 #{seq}（UID {uid}）已推送，{sent}/{len(groups)} 个群")
    return True, f"#{seq} 已推送到 {sent}/{len(groups)} 个群"


async def skip(seq: str) -> Tuple[bool, str]:
    """过滤掉待定项（只记录，不推送）。"""
    item = await take(seq, "skipped")
    if item is None:
        return False, f"没找到待定项 #{seq}（可能已经处理过或过期了）"
    logger.info(f"B站订阅：待定 #{seq}（UID {item.get('uid')}）已过滤")
    return True, f"#{seq} 已过滤，不会推送"


async def approve_all() -> Tuple[int, int, str]:
    """把所有待定项全部推送，返回 ``(成功数, 总数, 汇总文案)``。"""
    items = await pending_items()
    if not items:
        return 0, 0, "当前没有待定的动态"
    ok = 0
    for item in items:
        passed, _ = await approve(str(item.get("seq")))
        if passed:
            ok += 1
    return ok, len(items), f"已推送 {ok}/{len(items)} 条待定动态"


async def skip_all() -> Tuple[int, int, str]:
    """把所有待定项全部过滤。"""
    items = await pending_items()
    if not items:
        return 0, 0, "当前没有待定的动态"
    ok = 0
    for item in items:
        passed, _ = await skip(str(item.get("seq")))
        if passed:
            ok += 1
    return ok, len(items), f"已过滤 {ok}/{len(items)} 条待定动态"
