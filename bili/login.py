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

"""B站扫码登录：生成二维码 → 轮询 → 校验 → 写入 Cookie 配置。

流程（与参考实现的接口一致）：

1. ``passport.bilibili.com/x/passport-login/web/qrcode/generate`` 拿 ``url`` + ``qrcode_key``；
2. 把 ``url`` 渲染成二维码发给用户，用哔哩哔哩手机客户端扫；
3. 轮询 ``.../qrcode/poll?qrcode_key=...``：

   - ``code == 0`` 成功，Cookie 在响应的 ``Set-Cookie`` 里（兜底解析 ``data.url`` 的 query）；
   - ``86038`` 二维码失效；``86090``（已扫码待确认）/ ``86101``（未扫码）继续等；

4. 拿到 Cookie 先用 nav 接口校验，有效才写进配置；
5. 轮询放在后台任务里跑完再回报，不占住核心的命令队列。
"""
import asyncio
import io
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event, Message
from gsuid_core.segment import MessageSegment

from ..core import config as cfg
from ..render.push import reply, send_to_target
from .api import _UA, api

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# 登录态 Cookie 就这几个（buvid 指纹由 bili/api.py 自己补，不用存）
COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")

QR_TTL = 180          # 二维码有效期（秒）
POLL_INTERVAL = 2.0   # 轮询间隔（秒）

_busy = False


def _headers() -> Dict[str, str]:
    """登录接口专用请求头：不带任何已有 Cookie，避免拿旧登录态去换二维码。"""
    return {
        "User-Agent": _UA,
        "Referer": "https://www.bilibili.com",
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
    }


# ---------------------------------------------------------------------------
# 二维码
# ---------------------------------------------------------------------------
async def generate() -> Optional[Dict[str, str]]:
    """申请一个登录二维码。失败返回 None。"""
    session = await api.session()
    try:
        async with session.get(GENERATE_URL, headers=_headers()) as resp:
            if resp.status != 200:
                logger.warning(f"B站扫码登录：生成二维码失败 HTTP {resp.status}")
                return None
            body = await resp.json(content_type=None)
    except Exception as e:
        logger.warning(f"B站扫码登录：生成二维码异常 {e}")
        return None

    if not isinstance(body, dict) or body.get("code") != 0:
        logger.warning(f"B站扫码登录：生成二维码失败 {body}")
        return None
    data = body.get("data") or {}
    url = str(data.get("url") or "").strip()
    key = str(data.get("qrcode_key") or "").strip()
    if not url or not key:
        return None
    return {"url": url, "qrcode_key": key}


def render_qrcode(url: str) -> Optional[bytes]:
    """把登录链接渲染成 PNG 字节；``qrcode`` 库缺失时返回 None（调用方退回发链接）。"""
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except Exception as e:
        logger.warning(f"B站扫码登录：qrcode 库不可用（{e}），将只发送链接")
        return None

    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_M,
            box_size=8,
            border=4,
        )
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"B站扫码登录：渲染二维码失败 {e}")
        return None


# ---------------------------------------------------------------------------
# 轮询
# ---------------------------------------------------------------------------
def _build_cookie(set_cookie_headers: List[str], fallback_url: str = "") -> str:
    """从 ``Set-Cookie`` 里挑出登录 Cookie；兜底解析 ``data.url`` 的 query。"""
    found: Dict[str, str] = {}
    for raw in set_cookie_headers or []:
        first = str(raw).split(";", 1)[0]
        name, sep, value = first.partition("=")
        if sep and name.strip():
            found.setdefault(name.strip(), value.strip())

    if not any(found.get(k) for k in COOKIE_KEYS) and fallback_url:
        # 手写拆分而不是 parse_qs：Cookie 值要保留原始 %2C 这类编码，不能 unquote
        for pair in urlparse(fallback_url).query.split("&"):
            name, sep, value = pair.partition("=")
            if sep and name.strip() and not found.get(name.strip()):
                found[name.strip()] = value

    return "; ".join(f"{k}={found[k]}" for k in COOKIE_KEYS if found.get(k))


async def poll_once(qrcode_key: str) -> Tuple[Optional[int], str, str]:
    """轮询一次，返回 ``(code, cookie, message)``；请求失败 code 为 None。"""
    session = await api.session()
    try:
        async with session.get(
            POLL_URL, params={"qrcode_key": qrcode_key}, headers=_headers()
        ) as resp:
            if resp.status != 200:
                return None, "", ""
            body = await resp.json(content_type=None)
            raw_headers = list(resp.headers.getall("Set-Cookie", []))
    except Exception as e:
        logger.warning(f"B站扫码登录：轮询异常 {e}")
        return None, "", ""

    if not isinstance(body, dict):
        return None, "", ""
    data = body.get("data") or {}
    try:
        code = int(data.get("code"))
    except (TypeError, ValueError):
        return None, "", str(body.get("message") or "")

    message = str(data.get("message") or body.get("message") or "")
    if code == 0:
        return code, _build_cookie(raw_headers, str(data.get("url") or "")), message
    return code, "", message


# ---------------------------------------------------------------------------
# 登录流程
# ---------------------------------------------------------------------------
async def _notify(target: Dict, text: str) -> None:
    await send_to_target(
        str(target.get("kind") or "group"),
        str(target.get("id") or ""),
        [MessageSegment.text(f"【B站订阅】{text}")],
        target.get("entry") or {},
    )


async def _save_and_notify(cookie: str, target: Dict) -> None:
    if not cookie:
        await _notify(target, "登录成功但响应里没有拿到有效 Cookie，请手动配置")
        return
    info = await api.verify_cookie(cookie)
    if not info:
        await _notify(target, "拿到 Cookie 但校验没通过（可能已失效），请重新扫码")
        return
    if not cfg.set_value("bilibili_cookie", cookie):
        await _notify(target, "写入配置失败，请检查核心配置目录的权限")
        return

    api.clear_rate_limit()  # 之前被风控静默的话，登录后立刻可以重试
    who = info.get("uname") or info.get("mid") or "未知账号"
    logger.info(f"B站扫码登录成功：{who}")
    await _notify(target, f"登录成功（{who}），Cookie 已写入配置，动态与投稿接口现在走登录态")


async def _poll_and_save(qrcode_key: str, target: Dict) -> None:
    global _busy
    deadline = time.monotonic() + QR_TTL
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            code, cookie, message = await poll_once(qrcode_key)
            if code is None:
                continue
            if code == 0:
                await _save_and_notify(cookie, target)
                return
            if code == 86038:
                await _notify(target, "二维码已失效，请重新发送 bili登录")
                return
            # 86090 已扫码待确认 / 86101 未扫码：继续等
            logger.debug(f"B站扫码登录：等待扫码 {code} {message}")
        await _notify(target, f"扫码登录超时（{QR_TTL // 60} 分钟），请重新发送 bili登录")
    except Exception as e:
        logger.error(f"B站扫码登录：流程异常 {e}")
        await _notify(target, f"扫码登录失败：{e}")
    finally:
        _busy = False


async def start_login(bot: Bot, ev: Event) -> None:
    """命令入口：发二维码 + 起后台轮询。"""
    global _busy
    if _busy:
        await reply(bot, ev, "已经有一轮扫码登录在进行中，等它结束（或超时）再试")
        return

    _busy = True
    try:
        payload = await generate()
        if not payload:
            _busy = False
            await reply(bot, ev, "生成登录二维码失败（可能被风控或网络异常），稍后再试")
            return

        png = render_qrcode(payload["url"])
        tip = (
            "请用哔哩哔哩手机客户端扫描下方二维码登录\n"
            f"有效期约 {QR_TTL // 60} 分钟，登录成功后 Cookie 会自动写入配置\n"
            "提示：扫码等同于用该账号登录，Cookie 就是账号凭证，建议用小号"
        )
        if png:
            msgs: List[Message] = [MessageSegment.text(tip), MessageSegment.image(png)]
        else:
            msgs = [
                MessageSegment.text(
                    f"{tip}\n\n（二维码渲染失败，缺 qrcode 库可执行 pip install qrcode）\n"
                    f"也可以在手机浏览器打开链接登录：\n{payload['url']}"
                )
            ]
        await reply(bot, ev, msgs)
        logger.info("B站扫码登录：二维码已发送，开始轮询")
    except Exception as e:
        _busy = False
        logger.error(f"B站扫码登录：发起失败 {e}")
        await reply(bot, ev, f"发起扫码登录失败：{e}")
        return

    target = {
        "kind": str(ev.user_type or "group"),
        "id": str(ev.group_id or ev.user_id or ""),
        "entry": {
            "bot_id": str(ev.bot_id or ""),
            "bot_self_id": str(ev.bot_self_id or ""),
        },
    }
    asyncio.create_task(_poll_and_save(payload["qrcode_key"], target))
