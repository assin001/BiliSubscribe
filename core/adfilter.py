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

"""带货 / 广告动态的自动过滤（**只对非官方号生效**）。

为什么需要它：官方号（异环、卡拉彼丘这类）走 ``games.py`` 的游戏规则，已经足够；
普通 UP 现在是"照单全推"，而 B站上不少 UP 会在动态里挂商品、发带货链接，
这类内容群里通常不想要。

为什么规则定得这么**窄**：宁可漏掉几条广告，也不能误杀正常动态 ——
所以：

- 不用常见词（"推荐""收藏""链接""元""价格""限时"一律不进来，正常动态里满地都是）；
- 只认三类硬信号：B站自己的**商品类型字段**（结构字段，几乎不会误判）、
  明确的**电商域名**、以及**只有带货才会说**的词（到手价 / 领券 / 购物车…）。

返回"原因"而不是布尔值：日志里写清为什么没推，用户能一眼判断是不是误杀。
"""
from typing import Dict, Optional

# 只有带货/促销才会出现的词（故意避开"推荐、收藏、链接、价格、活动"这类常见词）
AD_WORDS = (
    "到手价",
    "优惠券",
    "领券",
    "满减",
    "下单",
    "抢购",
    "百亿补贴",
    "购物车",
    "加购",
    "橱窗",
    "带货",
    "小红书",
    "抖音小店",
    "拼多多",
)

# 明确的电商域名（出现在正文或链接卡片里才判）
SHOP_DOMAINS = (
    "item.taobao.com",
    "detail.tmall.com",
    "item.jd.com",
    "yangkeduo.com",
    "jinritemai.com",
    "mall.bilibili.com",
)


def looks_like_ad(parsed: Dict) -> Optional[str]:
    """这条动态像不像带货/广告；像就返回原因（写日志用），不像返回 ``None``。"""
    if not isinstance(parsed, dict):
        return None
    if parsed.get("goods"):
        return "商品动态（B站商品类型字段）"
    text = f"{parsed.get('title') or ''}\n{parsed.get('text') or ''}"
    if not text.strip():
        return None
    for word in AD_WORDS:
        if word in text:
            return f"含「{word}」"
    low = text.lower()
    for domain in SHOP_DOMAINS:
        if domain in low:
            return f"含商品链接 {domain}"
    return None
