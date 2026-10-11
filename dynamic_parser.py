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

"""polymer 动态条目的解析（纯函数）。

数据源是 ``x/polymer/web-dynamic/v1/feed/space`` 的 ``data.items``，
每个 item 结构大致为::

    {
        "id_str": "1148459269840961545",
        "type": "DYNAMIC_TYPE_AV",
        "modules": {
            "module_author": {"name": "...", "pub_ts": 1766234911},
            "module_dynamic": {"desc": {...}, "major": {...}},
        },
        "orig": {...},          # 转发动态带原动态
    }

动态 id 是递增的纯数字串，但位数不固定（老动态 18 位，新动态 19 位），
比较新旧必须按数值而不能按字符串，否则新动态会被判成"更旧"。
"""
import json
from typing import Any, Dict, List, Optional

# 预览消息最多带几张图（卡片只放首图，这个数只给"退回文字发法"用）
MAX_IMAGES = 3
# 一条动态最多收多少张图。
# 注意别在这里截断成 3 张：B站的 opus 图文会把 10 张图全放在 pics 里，
# 之前截断导致后面的图既没进预览也没进转发（"只推了前 3 张"就是这么来的）。
MAX_ALL_IMAGES = 20
# 预览之外还能带进合并转发的图（长图/动图多的时候用，避免一条消息发十几张）
MAX_FORWARD_IMAGES = 12
# 单轮最多补推几条动态
MAX_PUSH_PER_ROUND = 5

_T_BUF = "https://t.bilibili.com/"

_TYPE_KIND = {
    "DYNAMIC_TYPE_AV": "video",              # 投稿视频
    "DYNAMIC_TYPE_PGC_UNION": "video",       # 番剧/影视
    "DYNAMIC_TYPE_UGC_SEASON": "video",      # 合集
    "DYNAMIC_TYPE_DRAW": "draw",             # 图文
    "DYNAMIC_TYPE_ARTICLE": "article",       # 专栏
    "DYNAMIC_TYPE_WORD": "word",             # 纯文字
    "DYNAMIC_TYPE_FORWARD": "forward",       # 转发
    "DYNAMIC_TYPE_LIVE_RCMD": "live",        # 直播开播卡片
    "DYNAMIC_TYPE_LIVE": "live",
    "DYNAMIC_TYPE_COMMON_SQUARE": "video",   # 小视频
    "DYNAMIC_TYPE_COMMON_VERTICAL": "video",
    "DYNAMIC_TYPE_MUSIC": "music",           # 音频
}

KIND_ACTION = {
    "video": "投稿了视频",
    "draw": "发布了图文动态",
    "article": "投稿了文章",
    "word": "发布了动态",
    "forward": "转发了动态",
    "live": "开启了直播",
    "music": "投稿了音频",
    "other": "发布了新动态",
}


def _fix_url(u: Optional[str]) -> str:
    u = str(u or "").strip()
    if u.startswith("//"):
        return "https:" + u
    return u


# 只把 avif 的后缀改掉：avif 基本没人支持，而且它一定是静态图。
# **webp 不要动**：B站的动图有时就是动图 webp，改成 jpg 等于把动画丢了
# （这就是"动图变静态图"的一个来源）。宁可直接把原链接发出去，让平台按浏览器的方式取图。
_IMG_SUFFIX_FORMATS = ("avif",)


def normalize_image_url(url: Optional[str]) -> str:
    """图片链接：**尽量原样发**，只做三件必要的事。

    1. 补协议（``//i0.hdslb.com/...`` → ``https://i0.hdslb.com/...``）；
    2. B站自家 CDN 的 ``http://`` 升 ``https://``（opus 页面里给的图就是
       ``http://i0.hdslb.com/...``，http 图在客户端属于混合内容，可能直接不加载）；
    3. 只把 ``@参数.avif`` 这类 avif 渲染后缀换成 ``jpg``（avif 客户端基本发不出来，
       而且 avif 一定是静态的）。

    ``.gif`` / ``.webp`` 一律不动：B站的动图就是这两种（可能是一张 7.8MB 的动图
    webp），任何转码/压缩都可能让"动的图"变成"静态图"。非 B站 CDN 的链接也不动。
    """
    u = _fix_url(url)
    if not u:
        return ""
    if u.startswith("http://") and "hdslb.com" in u:
        u = "https://" + u[len("http://"):]
    base, sep, suffix = u.partition("@")
    if not sep or "hdslb.com" not in base:
        return u
    if base.lower().endswith((".gif", ".webp")):
        return u
    head, dot, fmt = suffix.rpartition(".")
    if dot and fmt.lower() in _IMG_SUFFIX_FORMATS:
        return f"{base}@{head}.jpg"
    return u


def is_animated_url(url: Optional[str]) -> bool:
    """看着像动图吗（``.gif`` / 动图 ``.webp``）。

    只用来决定"要不要单独发、不要拼进卡片" —— 判断不出来也没关系，
    反正现在图片一律原样发，不做转码。
    """
    path = str(url or "").split("?")[0].lower()
    return path.endswith((".gif", ".webp"))


def _fix_img(u: Optional[str]) -> str:
    """图片字段用：补协议 + 统一格式后缀。"""
    return normalize_image_url(u)


def _modules(item: Dict) -> Dict:
    """取 ``modules`` 并**统一成 dict**。

    接口（feed / detail）给的是按模块名索引的 dict；而 **opus 页面 state 里给的是
    一个 list**（每项只带一个 ``module_xxx``，其余全是 null），所以这里把 list 合并
    成 dict —— 两种形态解析器都能吃，否则页面数据一进来就 AttributeError。
    """
    modules = (item or {}).get("modules")
    if isinstance(modules, dict):
        return modules
    if isinstance(modules, list):
        merged: Dict[str, Any] = {}
        for holder in modules:
            if not isinstance(holder, dict):
                continue
            for key, value in holder.items():
                if value is not None and key not in merged:
                    merged[key] = value
        return merged
    return {}


def _desc_text(item: Dict) -> str:
    desc = (_modules(item).get("module_dynamic") or {}).get("desc")
    if isinstance(desc, dict):
        return str(desc.get("text") or "")
    return ""


def _major(item: Dict) -> Dict:
    return (_modules(item).get("module_dynamic") or {}).get("major") or {}


def _id_key(id_str: Any):
    """把 id_str 换成可比较的键：纯数字按数值，其余按字符串兜底。"""
    s = str(id_str or "").strip()
    if s.isdigit():
        return (1, int(s), "")
    return (0, 0, s)


def is_newer_id(new_id: Any, old_id: Any) -> bool:
    """new_id 是否比 old_id 新（按数值比较）。"""
    if not str(new_id or "").strip():
        return False
    if not str(old_id or "").strip():
        return True
    return _id_key(new_id) > _id_key(old_id)


def latest_id(items: List[Dict]) -> Optional[str]:
    """一批动态里最新的 id_str。"""
    best: Optional[str] = None
    for it in items or []:
        if not isinstance(it, dict):
            continue
        cur = str(it.get("id_str") or "").strip()
        if not cur:
            continue
        if best is None or _id_key(cur) > _id_key(best):
            best = cur
    return best


def select_new_dynamics(
    items: List[Dict], last_id: Optional[str], limit: int = MAX_PUSH_PER_ROUND
) -> List[Dict]:
    """挑出比 last_id 新的动态，按时间正序返回，最多 limit 条。"""
    valid = [
        it for it in (items or [])
        if isinstance(it, dict) and str(it.get("id_str") or "").strip()
    ]
    new_items = [it for it in valid if is_newer_id(it.get("id_str"), last_id)]
    new_items.sort(key=lambda it: _id_key(it.get("id_str")))
    if limit and len(new_items) > limit:
        return new_items[-limit:]
    return new_items


def _id_from_url(url: str, seg: str) -> str:
    """从 ``https://www.bilibili.com/opus/123456`` 这类链接里抠出 id。"""
    import re

    m = re.search(rf"/{seg}/(\d+)", str(url or ""))
    return m.group(1) if m else ""


def classify(item: Dict) -> str:
    """把一条动态归为内部 kind：优先顶层 type，再按 major.type 兜底。"""
    t = str((item or {}).get("type", ""))
    if t in _TYPE_KIND:
        return _TYPE_KIND[t]
    mt = str(_major(item).get("type", "")).upper()
    if mt in ("MAJOR_TYPE_ARCHIVE", "MAJOR_TYPE_PGC", "MAJOR_TYPE_UGC_SEASON", "MAJOR_TYPE_COMMON"):
        return "video"
    if mt == "MAJOR_TYPE_DRAW":
        return "draw"
    if mt in ("MAJOR_TYPE_OPUS", "MAJOR_TYPE_ARTICLE"):
        return "article"
    if mt == "MAJOR_TYPE_WORD":
        return "word"
    if mt in ("MAJOR_TYPE_LIVE", "MAJOR_TYPE_LIVE_RCMD"):
        return "live"
    return "other"


def _opus_text(opus: Dict) -> str:
    summary = opus.get("summary") or {}
    if isinstance(summary, dict):
        return str(summary.get("text") or "")
    return ""


def _opus_images(opus: Dict) -> List[str]:
    """opus 图文的所有配图。

    这里**不能按 MAX_IMAGES 截断**：一条 opus 动辄 7～10 张（还夹着 .gif 动图），
    截断到 3 张的话剩下的既不会进预览、也不会进转发，等于整条动态的图丢一半。
    """
    out: List[str] = []
    for p in (opus.get("pics") or [])[:MAX_ALL_IMAGES]:
        u = _fix_img((p or {}).get("url"))
        if u:
            out.append(u)
    return out


def _live_rcmd(major: Dict) -> Dict[str, Any]:
    """live_rcmd 的内容塞在 content 字符串里，尝试解一层 JSON。"""
    raw = major.get("live_rcmd")
    if not isinstance(raw, dict):
        return {}
    content = raw.get("content")
    if isinstance(content, str) and content.strip().startswith("{"):
        try:
            parsed = json.loads(content)
        except Exception:
            return {}
        if isinstance(parsed, dict) and isinstance(parsed.get("live_play_view"), dict):
            return parsed["live_play_view"]
        return parsed if isinstance(parsed, dict) else {}
    return raw


def extract_dynamic(item: Dict, depth: int = 0) -> Dict[str, Any]:
    """把一条动态解析成统一结构（纯函数）。

    返回 ``{id_str, kind, uname, mid, action, title, text, images,
    url, pub_ts, bvid, cv_id, opus_id}``。

    - ``images``：**全部**配图（按顺序，最多 ``MAX_ALL_IMAGES`` 张）。推送时第 1 张
      进卡片，其余单独补发；会动的 ``.gif`` 原样保留，转成 jpg 就没动画了；
    - ``cv_id`` / ``opus_id``：长文抓全文、手动补推时定位用。
    """
    modules = _modules(item)
    author = modules.get("module_author") or {}
    dyn = modules.get("module_dynamic") or {}
    major = dyn.get("major") or {}

    id_str = str((item or {}).get("id_str") or "")
    kind = classify(item)
    uname = str(author.get("name") or "")
    try:
        pub_ts = int(author.get("pub_ts") or 0)
    except (TypeError, ValueError):
        pub_ts = 0

    title = ""
    text = _desc_text(item)
    images: List[str] = []
    url = _T_BUF + id_str if id_str else ""
    bvid = ""
    cv_id = ""    # 专栏号（read/cv<id>），长文抓全文要用
    opus_id = ""  # opus 号（www.bilibili.com/opus/<id>），同上

    archive = major.get("archive") or {}
    opus = major.get("opus") or {}
    draw = major.get("draw") or {}
    article = major.get("article") or {}
    live = major.get("live") or _live_rcmd(major)
    common = major.get("common") or {}
    music = major.get("music") or {}

    if archive:
        kind = "video"
        title = str(archive.get("title") or "")
        text = text or str(archive.get("desc") or "")
        cover = _fix_img(archive.get("cover"))
        if cover:
            images.append(cover)
        bvid = str(archive.get("bvid") or "")
        url = _fix_url(archive.get("jump_url")) or (
            f"https://www.bilibili.com/video/{bvid}" if bvid else url
        )
    elif opus:
        otitle = str(opus.get("title") or "")
        kind = "article" if otitle else "draw"
        title = otitle
        text = text or _opus_text(opus)
        images.extend(_opus_images(opus))
        url = _fix_url(opus.get("jump_url")) or url
        opus_id = _id_from_url(url, "opus") or str(opus.get("id") or "")
    elif draw and draw.get("items"):
        kind = "draw"
        images.extend(
            _fix_img(d.get("src")) for d in (draw.get("items") or []) if d.get("src")
        )
    elif article:
        kind = "article"
        title = str(article.get("title") or "")
        text = text or str(article.get("desc") or "")
        for c in (article.get("covers") or [])[:MAX_ALL_IMAGES]:
            if c:
                images.append(_fix_img(c))
        cv_id = str(article.get("id") or "")
        url = _fix_url(article.get("jump_url")) or (
            f"https://www.bilibili.com/read/cv{cv_id}" if cv_id else url
        )
        opus_id = _id_from_url(url, "opus")
    elif common:
        kind = "video"
        title = str(common.get("title") or "")
        text = text or str(common.get("desc") or "")
        cover = _fix_img(common.get("cover"))
        if cover:
            images.append(cover)
        url = _fix_url(common.get("jump_url")) or url
    elif music:
        kind = "music"
        title = str(music.get("title") or "")
        cover = _fix_img(music.get("cover"))
        if cover:
            images.append(cover)
    elif live:
        kind = "live"
        title = str(live.get("title") or "")
        cover = _fix_img(live.get("cover") or live.get("cover_small"))
        if cover:
            images.append(cover)
        url = _fix_url(live.get("jump_url") or live.get("link")) or url

    # 页面形态的 opus：标题放在 module_title 里（major 里没有）
    if not title:
        title_mod = modules.get("module_title")
        if isinstance(title_mod, dict):
            title = str(title_mod.get("text") or "")

    # 页面形态的 opus 没有 major，但带 module_content（段落正文）：
    # 按"有标题=文章、没标题=图文动态"归档，别一路落成 other
    if kind == "other" and modules.get("module_content"):
        kind = "article" if title else "draw"

    # 转发动态：把原动态概要拼进正文（只递归一层）
    if kind == "forward" and depth == 0:
        orig = (item or {}).get("orig")
        if isinstance(orig, dict) and orig.get("modules"):
            sub = extract_dynamic(orig, depth=1)
            sub_head = f"\n⤷ 原@{sub['uname']}" if sub.get("uname") else "\n⤷ 原动态"
            sub_body = sub.get("title") or sub.get("text") or ""
            text = (text + sub_head + ("：" + sub_body if sub_body else "")).strip()
            for im in sub.get("images", []):
                if len(images) >= MAX_ALL_IMAGES:
                    break
                images.append(im)
        else:
            text = (text + "\n⤷ 原动态已删除或不可见").strip()

    kept = [i for i in images if i]
    # 带货/商品动态：``additional.type`` 里带 goods（老接口是 type=19），
    # 或 major.type 是 GOODS —— 这是**结构字段**，比关键词靠谱（不会误伤正常动态），
    # 广告过滤（adfilter）拿它当第一判据
    additional = dyn.get("additional")
    if not isinstance(additional, dict):
        additional = {}
    goods = (
        "goods" in str(additional.get("type") or "").lower()
        or "GOODS" in str(major.get("type") or "").upper()
    )
    return {
        "id_str": id_str,
        "kind": kind,
        "goods": goods,
        "uname": uname,
        "mid": str(author.get("mid") or ""),  # 作者 UID（手动补推时用来找订阅群）
        "avatar": _fix_img(author.get("face")),  # 作者头像（卡片头部用）
        "action": KIND_ACTION.get(kind, KIND_ACTION["other"]),
        "title": title,
        "text": text,
        # 全部配图（按顺序）：第 1 张进推送卡片，其余由推送层单独补发
        "images": kept[:MAX_ALL_IMAGES],
        "url": url,
        "pub_ts": pub_ts,
        "bvid": bvid,
        "cv_id": cv_id,
        "opus_id": opus_id,
    }
