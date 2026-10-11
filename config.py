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

"""插件配置：Web 控制台配置页 + 本地兜底。

优先注册到 GsCore 的插件配置体系（``gsuid_core.utils.plugins_config``）：
在插件里实例化一个 ``StringConfig``，它会被塞进核心的 ``all_config_list``，
Web 控制台的「插件 → BiliSubscribe → 配置」页就能直接读写这些项，改完立即生效。
核心是这样认出"这份配置属于哪个插件"的：``StringConfig`` 会通过调用栈找
``gsuid_core/plugins/<插件目录名>``，所以插件目录名必须是 ``BiliSubscribe``。

拿不到那套 API 时（GsCore 版本较老、离线单测）退化为本地扁平 JSON，
命令行 ``bili修改配置`` 依旧能用。

对外只用这四个入口：``get`` / ``set_value`` / ``load_config`` / ``parse_bool``。
"""
import json
from pathlib import Path
from typing import Any, Dict, Optional


def _data_dir() -> Path:
    """插件数据目录：优先用 GsCore 规范的 ``get_res_path``（<repo>/data/BiliSubscribe）。"""
    try:
        from gsuid_core.data_store import get_res_path

        return Path(get_res_path("BiliSubscribe"))
    except Exception:
        pass
    try:
        import gsuid_core

        return Path(gsuid_core.__file__).resolve().parents[1] / "data" / "BiliSubscribe"
    except Exception:
        return Path(__file__).parent / "data"


DATA_DIR = _data_dir()
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

# StringConfig 管的配置文件（Web 控制台配置页写的就是它）
CONFIG_PATH = DATA_DIR / "config.json"
# 兜底后端用的文件，和上面分开，避免两边互相覆盖
LOCAL_CONFIG_PATH = DATA_DIR / "local_config.json"
SUBS_PATH = DATA_DIR / "subscribe.json"
# 图片缓存（头像这类每次都一样的图，同一个 URL 只下载一次）
IMAGE_CACHE_DIR = DATA_DIR / "image_cache"


def valid_sleep_range(text: str) -> bool:
    """休眠时段格式校验：``HH:MM-HH:MM``（允许跨天，如 23:30-07:00）。"""
    try:
        start_raw, end_raw = str(text or "").split("-", 1)
        for part in (start_raw.strip(), end_raw.strip()):
            hh, mm = part.split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                return False
        return True
    except Exception:
        return False


def in_sleep_range(range_str: str) -> bool:
    """现在是否处于休眠时段（到点就不轮询：省风控、夜里不打扰）。

    跨天的写法也认；**解析不了就当作不休眠** —— 配置写错不该让轮询停摆。
    """
    import time

    text = str(range_str or "").strip()
    if not valid_sleep_range(text):
        _warn(f"B站订阅：休眠时段「{text}」解析不了，按不休眠处理")
        return False
    start_raw, end_raw = (p.strip() for p in text.split("-", 1))
    sh, sm = (int(x) for x in start_raw.split(":"))
    eh, em = (int(x) for x in end_raw.split(":"))
    now = time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return False
    return (start <= cur < end) if start < end else (cur >= start or cur < end)


def _warn(msg: str) -> None:
    try:
        from gsuid_core.logger import logger

        logger.warning(msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 默认值（扁平）：兜底后端与「合法键名集合」都以它为准
# ---------------------------------------------------------------------------
DEFAULT_CONFIG: Dict[str, Any] = {
    "bilibili_cookie": "",
    "check_interval": 300,
    "max_uid_per_round": 20,
    # 定时休眠：这个时段内不轮询（省风控、夜里不打扰群里）。格式 HH:MM-HH:MM
    "sleep_mode": True,
    "sleep_range": "01:00-07:30",
    # 头像这类重复图片的磁盘缓存有效期（天），到期自动清理
    "cache_ttl_days": 15,
    "notify_video": True,
    "notify_dynamic": True,
    "notify_live": False,
    "push_cover": True,
    # 推送画成卡片图（头部 + 标题 + 正文 + 首图），其余图单独补发
    "push_card": True,
    # 推送里的原文链接换成 b23.tv 短链（动态 / 投稿 / 专栏都支持）
    "push_short_link": True,
    "push_at_all": False,
    "check_video_list": True,
    # 带货/广告动态自动过滤（**只对非官方号**；官方号走白名单，不重复过滤）
    "ad_filter": True,
    "max_push_per_round": 5,
    # 长文动态（版本更新公告这类）：卡片之外再补发全文。
    # 默认**关**：卡片本身就是预览，正文太长会刷屏（要全文点链接）
    "push_full_text": False,
    # 全文用合并转发发：**默认关**（按条并块发更稳）。有些核心把 EnableForwardMessage
    # 设成数字，合并转发只会发出前 N 条，看起来就是"转发里没有全文"
    "full_text_node": False,
    # 动图压缩：默认关（原链接原样发）。开了之后 >1MB 的动图会先压小再发
    "gif_shrink": False,
    "full_text_limit": 5000,
    # 多图动态：预览放 3 张，其余图放进合并转发
    "push_extra_images": True,
    # 动态过滤（白名单）：哪些 UP 要走白名单由内置游戏规则决定（见 games.py），
    # 这里是管理员额外加的关键词：标题/正文命中就当命中白名单，直接推
    "auto_push_keywords": [],
    # 没命中白名单的动态要不要进待定队列（默认直接过滤掉，不推也不打扰人）
    "queue_unmatched": False,
    # 待定队列的通知发到哪些会话，格式 "group:群号" 或 "qq:direct:QQ号"
    # 键名沿用早期的 review_targets（改名会让老配置里的会话丢失）
    "review_targets": [],
}

_BOOL_TRUE = {"1", "true", "on", "yes", "开", "是", "启用", "开启"}
_BOOL_FALSE = {"0", "false", "off", "no", "关", "否", "禁用", "关闭"}


# ---------------------------------------------------------------------------
# Web 控制台配置页用的 GSC 结构（title / desc / options / secret 都在这里声明）
# ---------------------------------------------------------------------------
def _build_gsc_default() -> Optional[Dict[str, Any]]:
    """构造 GSC 配置项；拿不到核心的配置模型时返回 None（走本地兜底）。"""
    try:
        from gsuid_core.utils.plugins_config.models import (
            GsBoolConfig,
            GsDivider,
            GsIntConfig,
            GsListStrConfig,
            GsStrConfig,
        )
    except Exception:
        return None

    return {
        "divider_api": GsDivider("接口与请求", "Cookie 与轮询节奏"),
        "bilibili_cookie": GsStrConfig(
            "B站 Cookie",
            "登录后的 Cookie（形如 SESSDATA=xxx; bili_jct=yyy）。"
            "动态接口报 -636、投稿列表报 -352 风控时必须配置",
            "",
            secret=True,
        ),
        "check_interval": GsIntConfig(
            "轮询间隔(秒)",
            "多久检查一次 UP 的更新。太小容易被 B站风控，建议不低于 180",
            300,
            max_value=86400,
            options=[60, 180, 300, 600, 900, 1800],
        ),
        "max_uid_per_round": GsIntConfig(
            "单轮检查 UP 上限",
            "订阅很多时每轮只查其中一部分、轮着查，避免一轮请求过多",
            20,
            max_value=200,
        ),
        "divider_push": GsDivider("推送内容", "推什么、怎么推"),
        "notify_video": GsBoolConfig("推送视频", "UP 投稿了新视频时推送", True),
        "notify_dynamic": GsBoolConfig("推送动态", "图文、专栏、转发等动态是否推送", True),
        "notify_live": GsBoolConfig("推送开播卡片", "动态流里的直播开播卡片是否推送", False),
        "push_cover": GsBoolConfig("带封面图", "推送时是否附上封面 / 配图", True),
        "push_at_all": GsBoolConfig(
            "@全体成员",
            "推送时 @全体成员；也可以在订阅时用「bili订阅 <UID> at」单独对某个 UP 开",
            False,
        ),
        "push_card": GsBoolConfig(
            "推送用卡片图",
            "把每条更新画成一张卡片（B站粉头部 + 标题 + 正文 + 首图），比纯文字清楚；"
            "关掉就退回文字 + 图片",
            True,
        ),
        "push_short_link": GsBoolConfig(
            "推送用短链",
            "把推送里的原文链接换成 b23.tv 短链（动态 / 投稿 / 专栏都支持；"
            "生成失败会自动退回原链接）。直播链接这接口不给短链，原样保留",
            True,
        ),
        "divider_adv": GsDivider("高级", "一般不用改"),
        "sleep_mode": GsBoolConfig(
            "定时休眠",
            "打开后，休眠时段内不轮询：省风控、夜里不打扰群里。"
            "手动 bili检查更新 不受影响",
            True,
        ),
        "sleep_range": GsStrConfig(
            "休眠时段",
            "格式 HH:MM-HH:MM（如 01:00-07:30；写 23:30-07:00 这种跨天的也行）。"
            "填错就按不休眠处理，不会让轮询停摆",
            "01:00-07:30",
        ),
        "cache_ttl_days": GsIntConfig(
            "图片缓存天数",
            "头像这类每次都一样的图会存到本地（同一个 URL 只下一次），"
            "超过这个天数自动清理",
            15,
            max_value=365,
        ),
        "check_video_list": GsBoolConfig(
            "额外检查投稿列表",
            "除动态外再用「投稿列表」接口查视频，补上动态接口漏掉的投稿",
            True,
        ),
        "max_push_per_round": GsIntConfig(
            "单轮补推上限",
            "单个 UP 一轮最多补推几条动态，防止长时间离线后刷屏",
            5,
            max_value=20,
        ),
        "ad_filter": GsBoolConfig(
            "过滤带货/广告动态",
            "只对**非官方号**生效（官方号走白名单，不重复过滤）："
            "认 B站的商品类型字段、电商域名、以及\"到手价/领券/购物车\"这类只有带货才说的词。"
            "被过滤的会在日志里写明原因，觉得误杀了就关掉",
            True,
        ),
        "divider_full": GsDivider("长文与过滤", "长文补全文、官方号白名单过滤"),
        "push_full_text": GsBoolConfig(
            "长文补发全文",
            "默认关：卡片本身就是预览，正文太长会刷屏（要看全文点链接）。"
            "打开后长文会在卡片之外再补发全文",
            False,
        ),
        "full_text_limit": GsIntConfig(
            "全文上限字数",
            "全文最多放多少字，超出截断并提示看原文",
            5000,
            max_value=20000,
        ),
        "full_text_node": GsBoolConfig(
            "长文全文用合并转发",
            "默认关：全文按 ~1500 字并成几条普通消息发。"
            "打开才发成一条合并转发 —— 核心的「合并转发消息」配置设成数字时"
            "只会发出前 N 条，看起来就是「转发里没有全文」",
            False,
        ),
        "gif_shrink": GsBoolConfig(
            "动图先压小再发",
            "默认关：>1MB 的动图按原链接发。一条动态里好几张 7~8MB 的动图时建议打开"
            "（核心发图前会把远程图抓下来，几十兆的下载会慢到把适配器拖掉线）；"
            "压小是本地压一次、所有订阅群复用，合并转发里的动图同样会压",
            False,
        ),
        "push_extra_images": GsBoolConfig(
            "补发其余图片",
            "推送卡片只放第一张图，其余图片补发：≥2 张发成一条合并转发（单独一帧），"
            "只有 1 张就跟卡片同一条消息；图片按原链接发，不拼图不转码。"
            "其余图最多 9 张、其中动图最多 4 张、单张动图超过 10MB 的不发，"
            "并在转发里说明一句「动态动图触发大小上限规则，部分发送」。"
            "关掉就只发卡片",
            True,
        ),
        "divider_review": GsDivider("动态过滤（白名单）", "官方号只有命中白名单才推群，其余过滤"),
        "auto_push_keywords": GsListStrConfig(
            "额外白名单关键词",
            "除了内置游戏规则里各自的白名单栏目，再额外放行一批关键词"
            "（标题或正文命中就直接推，如：情报回顾）。留空不影响内置规则",
            [],
        ),
        "queue_unmatched": GsBoolConfig(
            "未命中的动态进待定队列",
            "默认关：没命中白名单的动态直接过滤，不推群也不通知。"
            "打开后它们会进待定队列并发通知，等你用「可以推送 <编号>」捞回来",
            False,
        ),
        "review_targets": GsListStrConfig(
            "待定通知接收会话",
            "开了待定队列后，通知发到哪里；在目标会话里发「bili待定 在这里」即可添加；"
            "格式 group:群号 或 qq:direct:QQ号",
            [],
        ),
    }


_GSC_DEFAULT = _build_gsc_default()

# 配置后端：gsuid_core（Web 控制台可改） / local（本地 JSON 兜底）
BACKEND = "local"
_config_obj: Any = None


def _read_legacy_flat() -> Dict[str, Any]:
    """读旧版扁平 config.json 里的值（本插件早期版本写过），用于一次性迁移。

    识别方式：GSC 结构里每个值都是带 ``type``/``title`` 的 dict，扁平结构是裸值。
    """
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    legacy: Dict[str, Any] = {}
    for key, value in raw.items():
        if key in DEFAULT_CONFIG and isinstance(value, (str, int, float, bool)):
            legacy[key] = value
    return legacy


def _init_backend() -> None:
    global _config_obj, BACKEND
    if _GSC_DEFAULT is None:
        return

    legacy = _read_legacy_flat()  # 必须在 StringConfig 接管文件之前读
    try:
        from gsuid_core.utils.plugins_config.gs_config import StringConfig

        obj = StringConfig("BiliSubscribe", CONFIG_PATH, dict(_GSC_DEFAULT))
    except Exception as e:
        _warn(f"BiliSubscribe：注册 GsCore 插件配置失败，改用本地配置（{e}）")
        return

    _config_obj = obj
    BACKEND = "gsuid_core"
    for key, value in legacy.items():
        try:
            obj.set_config(key, value)
        except Exception:
            pass


_init_backend()


def config_source() -> str:
    """给用户看的配置来源说明。"""
    if BACKEND == "gsuid_core":
        return "Web 控制台（插件 → BiliSubscribe → 配置）"
    return f"本地文件 {LOCAL_CONFIG_PATH}"


# ---------------------------------------------------------------------------
# 本地兜底
# ---------------------------------------------------------------------------
def _local_load() -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if LOCAL_CONFIG_PATH.exists():
        try:
            with open(LOCAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                data = raw
        except Exception:
            data = {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(
        {
            k: v
            for k, v in data.items()
            if k in DEFAULT_CONFIG and isinstance(v, (str, int, float, bool, list))
        }
    )
    return merged


def _local_save(conf: Dict[str, Any]) -> None:
    out = {k: conf.get(k, v) for k, v in DEFAULT_CONFIG.items()}
    tmp = LOCAL_CONFIG_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    tmp.replace(LOCAL_CONFIG_PATH)


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------
def get(key: str, default: Any = None) -> Any:
    """取单个配置项的值（每次实时读，控制台改完立刻生效）。"""
    if _config_obj is not None:
        try:
            item = _config_obj.get_config(key)
            data = getattr(item, "data", None)
            if data is not None:
                return data
        except Exception:
            pass
    return _local_load().get(key, DEFAULT_CONFIG.get(key, default))


def set_value(key: str, value: Any) -> bool:
    """改单个配置项，键不存在返回 False。"""
    if key not in DEFAULT_CONFIG:
        return False
    if _config_obj is not None:
        try:
            return bool(_config_obj.set_config(key, value))
        except Exception as e:
            _warn(f"BiliSubscribe：写入配置 {key} 失败：{e}")
            return False
    conf = _local_load()
    conf[key] = value
    _local_save(conf)
    return True


def load_config() -> Dict[str, Any]:
    """全部配置项的值（扁平，供展示用）。"""
    return {key: get(key) for key in DEFAULT_CONFIG}


def get_list(key: str) -> "list[str]":
    """取列表型配置，统一成 ``["a", "b"]`` 这种字符串列表。

    兼容两种手写形式：JSON 数组，或者 ``"a,b,c"`` 这种逗号串。
    """
    value = get(key)
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]
    return []


def parse_bool(text: str) -> Optional[bool]:
    """把用户输入解析成 bool；无法识别返回 None。"""
    s = str(text or "").strip().lower()
    if s in _BOOL_TRUE:
        return True
    if s in _BOOL_FALSE:
        return False
    return None
