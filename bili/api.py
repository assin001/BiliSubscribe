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

"""B站数据接口（aiohttp 异步封装）。

只保留订阅推送需要的端点：

- 空间动态 ``x/polymer/web-dynamic/v1/feed/space``（需 WBI 签名 + buvid 指纹，
  部分时段还要求登录 Cookie，否则 -636）
- 空间投稿列表 ``x/space/wbi/arc/search``（需 WBI 签名）
- 用户名片 ``x/web-interface/card``（取 UP 名字，无需签名）
- 用户搜索 ``x/web-interface/search/type``（需要 buvid，可能被风控）

对外约定：返回 ``None`` 表示**请求失败**（调用方不要推进游标），
空列表表示请求成功但确实没有数据。
"""
import asyncio
import hashlib
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import aiohttp

from gsuid_core.logger import logger

from ..core import config as cfg
from .wbi import encode_wbi

# 图片磁盘缓存：头像这种每次都一样的图，同一个 URL 只下载一次
# （封面基本都是一次性的，缓存只是顺带；大图不缓存，免得占磁盘）
CACHE_MAX_BYTES = 1 * 1024 * 1024
_cache_checked = False

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_TTL_WBI = 12 * 3600
_TTL_BUVID = 24 * 3600
# 吃到 412 后静默一段时间，避免继续撞风控
_TTL_RATE_LIMIT = 300


class BiliApi:
    DYNAMIC_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    VIDEO_URL = "https://api.bilibili.com/x/space/wbi/arc/search"
    VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
    NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
    SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
    CARD_URL = "https://api.bilibili.com/x/web-interface/card"
    SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
    OPUS_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail"
    DETAIL_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"
    ARTICLE_URL = "https://api.bilibili.com/x/article/view"
    LIVE_ROOM_URL = "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld"
    SHARE_CLICK_URL = "https://api.bilibili.com/x/share/click"

    def __init__(self) -> None:
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        self._wbi_keys: Dict[str, Any] = {}
        self._buvid: Dict[str, Any] = {}
        self._rate_limit_until = 0.0

    # ---------------- 基础设施 ----------------
    async def session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=15),
                    headers={"User-Agent": _UA},
                )
            return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    @property
    def rate_limited(self) -> bool:
        return time.time() < self._rate_limit_until

    def _mark_rate_limited(self, seconds: int = _TTL_RATE_LIMIT) -> None:
        self._rate_limit_until = time.time() + seconds

    def clear_rate_limit(self) -> None:
        """手动清掉风控静默期（例如刚配好 Cookie，想立刻重试）。"""
        self._rate_limit_until = 0.0

    def _cookie(self) -> str:
        """拼 Cookie：优先用配置里的登录 Cookie，再补 buvid 访客指纹。"""
        parts: List[str] = []
        b3 = str(self._buvid.get("b3") or "")
        b4 = str(self._buvid.get("b4") or "")
        if b3:
            parts.append(f"buvid3={b3}")
            parts.append(f"buvid_fp={b3}")
            parts.append(f"b_nut={int(self._buvid.get('fetched_at') or time.time())}")
        if b4:
            parts.append(f"buvid4={b4}")
        user_cookie = str(cfg.get("bilibili_cookie") or "").strip()
        if user_cookie:
            parts.append(user_cookie)
        return "; ".join(parts)

    def _headers(self, referer: str = "https://www.bilibili.com", origin: str = "") -> Dict[str, str]:
        headers = {
            "User-Agent": _UA,
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
        }
        if origin:
            headers["Origin"] = origin
        cookie = self._cookie()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _get_json(
        self,
        url: str,
        params: Optional[Dict] = None,
        referer: str = "https://www.bilibili.com",
        origin: str = "",
    ) -> Optional[Dict]:
        """GET 一个 B站接口，返回响应体 dict；网络异常 / HTTP 非 200 返回 None。"""
        if self.rate_limited:
            return None
        session = await self.session()
        full = f"{url}?{urlencode(params)}" if params else url
        try:
            async with session.get(full, headers=self._headers(referer, origin)) as resp:
                if resp.status == 412:
                    self._mark_rate_limited()
                    logger.warning(f"B站接口被风控 HTTP 412：{url}")
                    return None
                if resp.status != 200:
                    logger.warning(f"B站接口 HTTP {resp.status}：{url}")
                    return None
                body = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"B站接口请求异常：{url} -> {e}")
            return None
        if not isinstance(body, dict):
            return None
        return body

    async def _post_form(
        self,
        url: str,
        data: Dict,
        referer: str = "https://www.bilibili.com",
    ) -> Optional[Dict]:
        """POST 一个表单到 B站接口（``x/share/click`` 这类要 POST 的用），返回响应体 dict。"""
        if self.rate_limited:
            return None
        session = await self.session()
        try:
            async with session.post(
                url, data=data, headers=self._headers(referer)
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"B站接口 HTTP {resp.status}：{url}")
                    return None
                body = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"B站接口请求异常：{url} -> {e}")
            return None
        if not isinstance(body, dict):
            return None
        return body

    async def fetch_bytes(self, url: str, referer: str = "https://www.bilibili.com") -> Optional[bytes]:
        """下载一张图（画推送卡片用）。失败返回 None，不抛异常。

        图片 CDN 不吃风控那套（不需要签名），但带上 Referer/UA 更稳。
        **小图（≤1MB，比如头像）会落磁盘缓存**：同一个 URL 只下载一次，
        缓存超过 ``cache_ttl_days`` 天会在下次用到时自动清掉。
        """
        target = str(url or "").strip()
        if not target:
            return None
        if target.startswith("//"):
            target = "https:" + target
        elif target.startswith("http://"):
            target = "https://" + target[len("http://"):]

        path = self._cache_file(target)
        if path is not None and path.is_file():
            try:
                cached = path.read_bytes()
                if cached:
                    return cached
            except Exception:
                pass
        self._clean_cache_once()

        session = await self.session()
        try:
            async with session.get(target, headers=self._headers(referer)) as resp:
                if resp.status != 200:
                    logger.warning(f"B站订阅：图片 HTTP {resp.status}（{target[:80]}）")
                    return None
                data = await resp.read()
        except Exception as e:
            logger.warning(f"B站订阅：图片下载失败（{target[:80]}）-> {e}")
            return None
        if data and len(data) <= CACHE_MAX_BYTES and path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            except Exception:
                pass
        return data or None

    @staticmethod
    def _cache_file(url: str):
        """这个 URL 对应的缓存文件（拿不到缓存目录就返回 None，等于不缓存）。"""
        try:
            from ..core.config import IMAGE_CACHE_DIR

            return IMAGE_CACHE_DIR / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:24] + ".img")
        except Exception:
            return None

    def _clean_cache_once(self) -> None:
        """进程内第一次用到缓存时，清一遍过期文件（默认 15 天）。"""
        global _cache_checked
        if _cache_checked:
            return
        _cache_checked = True
        try:
            from ..core.config import IMAGE_CACHE_DIR

            if not IMAGE_CACHE_DIR.is_dir():
                return
            try:
                ttl = max(1, int(cfg.get("cache_ttl_days") or 15)) * 86400
            except (TypeError, ValueError):
                ttl = 15 * 86400
            now = time.time()
            removed = 0
            for item in IMAGE_CACHE_DIR.iterdir():
                try:
                    if item.is_file() and now - item.stat().st_mtime > ttl:
                        item.unlink()
                        removed += 1
                except Exception:
                    continue
            if removed:
                logger.info(f"B站订阅：清掉了 {removed} 个过期图片缓存（超过 {ttl // 86400} 天）")
        except Exception as e:
            logger.warning(f"B站订阅：清理图片缓存出错（{e}）")

    # ---------------- buvid 访客指纹 ----------------
    async def ensure_buvid(self, force: bool = False) -> bool:
        """拿 buvid3/buvid4；不带指纹硬打动态接口很容易吃 -352。失败不致命。"""
        if (
            not force
            and self._buvid.get("b3")
            and time.time() - float(self._buvid.get("fetched_at") or 0) < _TTL_BUVID
        ):
            return True
        body = await self._get_json(self.SPI_URL, referer="https://www.bilibili.com")
        if not body or body.get("code") != 0:
            return False
        data = body.get("data") or {}
        self._buvid = {
            "b3": str(data.get("b_3") or ""),
            "b4": str(data.get("b_4") or ""),
            "fetched_at": time.time(),
        }
        return bool(self._buvid["b3"])

    # ---------------- WBI 签名素材 ----------------
    async def ensure_wbi_keys(self, force: bool = False) -> bool:
        """从 nav 接口拿 img_key / sub_key（近 12 小时缓存）。"""
        if (
            not force
            and self._wbi_keys.get("img_key")
            and time.time() - float(self._wbi_keys.get("fetched_at") or 0) < _TTL_WBI
        ):
            return True
        await self.ensure_buvid()
        body = await self._get_json(self.NAV_URL, referer="https://www.bilibili.com")
        if not body:
            return False
        wbi_img = (body.get("data") or {}).get("wbi_img") or {}
        img_url = str(wbi_img.get("img_url") or "")
        sub_url = str(wbi_img.get("sub_url") or "")
        if not img_url or not sub_url:
            logger.warning(f"WBI 密钥响应异常：code={body.get('code')}")
            return False
        # 链接的文件名去掉扩展名就是 key：.../7cd084941338484aae1ad9425b84077c.png
        self._wbi_keys = {
            "img_key": img_url.rsplit("/", 1)[-1].split(".")[0],
            "sub_key": sub_url.rsplit("/", 1)[-1].split(".")[0],
            "fetched_at": time.time(),
        }
        return True

    # ---------------- 长文正文（专栏 / opus 动态） ----------------
    async def _signed_get(self, url: str, base_params: Dict, referer: str, origin: str = "") -> Optional[Dict]:
        """带 WBI 签名的 GET（签名失效时刷一次密钥重试）。"""
        for attempt in range(2):
            if not await self.ensure_wbi_keys(force=attempt > 0):
                return None
            params = encode_wbi(
                base_params,
                str(self._wbi_keys["img_key"]),
                str(self._wbi_keys["sub_key"]),
            )
            body = await self._get_json(url, params, referer=referer, origin=origin)
            if body is None:
                return None
            if body.get("code") == 0:
                return body
            if body.get("code") in (-352, -403) and attempt == 0:
                continue
            logger.warning(f"B站接口 code={body.get('code')} {body.get('message')}：{url}")
            return None
        return None

    async def head_size(self, url: str) -> int:
        """问一下这张图多大（字节），拿不到返回 0。

        用来判断"这张动图太大，别抓了"：先 HEAD；CDN 不支持 HEAD（405/没给长度）
        就用 ``Range: bytes=0-0`` 从 ``Content-Range`` 里取总长度。都失败返回 0，
        调用方按"没超限"处理 —— 不能因为一次探测失败就把图吞了。
        """
        target = str(url or "").strip()
        if not target:
            return 0
        if target.startswith("//"):
            target = "https:" + target
        elif target.startswith("http://"):
            target = "https://" + target[len("http://"):]
        session = await self.session()
        headers = {"User-Agent": _UA, "Referer": "https://www.bilibili.com"}
        for attempt in ("head", "range"):
            size = 0
            try:
                if attempt == "head":
                    async with session.head(target, headers=headers) as resp:
                        size = int(resp.headers.get("Content-Length") or 0)
                else:
                    async with session.get(
                        target, headers=dict(headers, Range="bytes=0-0")
                    ) as resp:
                        size = int(resp.headers.get("Content-Length") or 0)
                        if not size:
                            cr = str(resp.headers.get("Content-Range") or "")
                            size = int(cr.rsplit("/", 1)[-1]) if "/" in cr else 0
            except Exception:
                size = 0
            if size:
                return size
        return 0

    async def get_opus_detail(self, opus_id: str) -> Optional[Dict]:
        """opus 详情（现代接口，正文是段落模型 ``modules.module_content.paragraphs``）。"""
        if not str(opus_id or "").strip():
            return None
        return await self._signed_get(
            self.OPUS_URL,
            {"id": str(opus_id), "timezone_offset": -480, "features": "itemOpusStyle"},
            referer=f"https://www.bilibili.com/opus/{opus_id}",
        )

    async def get_dynamic_detail(self, dyn_id: str) -> Optional[Dict]:
        """单条动态详情（有时比 feed 里多带正文段落）。"""
        if not str(dyn_id or "").strip():
            return None
        return await self._signed_get(
            self.DETAIL_URL,
            {"id": str(dyn_id), "timezone_offset": -480, "features": "itemOpusStyle"},
            referer="https://t.bilibili.com/",
        )

    async def get_detail_item(self, dyn_id: str) -> Optional[Dict]:
        """单条动态的 ``data.item``（手动补推一条动态用）。"""
        body = await self.get_dynamic_detail(dyn_id)
        if not body:
            return None
        data = body.get("data")
        item = (data or {}).get("item") if isinstance(data, dict) else None
        return item if isinstance(item, dict) else None

    async def get_article_content(self, cv_id: str) -> Optional[Dict]:
        """老专栏正文接口：``data.content`` 是 HTML。已被限流，属于兜底之一。"""
        if not str(cv_id or "").strip():
            return None
        return await self._get_json(
            self.ARTICLE_URL,
            {"id": str(cv_id)},
            referer="https://www.bilibili.com/",
            origin="https://www.bilibili.com",
        )

    async def _get_html(self, url: str, tag: str) -> Optional[str]:
        """取一个网页的 HTML（页面类兜底抓取用）。"""
        if self.rate_limited:
            return None
        session = await self.session()
        try:
            async with session.get(
                url,
                headers=self._headers("https://www.bilibili.com/", "https://www.bilibili.com"),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"{tag} HTTP {resp.status}：{url}")
                    return None
                return await resp.text()
        except Exception as e:
            logger.warning(f"拉{tag}异常：{url} -> {e}")
            return None

    async def get_read_page(self, cv_id: str) -> Optional[str]:
        """专栏页面 HTML（正文可能在 ``__INITIAL_STATE__`` 或正文容器里）。"""
        if not str(cv_id or "").strip():
            return None
        return await self._get_html(f"https://www.bilibili.com/read/cv{cv_id}/", "专栏页面")

    async def get_opus_page(self, opus_id: str) -> Optional[str]:
        """opus 动态页面 HTML（点开动态时就是 ``www.bilibili.com/opus/<id>``）。

        这个页面是服务端渲染的，正文直出，所以接口全被风控时还能从页面捞。
        """
        if not str(opus_id or "").strip():
            return None
        return await self._get_html(f"https://www.bilibili.com/opus/{opus_id}", "opus 页面")

    # ---------------- b23.tv 短链 ----------------
    @staticmethod
    def _search(pattern: str, text: str) -> str:
        """正则取第一个分组（没写分组就取整个匹配），取不到返回空串。"""
        m = re.search(pattern, str(text or ""))
        if not m:
            return ""
        return m.group(1) if m.lastindex else m.group(0)

    async def _share_click(
        self, oid: str, share_id: str, origin: str, referer: str
    ) -> str:
        """调一次 ``x/share/click`` 拿 b23.tv 短链；失败返回空串。

        线上实测（2026-10）这接口还活着，参数按 bilibili-API-collect 的对照表：
        ``share_channel=COPY`` + ``share_mode=4`` + 对应的 ``share_id`` / ``oid``。
        返回的 ``data.content`` 形如 ``【标题】 https://b23.tv/xxxx``，抠出链接那一段。
        """
        buvid = (
            str(self._buvid.get("b3") or "")
            or str(self._buvid.get("b4") or "")
            or "buvid_bililink"
        )
        body = await self._post_form(
            self.SHARE_CLICK_URL,
            {
                "buvid": buvid,
                "build": 7710300,
                "platform": "android",
                "share_channel": "COPY",
                "share_mode": 4,
                "share_id": share_id,
                "oid": str(oid),
                "share_origin": origin,
            },
            referer=referer,
        )
        if not body or body.get("code") != 0:
            return ""
        data = body.get("data")
        content = str((data or {}).get("content") or "") if isinstance(data, dict) else ""
        return self._search(r"https?://b23\.tv/[\w\-]+", content)

    async def get_short_link(
        self,
        url: str = "",
        bvid: str = "",
        aid: str = "",
        cv_id: str = "",
        opus_id: str = "",
    ) -> str:
        """长链接 → b23.tv 短链（推送里发给人看的那个链接）。

        三条路，任一成功即返回，都不行返回空串（调用方保留原链接）：

        - **视频**：直接拼 ``https://b23.tv/<BV号>`` —— B站认这个写法，不用调接口；
        - **动态 / opus**：``share_id=main.dynamic.0.0``、``oid=动态id``；
        - **专栏**：``share_id=main.article.0.0``、``oid=cv号``。

        直播间链接这接口不给短链（返回里没有 ``content``），所以调用方对直播原样保留。
        ``bvid`` / ``cv_id`` / ``opus_id`` 这些线索有就直接用（``parsed`` 里现成的），
        没有才从 ``url`` 里抠。
        """
        target = str(url or "").strip()
        if "b23.tv/" in target:
            return target if target.lower().startswith("http") else "https://" + target

        bv = str(bvid or "").strip() or self._search(r"(BV[0-9A-Za-z]{10})", target)
        if bv:
            return f"https://b23.tv/{bv}"
        av = str(aid or "").strip() or self._search(r"(?:/video/av|^av)(\d+)", target)
        if av:
            return f"https://b23.tv/av{av}"

        dyn = (
            str(opus_id or "").strip()
            or self._search(r"/opus/(\d+)", target)
            or self._search(r"(?:t\.bilibili\.com|dynamic)/(\d{10,})", target)
        )
        if dyn:
            return await self._share_click(
                dyn, "main.dynamic.0.0", "dynamic", f"https://www.bilibili.com/opus/{dyn}"
            )

        cv = str(cv_id or "").strip() or self._search(r"(?:read/cv|/cv)(\d+)", target)
        if cv:
            return await self._share_click(
                cv, "main.article.0.0", "article", f"https://www.bilibili.com/read/cv{cv}/"
            )
        return ""

    # ---------------- Cookie 校验 ----------------
    async def verify_cookie(self, cookie: str) -> Optional[Dict]:
        """用**指定的** Cookie 打一次 nav：有效返回 ``{uname, mid}``，无效返回 None。

        扫码登录拿到 Cookie 后先过这一关再写配置，避免把无效 Cookie 存进去。
        """
        ck = str(cookie or "").strip()
        if not ck:
            return None
        session = await self.session()
        headers = {
            "User-Agent": _UA,
            "Referer": "https://www.bilibili.com",
            "Cookie": ck,
        }
        try:
            async with session.get(self.NAV_URL, headers=headers) as resp:
                if resp.status != 200:
                    return None
                body = await resp.json(content_type=None)
        except Exception as e:
            logger.warning(f"校验 Cookie 失败：{e}")
            return None
        if not isinstance(body, dict) or body.get("code") != 0:
            return None
        data = body.get("data") or {}
        if not data.get("isLogin"):
            return None
        return {"uname": str(data.get("uname") or ""), "mid": str(data.get("mid") or "")}

    # ---------------- 用户名片 / 搜索 ----------------
    async def get_live_room(self, uid: str) -> Optional[Dict]:
        """UID → 直播间信息（房间号 / 开播状态 / 标题 / 封面）。

        走 ``room/v1/Room/getRoomInfoOld``：**不需要 WBI、也不用 Cookie**（实测两个
        UID 都通）；而带 ``live_room`` 的 ``x/space/wbi/acc/info`` 动不动就 ``-352``
        风控，只能当备胎。``live_status``：0 未开播 / 1 直播中 / 2 轮播。

        没开过直播的 UP 会拿到 ``room_id = ""``，调用方据此跳过即可。
        """
        uid = str(uid or "").strip()
        if not uid:
            return None
        body = await self._get_json(
            self.LIVE_ROOM_URL,
            {"mid": uid},
            referer="https://live.bilibili.com/",
        )
        if not body or body.get("code") != 0:
            if body:
                logger.debug(
                    f"B站订阅：直播间接口 code={body.get('code')}（UID {uid}）"
                )
            return None
        data = body.get("data")
        if not isinstance(data, dict):
            return None
        status = data.get("liveStatus", data.get("live_status"))
        try:
            live_status = None if status is None else int(status)
        except (TypeError, ValueError):
            live_status = None
        return {
            "room_id": str(data.get("roomid") or data.get("room_id") or ""),
            "live_status": live_status,
            "title": str(data.get("title") or ""),
            "cover": str(data.get("cover") or ""),
            "online": data.get("online"),
        }

    async def get_user_card(self, uid: str) -> Optional[Dict]:
        """取 UP 名片信息（名字、头像）。失败返回 None。"""
        body = await self._get_json(
            self.CARD_URL,
            {"mid": str(uid), "photo": "false"},
            referer=f"https://space.bilibili.com/{uid}",
            origin="https://space.bilibili.com",
        )
        if not body or body.get("code") != 0:
            return None
        card = (body.get("data") or {}).get("card") or {}
        if not card:
            return None
        return {
            "uid": str(uid),
            "name": str(card.get("name") or ""),
            "face": str(card.get("face") or ""),
            "sign": str(card.get("sign") or ""),
        }

    async def search_user(self, keyword: str, limit: int = 5) -> Optional[List[Dict]]:
        """按关键词搜 UP，返回 ``[{uid, name, fans}]``；失败（含风控）返回 None。"""
        await self.ensure_buvid()
        body = await self._get_json(
            self.SEARCH_URL,
            {
                "search_type": "bili_user",
                "keyword": str(keyword),
                "page": 1,
                "page_size": max(1, min(20, limit)),
            },
            referer="https://search.bilibili.com/",
        )
        if not body:
            return None
        if body.get("code") != 0:
            logger.warning(f"搜索UP失败：code={body.get('code')} {body.get('message')}")
            return None
        result = (body.get("data") or {}).get("result") or []
        out: List[Dict] = []
        for item in result[:limit]:
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "uid": str(item.get("mid") or ""),
                    "name": str(item.get("uname") or "").replace("<em class=\"keyword\">", "").replace("</em>", ""),
                    "fans": int(item.get("fans") or 0),
                }
            )
        return [u for u in out if u["uid"]]

    # ---------------- 空间动态 ----------------
    async def get_dynamic(self, uid: str, page_size: int = 12) -> Optional[List[Dict]]:
        """拉某个 UID 的空间动态（最新在前）。失败返回 None。"""
        if not await self.ensure_wbi_keys():
            return None
        base_params = {
            "host_mid": str(uid),
            "timezone_offset": -480,
            "features": "itemOpusStyle",
            "offset": "",
        }
        headers_referer = f"https://space.bilibili.com/{uid}"
        # 签名被判风控（-352 / -403）时强制刷新密钥再试一次
        for attempt in range(2):
            if not await self.ensure_wbi_keys(force=attempt > 0):
                return None
            params = encode_wbi(
                base_params,
                str(self._wbi_keys["img_key"]),
                str(self._wbi_keys["sub_key"]),
            )
            params["offset"] = ""
            body = await self._get_json(
                self.DYNAMIC_URL,
                params,
                referer=headers_referer,
                origin="https://space.bilibili.com",
            )
            if body is None:
                return None
            code = body.get("code")
            if code == 0:
                items = (body.get("data") or {}).get("items") or []
                return [it for it in items[:page_size] if isinstance(it, dict)]
            if code in (-352, -403) and attempt == 0:
                logger.warning(f"动态接口 code={code}，刷新 WBI 密钥后重试（UID {uid}）")
                continue
            if code == -636:
                logger.warning(
                    f"动态接口需要登录 Cookie（code=-636，UID {uid}），"
                    "请用 bili设置ck 配置 Cookie"
                )
                return None
            logger.warning(f"动态接口错误 code={code}（UID {uid}）：{body.get('message')}")
            return None
        return None

    # ---------------- 空间投稿列表 ----------------
    async def get_video_info(self, bvid: str = "", aid: str = "") -> Optional[Dict]:
        """单个投稿的视频信息（手动补推视频用，`bili推这条 BV…`）。

        走 ``x/web-interface/view``：**不需要 WBI 签名**，也基本不吃 Cookie 风控，
        比空间投稿列表稳。返回结构对齐投稿列表那条路（``format_video`` /
        ``build_video_msgs`` 直接能用），另附作者 UID（推订阅群要按它找群）。
        """
        params: Dict[str, Any] = {}
        if str(bvid or "").strip():
            params["bvid"] = str(bvid).strip()
        elif str(aid or "").strip():
            params["aid"] = str(aid).strip()
        else:
            return None
        body = await self._get_json(
            self.VIEW_URL,
            params,
            referer="https://www.bilibili.com/",
            origin="https://www.bilibili.com",
        )
        if not body or body.get("code") != 0:
            if body:
                logger.warning(
                    f"B站订阅：视频详情接口 code={body.get('code')}"
                    f"（{body.get('message')}）：{params}"
                )
            return None
        data = body.get("data") or {}
        owner = data.get("owner") or {}
        try:
            duration = int(data.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        return {
            "bvid": str(data.get("bvid") or bvid or ""),
            "aid": str(data.get("aid") or aid or ""),
            "title": str(data.get("title") or ""),
            "pic": str(data.get("pic") or ""),
            "description": str(data.get("desc") or ""),
            "created": int(data.get("pubdate") or 0),
            "length": f"{duration // 60:02d}:{duration % 60:02d}" if duration else "",
            "author": str(owner.get("name") or ""),
            "mid": str(owner.get("mid") or ""),
            "face": str(owner.get("face") or ""),  # 作者头像（卡片头部用）
        }

    async def get_latest_videos(self, uid: str, page_size: int = 5) -> Optional[List[Dict]]:
        """拉某个 UID 的最新投稿（按发布时间倒序）。失败返回 None。

        部分账号这个接口会被风控（-352），此时需要配置 Cookie；
        拿不到也不影响动态推送。
        """
        if not await self.ensure_wbi_keys():
            return None
        base_params = {
            "mid": str(uid),
            "ps": max(1, min(30, page_size)),
            "pn": 1,
            "order": "pubdate",
            "platform": "web",
            "web_location": 1550101,
            "index": 0,
            "order_avoided": "true",
            "keyword": "",
            "tid": 0,
        }
        for attempt in range(2):
            if not await self.ensure_wbi_keys(force=attempt > 0):
                return None
            params = encode_wbi(
                base_params,
                str(self._wbi_keys["img_key"]),
                str(self._wbi_keys["sub_key"]),
            )
            body = await self._get_json(
                self.VIDEO_URL,
                params,
                referer=f"https://space.bilibili.com/{uid}/video",
                origin="https://space.bilibili.com",
            )
            if body is None:
                return None
            code = body.get("code")
            if code == 0:
                vlist = ((body.get("data") or {}).get("list") or {}).get("vlist") or []
                return [v for v in vlist if isinstance(v, dict)]
            if code in (-352, -403) and attempt == 0:
                await asyncio.sleep(1)
                continue
            logger.warning(f"投稿列表接口错误 code={code}（UID {uid}）：{body.get('message')}")
            return None
        return None


api = BiliApi()
