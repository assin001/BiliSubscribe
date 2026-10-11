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

"""长文动态的全文抓取 + 合并转发节点构造。

版本更新公告这类动态正文很长，一条消息塞进去会刷屏，所以推送分两条：

1. **预览**：标题 + 摘要 + 封面（就是 ``format_dynamic`` 那条，和以前一样）；
2. **全文**：一条**合并转发**（``MessageSegment.node``），里面按段落切块，
   文章里的图片也按顺序放进去。

全文来源按"现代 → 老接口 → 页面"依次尝试，任一成功即用，全失败就只发预览
（预览里本来就有原文链接，不会丢信息）：

==============  ==========================================================
来源            接口 / 说明
==============  ==========================================================
opus 详情       ``x/polymer/web-dynamic/v1/opus/detail?id=<opus_id>``
                → ``data.item.modules.module_content.paragraphs``（段落模型）
动态详情        ``x/polymer/web-dynamic/v1/detail?id=<动态id>``（正文更全）
opus 页面       ``https://www.bilibili.com/opus/<动态id>``（点开动态就是这页，
                服务端渲染、正文直出，接口被风控时靠它）
老专栏正文      ``x/article/view?id=<cv>`` → ``data.content``（HTML）
专栏页面        ``https://www.bilibili.com/read/cv<cv>/`` → 页面里的
                ``__INITIAL_STATE__`` 或正文容器（HTML）
==============  ==========================================================

注意：**opus id 和动态 id 是同一个数**（动态点开后的链接是
``www.bilibili.com/opus/<动态id>``），所以没解析出 opus id 时直接拿动态 id 去问，
专栏类动态（``cv<号>``）也照样能通过 opus 接口/页面拿到全文。

抓取结果统一成"有序片段"：``{"type": "text", "text": ...}`` /
``{"type": "image", "url": ...}``，这样转发出来能保持图文顺序。
"""
import json
import re
from html import unescape
from typing import Any, Dict, List, Optional, Tuple

from gsuid_core.logger import logger
from gsuid_core.models import Message
from gsuid_core.segment import MessageSegment

from . import config as cfg
from .bili_api import api
from .dynamic_parser import extract_dynamic, normalize_image_url

# 一条合并转发里最多放几张图
MAX_FORWARD_IMAGES = 6
# 正文容器的 class（F12 看到的：长文在 opus-module-content / opus-paragraph-children 里）
OPUS_BODY_CLASSES = ("opus-module-content", "opus-paragraph-children", "bili-dyn-content")
# 每个转发节点最多多少字（短文的切法）
NODE_CHUNK = 500
# 一条合并转发**最多切几段**：平台对转发条数普遍有上限（超了会截断、甚至整条发不出去，
# 实测 6000 字公告按 500 字切 = 16 段就是"长文转发失败、短文正常"的原因），
# 所以长文自动把每段字数放大（最多 NODE_CHUNK_MAX），把段数压回 MAX_NODES 以内
MAX_NODES = 8
NODE_CHUNK_MAX = 2000
# 少于这个字数就不折腾全文了（预览一条就够）
MIN_FULL_LEN = 200


# ---------------------------------------------------------------------------
# 要不要抓全文
# ---------------------------------------------------------------------------
def needs_full(parsed: Dict) -> bool:
    """这条动态值不值得抓全文。

    专栏/opus（``kind == article``）一定抓（正文铁定比摘要长）；
    其它类型看摘要是否被截断过（带"全文"字样）或者本身就偏长。
    """
    if not cfg.get("push_full_text", True):
        return False
    kind = str(parsed.get("kind") or "")
    if kind == "article":
        return True
    text = str(parsed.get("text") or "")
    if "全文" in text:
        return True
    return len(text) >= MIN_FULL_LEN


# ---------------------------------------------------------------------------
# HTML / 段落 → 有序片段
# ---------------------------------------------------------------------------
def _image_url(raw: Any) -> str:
    """页面里抠出来的图片地址：补协议、统一格式后缀（avif/webp → jpg）、丢掉 data: 占位。"""
    url = str(raw or "").strip()
    if not url or url.startswith("data:"):
        return ""
    return normalize_image_url(url)


def _clean_text(text: str) -> str:
    text = unescape(str(text or ""))
    text = text.replace("\u200b", "").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip("\n")


def _as_dict(value: Any) -> Dict:
    """线上返回里同一个字段可能是 dict，也可能是 list（踩过：opus 的 major 是 list）。"""
    return value if isinstance(value, dict) else {}


def html_to_parts(html: str) -> List[Dict]:
    """把文章 HTML 转成有序片段（文本 / 图片），顺序按出现位置。"""
    if not html:
        return []
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", html)
    parts: List[Dict] = []
    pos = 0
    # 图片（img / picture 里的 source）、以及会造成换行的块级标签，都当作切分点
    token = re.compile(
        r"(?is)<img[^>]*>|<source[^>]*>|</?(?:p|div|h\d|li|tr|br|section|figure)[^>]*>"
    )
    for m in token.finditer(html):
        chunk = html[pos:m.start()]
        pos = m.end()
        tag = m.group(0)
        text = _clean_text(re.sub(r"(?s)<[^>]+>", "", chunk))
        if text:
            parts.append({"type": "text", "text": text})
        low = tag.lower()
        if low.startswith("<img"):
            # 页面版正文里的图常常是懒加载：真图在 data-src，src 是占位图
            src = re.search(r'\bdata-src=["\']([^"\']+)["\']', tag) or re.search(
                r'\bsrc=["\']([^"\']+)["\']', tag
            )
            url = _image_url(src.group(1) if src else "")
            if url:
                parts.append({"type": "image", "url": url})
        elif low.startswith("<source"):
            # <picture><source type="image/webp" srcset="..."> —— 比同块的 <img>(avif) 更通用
            srcset = re.search(r'\bsrcset=["\']([^"\']+)["\']', tag)
            url = _image_url(srcset.group(1).split(",")[0].strip() if srcset else "")
            if url:
                parts.append({"type": "image", "url": url})
        elif low.startswith(("</p", "</div", "</h", "</li", "</tr", "<br", "</section", "</figure")):
            parts.append({"type": "text", "text": "\n"})

    tail = _clean_text(re.sub(r"(?s)<[^>]+>", "", html[pos:]))
    if tail:
        parts.append({"type": "text", "text": tail})
    return _merge_parts(parts)


def _merge_parts(parts: List[Dict]) -> List[Dict]:
    """合并相邻文本、去掉空片段、压缩空行；图片去重并限量。"""
    out: List[Dict] = []
    images = 0
    for part in parts:
        if part.get("type") == "image":
            url = str(part.get("url") or "").strip()
            if not url or images >= MAX_FORWARD_IMAGES:
                continue
            if any(p.get("type") == "image" and p.get("url") == url for p in out):
                continue
            out.append({"type": "image", "url": url})
            images += 1
            continue
        text = str(part.get("text") or "")
        if not text.strip():
            continue
        if out and out[-1].get("type") == "text":
            out[-1]["text"] = f"{out[-1]['text']}\n{text}".strip("\n")
        else:
            out.append({"type": "text", "text": text.strip("\n")})

    for item in out:
        if item.get("type") == "text":
            item["text"] = re.sub(r"\n{3,}", "\n\n", item["text"]).strip()
    return [p for p in out if p.get("type") != "text" or p.get("text")]


def _node_text(node: Dict) -> str:
    """一个段落节点里**人眼能看到**的文字。

    节点类型比文档里多得多，逐个兜：
    ``WORD``（普通文字）、``RICH``（话题/表情/链接/@，文字在 ``rich.text``）、
    ``AT``、``EMOJI``、``WEB``（外链卡片，取标题）、``FORMULA``（公式）。
    这样拼出来的正文才和页面上看到的一致 —— 之前只认 WORD，结果
    ``#异环#`` 这类话题（RICH）整段丢了。
    """
    ntype = str(node.get("type") or "")
    if ntype == "TEXT_NODE_TYPE_AT":
        for key in ("user", "at"):
            group = _as_dict(node.get(key))
            name = str(group.get("name") or "").strip()
            if name:
                return f"@{name}"
        return ""
    if ntype == "TEXT_NODE_TYPE_EMOJI":
        emoji = _as_dict(node.get("emoji"))
        return str(emoji.get("text") or "").strip()
    if ntype == "TEXT_NODE_TYPE_WEB":
        web = _as_dict(node.get("web"))
        title = str(web.get("title") or "").strip()
        url = str(web.get("url") or web.get("jump_url") or "").strip()
        return f"{title} {url}".strip()
    if ntype == "TEXT_NODE_TYPE_FORMULA":
        formula = str(_as_dict(node.get("formula")).get("latex_content") or "")
        return f"[公式] {formula}" if formula else ""
    if ntype == "TEXT_NODE_TYPE_RICH":
        rich = _as_dict(node.get("rich"))
        return str(rich.get("text") or rich.get("orig_text") or "").strip()
    # WORD 走这里；没写 type 的也当普通文字
    word = _as_dict(node.get("word"))
    text = str(word.get("words") or node.get("text") or "")
    if not text and ntype == "TEXT_NODE_TYPE_WORD":
        return ""
    return text


def _node_images(node: Dict) -> List[str]:
    """节点里夹的图（RICH 带图、表情图标、视频封面这类）。"""
    rich = _as_dict(node.get("rich"))
    out: List[str] = []
    for pic in _as_dict(rich.get("pic")).get("pics") or []:
        url = _image_url(_as_dict(pic).get("src") or _as_dict(pic).get("url"))
        if url:
            out.append(url)
    return out


def _link_card_text(para: Dict) -> str:
    """``para_type 6`` 的链接卡片（B站常见的"游戏/视频卡片"）。

    页面上它显示的是一张卡：标题 + 简介 + 按钮。这里降级成一行文字，
    卡片里夹的图就不发了（那多半是宣传图，正文才是要看的东西）。
    """
    card = _as_dict(_as_dict(para.get("link_card")).get("card"))
    for branch in ("common", "archive", "live", "music", "goods", "ugc"):
        node = _as_dict(card.get(branch))
        if not node:
            continue
        title = str(node.get("title") or node.get("name") or "").strip()
        desc = str(node.get("desc1") or node.get("desc") or "").strip()
        url = str(node.get("jump_url") or node.get("url") or "").strip()
        line = "｜".join(x for x in (title, desc) if x)
        if url:
            line = f"{line} {url}".strip()
        if line:
            return line
    return ""


def paragraphs_to_parts(paragraphs: Any) -> List[Dict]:
    """opus 段落模型 → 有序片段（文本 / 图片，按原文顺序）。

    线上段落按 ``para_type`` 分几种（都见过）：``1`` 文字、``2`` 图片、
    ``3`` 分割线/表格、``4``/``5`` 列表/代码、``6`` 链接卡片。挑主流的处理，
    认不出来的整段跳过（宁可少一段，也不要把引用/投票那些拼成乱码）。
    """
    parts: List[Dict] = []
    if not isinstance(paragraphs, list):
        return parts
    for para in paragraphs:
        para = _as_dict(para)
        ptype = para.get("para_type")

        # ① 段落级图片：``{"para_type": 2, "pic": {"pics": [{"url": ...}]}}``
        for pic in _as_dict(para.get("pic")).get("pics") or []:
            url = _image_url(_as_dict(pic).get("url") or _as_dict(pic).get("src"))
            if url:
                parts.append({"type": "image", "url": url})

        # ② 文本段落：nodes 里按顺序取文字，夹着的图插在原位
        nodes = _as_dict(para.get("text")).get("nodes")
        for node in nodes if isinstance(nodes, list) else []:
            node = _as_dict(node)
            for url in _node_images(node):
                parts.append({"type": "image", "url": url})
            text = _node_text(node)
            if text:
                parts.append({"type": "text", "text": text})

        # ③ 链接卡片
        card_text = _link_card_text(para)
        if card_text:
            parts.append({"type": "text", "text": card_text})

        # ④ 分割线：换行就够（别丢，正文分段全靠它）
        if para.get("line") is not None or ptype in (3,):
            parts.append({"type": "text", "text": "\n"})
        parts.append({"type": "text", "text": "\n"})
    return _merge_parts(parts)


# ---------------------------------------------------------------------------
# 各级抓取
# ---------------------------------------------------------------------------
def _item_of(body: Dict) -> Dict:
    data = _as_dict(_as_dict(body).get("data"))
    item = data.get("item")
    if isinstance(item, dict):
        return item
    items = data.get("items")
    if isinstance(items, list) and items:
        return _as_dict(items[0])
    return {}


def _paragraphs_of(item: Any) -> List[Dict]:
    """从动态/opus item 里找段落模型（位置和类型在不同版本里都不太一样）。

    线上见过三种形态：
    - 接口：``{"modules": {"module_dynamic": {"major": {"opus": {"summary": ...}}}}}``
    - 详情：``{"modules": {"module_content": {"paragraphs": [...]}}}``
    - **页面 state：``{"modules": [ {...}, {"module_content": {"paragraphs": [...]}} ]}``（列表！）**
    """
    # modules 可能是 dict（按模块名索引），也可能是 list（opus 页面里就是 list）
    holders: List[Any] = []
    modules = _as_dict(item).get("modules")
    if isinstance(modules, list):
        holders.extend(modules)
    elif isinstance(modules, dict):
        holders.append(modules)
    else:
        holders.append({})

    # ① 直接挂在 module_content 下的
    for holder in holders:
        paras = _as_dict(_as_dict(holder).get("module_content")).get("paragraphs")
        if isinstance(paras, list) and paras:
            return [p for p in paras if isinstance(p, dict)]

    # ② 再往里一层：module_dynamic / major / opus
    for holder in holders:
        dynamic = _as_dict(_as_dict(holder).get("module_dynamic"))
        major = _as_dict(dynamic.get("major"))
        opus = _as_dict(major.get("opus"))
        for sub in (dynamic, major, opus):
            paras = _as_dict(sub.get("module_content")).get("paragraphs")
            if isinstance(paras, list) and paras:
                return [p for p in paras if isinstance(p, dict)]
    return []


def _state_json_of(html: str) -> Dict:
    """从专栏页面里抠出 ``__INITIAL_STATE__``（可能没有）。"""
    for marker in ("window.__INITIAL_STATE__=", "__INITIAL_STATE__="):
        idx = html.find(marker)
        if idx < 0:
            continue
        start = idx + len(marker)
        end = html.find(";(function", start)
        if end < 0:
            end = html.find("</script>", start)
        if end < 0:
            end = start + 400000
        raw = html[start:end].strip().rstrip(";")
        try:
            data = json.loads(raw)
        except Exception:
            # 老的页面里是 JS 对象字面量，把 undefined 之类的收一下再试
            try:
                data = json.loads(re.sub(r"\bundefined\b", "null", raw))
            except Exception:
                continue
        if isinstance(data, dict):
            return data
    return {}


def _slice_balanced_div(html: str, open_pos: int) -> str:
    """从 ``<div`` 的位置切出配对的 ``</div>``（简单计数，页面结构够用）。"""
    depth = 0
    for m in re.finditer(r"(?is)<div\b[^>]*>|</div\s*>", html[open_pos:]):
        if m.group(0).lower().startswith("</"):
            depth -= 1
            if depth <= 0:
                return html[open_pos: open_pos + m.end()]
        else:
            depth += 1
    return html[open_pos:]


def _container_html(html: str, class_names: Any) -> str:
    """按 class 名找正文容器并切出整块（F12 看到的正文容器就在这里）。"""
    for cls in class_names:
        m = re.search(rf'(?is)<div[^>]+class="[^"]*{re.escape(cls)}[^"]*"', html)
        if m:
            block = _slice_balanced_div(html, m.start())
            if block:
                return block
    return ""


def _first_p_with_images(html: str) -> str:
    """页面只有一个 ``<p>`` 的情况：那个 p 就是正文，它后面的 img 是配图。"""
    m = re.search(r"(?is)<p\b[^>]*>.*?</p>", html)
    if not m:
        return ""
    tail = html[m.end(): m.end() + 300000]
    imgs = re.findall(r"(?is)<img[^>]*>|<source[^>]*>", tail)
    return m.group(0) + "".join(imgs[: MAX_FORWARD_IMAGES * 2])


def _find_paragraphs_in_state(state: Any, depth: int = 0) -> List[Dict]:
    """在页面 state 里递归找段落模型（页面结构的版本差异比较大）。"""
    if depth > 6:
        return []
    if isinstance(state, dict):
        paras = _paragraphs_of(state)
        if paras:
            return paras
        for value in state.values():
            got = _find_paragraphs_in_state(value, depth + 1)
            if got:
                return got
    elif isinstance(state, list):
        for value in state:
            got = _find_paragraphs_in_state(value, depth + 1)
            if got:
                return got
    return []


def _find_html_in_state(state: Any, depth: int = 0) -> str:
    """在 ``__INITIAL_STATE__`` 里挖出最长的那段 HTML 正文。"""
    if depth > 6:
        return ""
    best = ""
    if isinstance(state, dict):
        for value in state.values():
            if isinstance(value, str):
                if 80 < len(value) and "<" in value and ">" in value and len(value) > len(best):
                    best = value
            else:
                got = _find_html_in_state(value, depth + 1)
                if len(got) > len(best):
                    best = got
    elif isinstance(state, list):
        for value in state:
            got = _find_html_in_state(value, depth + 1)
            if len(got) > len(best):
                best = got
    return best


# ---------------------------------------------------------------------------
# 各级抓取
# ---------------------------------------------------------------------------
def _title_of(item: Any) -> str:
    """从 item / 页面 state 里挖标题（模块化的 opus 把标题放在 module_title 里）。"""
    modules = _as_dict(item).get("modules")
    holders = modules if isinstance(modules, list) else [modules]
    for holder in holders:
        node = _as_dict(holder)
        for key in ("module_title", "module_content"):
            group = _as_dict(node.get(key))
            for field in ("text", "title"):
                value = group.get(field)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _author_of(item: Any) -> Dict:
    """从 item / 页面 state 里挖作者（module_author）。"""
    modules = _as_dict(item).get("modules")
    holders = modules if isinstance(modules, list) else [modules]
    for holder in holders:
        author = _as_dict(_as_dict(holder).get("module_author"))
        name = str(author.get("name") or "").strip()
        mid = str(author.get("mid") or "").strip()
        face = str(author.get("face") or "").strip()
        if name or mid:
            return {"uname": name, "mid": mid, "avatar": _image_url(face)}
    return {}


def _item_of_state(state: Any) -> Dict:
    """页面 state 里的那条动态：新版在 ``state["detail"]``（老版直接是 state）。

    ``_title_of`` / ``_author_of`` 读的是 item 里的 ``modules``，直接拿整页 state 去问
    是找不到的（标题/作者都埋在 detail 里）。
    """
    detail = _as_dict(_as_dict(state).get("detail"))
    if detail.get("modules"):
        return detail
    return _as_dict(state)


def _fill_meta(parsed: Dict, item: Any) -> None:
    """正文来源能给标题/作者就补进 parsed。

    手动补推、调试时用得上：只给了 cv 号 / opus id 的时候，plugins 手里这条
    parsed 是空壳，标题和作者都得靠这里补，预览才像样（也才能按作者找订阅群）。
    """
    if not parsed.get("title"):
        title = _title_of(item)
        if title:
            parsed["title"] = title
    author = _author_of(item)
    for key in ("uname", "mid", "avatar"):  # 头像也补上：卡片头部要画
        if author.get(key) and not parsed.get(key):
            parsed[key] = author[key]


def _fill_parts_images(parsed: Dict, parts: List[Dict]) -> None:
    """把正文片段里带出来的图收进 ``parsed["images"]``。

    卡片要放首图、其余要补发，而"正文里的图"是这些页面兜底路径唯一能拿到的图
    （接口路径的图来自 ``extract_dynamic``）。
    """
    urls = [str(u) for u in (parsed.get("images") or []) if str(u or "").strip()]
    for part in parts:
        url = str(part.get("url") or "")
        if part.get("type") == "image" and url and url not in urls:
            urls.append(url)
    if urls:
        parsed["images"] = urls[:20]


def _total_len(parts: List[Dict]) -> int:
    return sum(len(str(p.get("text") or "")) for p in parts if p.get("type") == "text")


def _limit() -> int:
    try:
        value = int(cfg.get("full_text_limit") or 5000)
    except (TypeError, ValueError):
        value = 5000
    return max(200, min(20000, value))


def _cap_parts(parts: List[Dict], limit: int) -> List[Dict]:
    """按字数上限裁剪（超了就截断并留一句提示），图片不受影响。"""
    out: List[Dict] = []
    used = 0
    truncated = False
    for part in parts:
        if part.get("type") == "image":
            out.append(part)
            continue
        text = str(part.get("text") or "")
        if used >= limit:
            truncated = True
            break
        if used + len(text) > limit:
            out.append({"type": "text", "text": text[: max(0, limit - used)]})
            used = limit
            truncated = True
            break
        out.append(part)
        used += len(text)
    if truncated:
        out.append({"type": "text", "text": "\n（正文过长，这里做了截断，完整内容见原文链接）"})
    return out


def opus_candidates(parsed: Dict) -> List[str]:
    """这条动态可以拿去问 opus 接口的 id 列表（去重保序）。

    **opus id 和动态 id 是同一个数**（点开动态的链接就是
    ``www.bilibili.com/opus/<动态id>``），所以没解析出 opus id 时直接拿动态 id 试。
    """
    out: List[str] = []
    for key in ("opus_id", "id_str"):
        value = str(parsed.get(key) or "").strip()
        if value and value not in out:
            out.append(value)
    return out


async def _from_opus(parsed: Dict) -> Tuple[List[Dict], str]:
    for opus_id in opus_candidates(parsed):
        body = await api.get_opus_detail(opus_id)
        if not body:
            continue
        item = _item_of(body)
        _fill_meta(parsed, item)
        parts = paragraphs_to_parts(_paragraphs_of(item))
        if not parts:
            modules = _as_dict(item.get("modules"))
            major = _as_dict(_as_dict(modules.get("module_dynamic")).get("major"))
            opus = _as_dict(major.get("opus"))
            text = str(_as_dict(opus.get("summary")).get("text") or "")
            if text:
                parts = [{"type": "text", "text": text}]
        if parts:
            return parts, "opus详情"
    return [], ""


async def _from_detail(parsed: Dict) -> Tuple[List[Dict], str]:
    dyn_id = str(parsed.get("id_str") or "")
    if not dyn_id:
        return [], ""
    body = await api.get_dynamic_detail(dyn_id)
    if not body:
        return [], ""
    item = _item_of(body)
    _fill_meta(parsed, item)
    parts = paragraphs_to_parts(_paragraphs_of(item))
    if not parts:
        modules = _as_dict(item.get("modules"))
        dynamic = _as_dict(modules.get("module_dynamic"))
        major = _as_dict(dynamic.get("major"))
        text = str(_as_dict(_as_dict(major.get("opus")).get("summary")).get("text") or "")
        text = text or str(_as_dict(dynamic.get("desc")).get("text") or "")
        if len(text) > _total_len([{"type": "text", "text": str(parsed.get("text") or "")}]):
            parts = [{"type": "text", "text": text}]
    return parts, "动态详情"


async def _from_article_api(parsed: Dict) -> Tuple[List[Dict], str]:
    cv_id = str(parsed.get("cv_id") or "")
    if not cv_id:
        return [], ""
    body = await api.get_article_content(cv_id)
    if not body or body.get("code") != 0:
        return [], ""
    data = body.get("data") or {}
    # 专栏接口顺带给标题和作者，补进 parsed：手动补推时预览里能显示标题，
    # 只给 cv 号也能认出作者 UID（这样「订阅群」模式才找得到订阅它的群）
    if data.get("title") and not parsed.get("title"):
        parsed["title"] = str(data.get("title"))
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    if author.get("name") and not parsed.get("uname"):
        parsed["uname"] = str(author["name"])
    if author.get("mid") and not parsed.get("mid"):
        parsed["mid"] = str(author["mid"])
    # 顺带把封面补上：新版专栏的图**不在正文 HTML 里**（content 里 img 数为 0），
    # 单独给在 image_urls / banner_url 里；不补的话 cv 号补推的卡片没有配图
    if not parsed.get("images"):
        raw_images = data.get("image_urls")
        if isinstance(raw_images, str):
            try:
                raw_images = json.loads(raw_images)
            except Exception:
                raw_images = []
        urls = [
            _image_url(item)
            for item in (raw_images if isinstance(raw_images, list) else [])
        ]
        cover = _image_url(data.get("banner_url"))
        if cover:
            urls.insert(0, cover)
        urls = [u for u in urls if u]
        if urls:
            parsed["images"] = urls[:MAX_FORWARD_IMAGES]
    parts = html_to_parts(str(data.get("content") or ""))
    if not parts and data.get("summary"):
        parts = [{"type": "text", "text": str(data.get("summary"))}]
    return parts, "专栏正文接口"


async def _from_opus_page(parsed: Dict) -> Tuple[List[Dict], str]:
    """opus 页面兜底：``www.bilibili.com/opus/<动态id>``（服务端渲染，正文直出）。"""
    for opus_id in opus_candidates(parsed):
        html = await api.get_opus_page(opus_id)
        if not html:
            continue
        state = _state_json_of(html)
        _fill_meta(parsed, _item_of_state(state))
        parts = paragraphs_to_parts(_find_paragraphs_in_state(state)) if state else []
        if not parts and state:
            parts = html_to_parts(_find_html_in_state(state))
        if not parts:
            block = _container_html(html, OPUS_BODY_CLASSES)
            if block:
                parts = html_to_parts(block)
        if not parts:
            # 页面只有一个 <p> 时，它就是正文，后面的 img 是配图
            parts = html_to_parts(_first_p_with_images(html))
        if parts:
            _fill_parts_images(parsed, parts)
            return parts, "opus页面"
    # 注意缩进：这句要在 for **外面**。以前多缩了一层，导致候选 id 为空时（比如只给了
    # cv 号）整个函数返回 None，"fetch" 那边 `parts, _ = ...` 直接抛
    # "cannot unpack non-iterable NoneType object"（探针抓到的）。
    return [], ""


async def parse_opus_page(opus_id: str) -> Optional[Dict]:
    """**从 opus 页面直接解析出一条动态**（手动补推只给了链接时用）。

    为什么单独做一条：接口 ``x/polymer/web-dynamic/...`` 经常整片 **-352 风控**
    （探过：不带 Cookie 时动态详情、opus 详情全是 -352），而
    ``www.bilibili.com/opus/<id>`` 是服务端渲染、正文直出，几条路里最稳的。
    页面里的 ``detail`` 结构和接口 item 基本一致（唯一区别是 ``modules`` 是**列表**，
    解析器那边已经兼容），所以这里直接复用它。

    解析不出东西返回 ``None``（调用方再退文字兜底）。
    """
    opus_id = str(opus_id or "").strip()
    if not opus_id:
        return None
    html = await api.get_opus_page(opus_id)
    if not html:
        return None
    state = _state_json_of(html)
    if not state:
        return None

    detail = _as_dict(_as_dict(state).get("detail"))
    item = _item_of_state(state)
    parsed = extract_dynamic(item)
    _fill_meta(parsed, item)
    if not parsed.get("id_str"):
        parsed["id_str"] = str(detail.get("id_str") or opus_id)
    parsed["opus_id"] = opus_id
    parsed["url"] = f"https://www.bilibili.com/opus/{opus_id}"

    parts = paragraphs_to_parts(_find_paragraphs_in_state(state))
    if parts:
        text = "\n".join(str(p.get("text") or "") for p in parts if p.get("type") == "text")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            parsed["text"] = text
        _fill_parts_images(parsed, parts)
        # 已经拿到正文了，缓存起来：后面 nodes_for / 卡片正文都用它，不再重复抓
        parsed["full_parts"] = parts
        parsed["full_source"] = "opus页面"

    if not (parts or parsed.get("title") or parsed.get("text") or parsed.get("uname")):
        return None
    return parsed


async def _from_read_page(parsed: Dict) -> Tuple[List[Dict], str]:
    cv_id = str(parsed.get("cv_id") or "")
    if not cv_id:
        return [], ""
    html = await api.get_read_page(cv_id)
    if not html:
        return [], ""
    state = _state_json_of(html)
    # 新版专栏页和 opus 页共用一套 state（detail.modules 列表），标题/作者也在这里，
    # 所以一并回填 —— 专栏接口被限流时，标题/作者/配图就靠这条兜住
    _fill_meta(parsed, _item_of_state(state))
    parts = paragraphs_to_parts(_find_paragraphs_in_state(state)) if state else []
    if not parts and state:
        parts = html_to_parts(_find_html_in_state(state))
    if not parts:
        # 专栏页正文容器是 read-article-holder（新版有时也走 opus 的容器）
        block = _container_html(html, ("read-article-holder",) + OPUS_BODY_CLASSES)
        if block:
            parts = html_to_parts(block)
    if not parts:
        parts = html_to_parts(_first_p_with_images(html))
    if parts:
        _fill_parts_images(parsed, parts)
    return parts, "专栏页面"


async def fetch(parsed: Dict, force: bool = False) -> Tuple[List[Dict], str]:
    """抓正文。返回 ``(有序片段, 来源说明)``；抓不到返回 ``([], "")``。

    ``force=True`` 时不看 ``needs_full`` 的门槛（调试命令用）。
    """
    if not force and not needs_full(parsed):
        return [], ""

    chain = (
        ("opus详情", _from_opus),
        ("动态详情", _from_detail),
        ("opus页面", _from_opus_page),
        ("专栏正文接口", _from_article_api),
        ("专栏页面", _from_read_page),
    )
    for name, handler in chain:
        try:
            parts, _ = await handler(parsed)
        except Exception as e:
            logger.warning(f"B站订阅：抓全文（{name}）出错：{e}")
            continue
        # force（手动补推 / 调试）时不看长短：抓到的就是抓到的，短也有用（正文要进卡片）
        if parts and (force or _total_len(parts) >= MIN_FULL_LEN // 2):
            parts = _cap_parts(parts, _limit())
            logger.info(
                f"B站订阅：动态 {parsed.get('id_str')} 全文抓到了（{name}，{_total_len(parts)} 字）"
            )
            return parts, name
    logger.info(f"B站订阅：动态 {parsed.get('id_str')} 没抓到全文，只发预览")
    return [], ""


async def fill(parsed: Dict, force: bool = True) -> List[Dict]:
    """抓正文、缓存进 ``parsed``，顺手回填标题/作者；返回有序片段（可能为空）。

    **手动补推必须先调它再画卡片**：卡片是拿 ``parsed`` 现画的，而 ``fetch`` 是在
    抓的过程中把标题/作者/正文回填进去的 —— 顺序反了就会画成
    "未知UP · 投稿了文章"那种空壳（只给链接补推时踩过）。
    """
    if force:
        parsed.pop("full_parts", None)
    parts = parsed.get("full_parts")
    if parts is None:
        parts, source = await fetch(parsed, force=force)
        if parts:
            parsed["full_parts"] = parts
            parsed["full_source"] = source
    return parts or []


# ---------------------------------------------------------------------------
# 合并转发节点
# ---------------------------------------------------------------------------
def _split_text(text: str, chunk: int = NODE_CHUNK) -> List[str]:
    """按行切块，单行过长就硬切（避免一个节点塞几千字）。"""
    chunks: List[str] = []
    for line in str(text or "").split("\n"):
        line = line.rstrip()
        if not line:
            chunks.append("")
            continue
        while len(line) > chunk:
            chunks.append(line[:chunk])
            line = line[chunk:]
        chunks.append(line)
    return chunks


def _chunk_size(parts: List[Dict]) -> int:
    """这条正文每个转发节点切多少字：短文 500，长文自动放大（把段数压进 MAX_NODES）。

    起因：6000 字的版本公告按 500 字切是 16 段，平台对转发条数有上限，
    于是"长文转发看起来失败、短文正常"。段数按"文本段 + 图片段 + 1 个标题段"估。
    """
    total = _total_len(parts)
    images = min(sum(1 for p in parts if p.get("type") == "image"), MAX_FORWARD_IMAGES)
    slots = max(1, MAX_NODES - images - 1)
    if total <= NODE_CHUNK * slots:
        return NODE_CHUNK
    return min(NODE_CHUNK_MAX, max(NODE_CHUNK, -(-total // slots)))


def build_nodes(title: str, parts: List[Dict], link: str = "") -> List[Message]:
    """有序片段 → 合并转发的节点列表（正文里的图插在原位）。

    每个节点多少字由 ``_chunk_size`` 按正文长度定：短文的 500 字一刀，
    长文自动放大到最多 ``NODE_CHUNK_MAX``，避免段数超过平台的转发上限。

    这里的图只来自**正文段落**（专栏正文里夹的图）。动态本身的配图不在这里发：
    卡片放首图、其余单独补发，动图才能保持会动的 ``.gif``。
    """
    items: List[Message] = []
    head = f"《{title}》" if title else "全文"
    items.append(MessageSegment.text(f"{head}\n{'—' * 10}"))

    chunk_size = _chunk_size(parts)
    buf = ""
    for part in parts:
        if part.get("type") == "image":
            if buf:
                items.append(MessageSegment.text(buf))
                buf = ""
            _append_image(items, part.get("url"))
            continue
        for chunk in _split_text(str(part.get("text") or ""), chunk_size):
            if buf and len(buf) + len(chunk) + 1 > chunk_size:
                items.append(MessageSegment.text(buf))
                buf = ""
            buf = f"{buf}\n{chunk}" if buf else chunk
    if buf:
        items.append(MessageSegment.text(buf))

    if link:
        items.append(MessageSegment.text(f"原文：{link}"))
    return items


def _append_image(items: List[Message], url: Any) -> None:
    target = str(url or "").strip()
    if not target:
        return
    try:
        items.append(MessageSegment.image(target))  # type: ignore[union-attr]
    except Exception as e:
        logger.warning(f"B站订阅：配图构造失败（{target}）：{e}")


async def nodes_for(parsed: Dict, force: bool = False) -> List[Message]:
    """给一条动态准备合并转发节点：长文的正文（没有长文就返回空）。

    带缓存：抓过的动态不重复抓。动态本身的配图不走这里（卡片放首图、其余单独发）。
    """
    if not cfg.get("push_full_text", True):
        return []

    parts = await fill(parsed, force=force)
    # 太短就不补"全文"这一条转发了（短内容卡片上本来就看得全）
    if not parts or (not force and _total_len(parts) < MIN_FULL_LEN // 2):
        return []
    # short_url 是推送层生成的 b23.tv 短链（推送时才有），没有就用原链接
    nodes = build_nodes(
        str(parsed.get("title") or ""),
        parts,
        str(parsed.get("short_url") or parsed.get("url") or ""),
    )
    if len(nodes) > MAX_NODES + 2:
        # 段数太多：平台对转发条数有上限（常见是只发前几条），提醒一下怎么绕
        logger.warning(
            f"B站订阅：正文 {_total_len(parts)} 字切成了 {len(nodes)} 段转发，"
            "平台可能截断；可以「bili修改配置 转发 关」改成按段并块发"
        )
    return nodes