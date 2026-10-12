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

"""游戏白名单规则（**外挂 JSON**）：一个游戏一个文件，删掉文件即停用。

背景：游戏官方号的动态里混着大量推广、联动、抽奖，绝大多数是群里不想看的。
但**不同官号的发帖习惯差很多**，一刀切不行，所以每套规则自己选口径（``mode``）：

- ``whitelist``（白名单，默认）：只有命中的才推，其余过滤。
  适合标题就写清栏目的（异环：``活动日历`` ``角色档案`` ``更新公告``…）。

     命中 pass_keywords → 直接推群；其余 → 过滤/待定

- ``blacklist``（黑名单）：默认全推，只挡掉命中的。
  适合标题看不出类型、正文措辞又不固定的（卡拉彼丘：日常的安全巡查公告之类）。

     命中 block_keywords → 直接丢弃；其余 → 直接推群

两种模式共有：

- ``pass_keywords`` 命中 → **直接推群**（黑名单模式下也能用，用来捞回被误挡的）；
- ``block_keywords`` 命中 → **直接丢弃**（不推群、不通知、连待定队列都不进），**优先于** pass；
- 投稿视频不受影响，照常自动推送（视频本来就是官方号的主内容）；
- 白名单模式下"其余"要不要留一份待定，由配置 ``queue_unmatched`` 决定
  （默认直接过滤，开了就进待定队列、等你手动 ``可以推送``）。

规则**不写死在代码里**，就放在插件目录的 ``game_rules/`` 下（随插件分发，
和 ``fonts/`` / ``assets/`` 并列），**一个游戏一个 JSON**：

- **新增游戏**：往里加一个 ``<游戏名>.json``（照抄 ``异环.json`` 或 ``卡拉彼丘.json``）；
- **改规则内容**：直接改 JSON，**或者**去控制台「数据库管理 → 游戏过滤规则」里改
  （那边改完会自动写回 JSON，见 ``core/game_rules_db.py``）；
- **停用某个游戏**：把规则名从配置项「启用的游戏规则」里删掉
  （也可以 ``bili游戏规则 停用 <规则名>``）；
- **游戏没了**：**直接删掉那个 JSON** —— 代码不会再生成它，删了就是删了。

文件格式（**文件名就是规则名**，启停 / 展示都用它）::

    {
      "name": "异环",                        // 展示名，缺省用文件名
      "enabled": true,                       // false = 不启用（该官方号不再走本规则）
      "mode": "whitelist",                   // whitelist（默认）/ blacklist
      "uids": {"3546636978489848": "异环"},   // 官方号 {UID: 号的名字}，可以多个
      "pass_keywords": ["活动日历"],          // 标题/正文命中 → 直接推群
      "block_keywords": ["安全巡查公告"],      // 标题/正文命中 → 直接丢弃
      "note": "……"                           // 展示用说明，可省
    }

改完**不用重启**：每次取规则时按目录的「文件名 + 修改时间」判断要不要重读
（5 秒节流，文件本来就没几个，这点开销可以忽略）。
"""
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gsuid_core.logger import logger

from ..paths import GAME_RULES_DIR
from . import config as cfg

# 一条动态的三种处理方式
PUSH = "push"        # 命中/兜底：直接推群
PENDING = "pending"  # 没命中：过滤；配置里把「未命中的动态进待定队列」打开则进队列
FILTER = "filter"    # 明确丢弃：连待定都不进（黑名单命中、纯广告/抽奖这类）

# 每套规则的口径
MODE_WHITELIST = "whitelist"  # 白名单：命中才推，其余过滤（默认）
MODE_BLACKLIST = "blacklist"  # 黑名单：默认全推，命中屏蔽词才丢

_MODE_ALIASES = {
    "whitelist": MODE_WHITELIST,
    "white": MODE_WHITELIST,
    "白名单": MODE_WHITELIST,
    "blacklist": MODE_BLACKLIST,
    "black": MODE_BLACKLIST,
    "黑名单": MODE_BLACKLIST,
}

_JSON_GLOB = "*.json"
_SIG_TTL = 5.0  # 目录指纹的最长缓存秒数：同一轮轮询里不反复 stat 目录

# 这几类提醒都只打一次（5 秒一次的读取循环里别刷屏）
_missing_dir_warned = False
_disabled_noted = False


class GamePreset:
    """一个游戏的官方号 + 过滤口径（从 JSON 读出来）。"""

    def __init__(
        self,
        name: str,
        uids: Dict[str, str],
        mode: str = MODE_WHITELIST,
        pass_keywords: "Tuple[str, ...]" = (),
        block_keywords: "Tuple[str, ...]" = (),
        enabled: bool = True,
        note: str = "",
        source: str = "",
    ):
        self.name = str(name)
        # {UID: 号的名字}，一个游戏可以有多个官方号
        self.uids = {
            str(k).strip(): (str(v).strip() or str(name))
            for k, v in (uids or {}).items()
        }
        self.mode = mode if mode in (MODE_WHITELIST, MODE_BLACKLIST) else MODE_WHITELIST
        self.pass_keywords = tuple(str(k) for k in pass_keywords if str(k).strip())
        self.block_keywords = tuple(str(k) for k in block_keywords if str(k).strip())
        self.enabled = bool(enabled)
        self.note = str(note or "")
        self.source = str(source or "")  # JSON 文件名（= 规则名）

    @property
    def is_blacklist(self) -> bool:
        """黑名单口径：默认全推，只挡命中的。"""
        return self.mode == MODE_BLACKLIST

    @property
    def mode_text(self) -> str:
        return "黑名单" if self.is_blacklist else "白名单"

    def uid_of(self) -> List[str]:
        return list(self.uids)

    @staticmethod
    def _blob(parsed: Dict) -> str:
        """匹配范围是**标题 + 正文**（有的栏目写在标题里，有的写在正文里）。"""
        return f"{parsed.get('title') or ''}\n{parsed.get('text') or ''}"

    def match_keyword(self, parsed: Dict, extra_keywords: "Tuple[str, ...]" = ()) -> Optional[str]:
        """命中了哪个「放行」关键词？没命中返回 None。"""
        blob = self._blob(parsed)
        for kw in list(self.pass_keywords) + list(extra_keywords):
            if kw and kw in blob:
                return str(kw)
        return None

    def match_block(self, parsed: Dict) -> Optional[str]:
        """命中了哪个「屏蔽」关键词？没命中返回 None。"""
        blob = self._blob(parsed)
        for kw in self.block_keywords:
            if kw and kw in blob:
                return str(kw)
        return None

    def decide(self, parsed: Dict, extra_keywords: "Tuple[str, ...]" = ()) -> str:
        """这条动态怎么处理：``PUSH`` / ``PENDING`` / ``FILTER``。

        顺序：**屏蔽词**（显式丢弃的意图最强）→ **放行词** → 兜底按口径走
        （白名单 = 过滤/待定，黑名单 = 直接推）。
        ``extra_keywords`` 是管理员额外加的（``bili白名单``），对所有游戏生效。
        """
        if self.match_block(parsed):
            return FILTER
        if self.match_keyword(parsed, extra_keywords):
            return PUSH
        return PUSH if self.is_blacklist else PENDING


# ---------------------------------------------------------------------------
# 加载 / 缓存
# ---------------------------------------------------------------------------
_CACHE: Dict[str, Any] = {
    "sig": None,
    "enabled": None,
    "presets": {},
    "checked_at": 0.0,
}


def rules_dir() -> Path:
    """规则目录（插件目录下的 ``game_rules/``）。"""
    return GAME_RULES_DIR


def _signature(d: Path) -> Any:
    """目录指纹：文件名 + 修改时间 + 大小；目录读不了返回 None（每次都重读）。"""
    items: List[Tuple[str, int, int]] = []
    try:
        for path in sorted(d.glob(_JSON_GLOB)):
            try:
                st = path.stat()
            except OSError:
                continue
            items.append((path.name, st.st_mtime_ns, st.st_size))
    except OSError:
        return None
    return tuple(items)


def _warn_missing_dir(d: Path) -> None:
    """规则目录不见了（被整个删掉）时提醒一次。"""
    global _missing_dir_warned
    if _missing_dir_warned:
        return
    _missing_dir_warned = True
    logger.warning(f"B站订阅：游戏规则目录不存在：{d}（插件的 game_rules/ 被删了？）")


def _note_disabled(d: Path, names: List[str], total: int) -> None:
    """有规则文件却没启用时提一句（一个进程只说一次）。

    新增 JSON 之后最容易踩的坑：文件在了、但「启用的游戏规则」列表里没有它，
    于是"加了文件却什么都没发生"。全部停用算严重（warning），部分停用算提醒（info）。
    """
    global _disabled_noted
    if _disabled_noted or not names:
        return
    _disabled_noted = True
    tip = "要启用：控制台「启用的游戏规则」里加上，或 bili游戏规则 启用 <规则名>"
    if len(names) >= total:
        logger.warning(
            f"B站订阅：{d} 里有 {total} 条规则，但一条都没启用 —— 官方号会当普通 UP 照单全推。{tip}"
        )
    else:
        logger.info(f"B站订阅：这些规则文件存在但没启用：{'、'.join(names)}（{tip}）")


def _as_str_list(raw: Any) -> List[str]:
    """接受 JSON 数组，也接受 ``"a,b,c"`` 这种逗号串。"""
    if isinstance(raw, str):
        return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return []


def _as_bool(raw: Any, default: bool = True) -> bool:
    """JSON 里写 ``true`` / ``"开"`` / ``1`` 都认。"""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("1", "true", "on", "yes", "开", "是", "启用", "开启"):
            return True
        if s in ("0", "false", "off", "no", "关", "否", "禁用", "关闭"):
            return False
    return default


def _as_mode(raw: Any, default: str = MODE_WHITELIST) -> str:
    """口径：``whitelist`` / ``blacklist``（写中文「白名单」「黑名单」也认）。"""
    s = str(raw or "").strip().lower()
    return _MODE_ALIASES.get(s, default)


def _read_one(path: Path, enabled_names: "Tuple[str, ...]") -> Optional[GamePreset]:
    """读一个规则文件；格式不对就记日志跳过（不影响其它规则）。

    **启停只看配置项「启用的游戏规则」**：规则名在列表里才生效。文件里那个
    ``enabled`` 字段只在"第一次生成配置默认值"时用过（见 config.py），运行时不再看。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"B站订阅：游戏规则 {path.name} 读不了（{e}），已跳过")
        return None
    if not isinstance(raw, dict):
        logger.warning(f"B站订阅：游戏规则 {path.name} 不是 JSON 对象，已跳过")
        return None

    stem = path.stem
    name = str(raw.get("name") or stem).strip() or stem
    uids = raw.get("uids")
    if not isinstance(uids, dict) or not uids:
        logger.warning(f"B站订阅：游戏规则 {path.name} 没写 uids（官方号），已跳过")
        return None

    return GamePreset(
        name=name,
        uids=uids,
        mode=_as_mode(raw.get("mode")),
        pass_keywords=_as_str_list(raw.get("pass_keywords")),
        # block_keywords 是正式名；早期版本叫 filter_keywords，兼容一下
        block_keywords=_as_str_list(
            raw.get("block_keywords", raw.get("filter_keywords"))
        ),
        enabled=stem in enabled_names,
        note=str(raw.get("note") or ""),
        source=path.name,
    )


def load(force: bool = False) -> Dict[str, GamePreset]:
    """读 ``game_rules/`` 里的全部规则，键是规则名（= JSON 文件名）。

    带缓存：目录指纹 + 「停用的规则」配置都没变就直接返回上次的结果；
    ``force`` 或超过 ``_SIG_TTL`` 秒才重新 stat 目录。
    """
    now = time.time()
    if not force and now - float(_CACHE["checked_at"] or 0.0) < _SIG_TTL:
        return _CACHE["presets"]

    d = rules_dir()
    sig = _signature(d)
    enabled_names = tuple(str(x).strip() for x in cfg.get_list("game_rules_enabled") if str(x).strip())
    if not force and _CACHE["sig"] == sig and _CACHE["enabled"] == enabled_names:
        _CACHE["checked_at"] = now
        return _CACHE["presets"]

    if not d.is_dir():
        _warn_missing_dir(d)

    presets: Dict[str, GamePreset] = {}
    for path in sorted(d.glob(_JSON_GLOB)):
        preset = _read_one(path, enabled_names)
        if preset is not None:
            presets[path.stem] = preset
    off = [p.name for p in presets.values() if not p.enabled]
    if off:
        _note_disabled(d, off, len(presets))
    _CACHE.update(sig=sig, enabled=enabled_names, presets=presets, checked_at=now)
    return presets


def reload() -> Dict[str, GamePreset]:
    """强制重读一遍（``bili游戏规则 重载`` / 改了文件想立刻生效时用）。"""
    return load(force=True)


# ---------------------------------------------------------------------------
# 对外查询
# ---------------------------------------------------------------------------
def all_presets() -> Dict[str, GamePreset]:
    """全部规则（含停用的），键是规则名。"""
    return load()


def active_presets() -> Dict[str, GamePreset]:
    """当前**启用中**的规则。"""
    return {key: p for key, p in load().items() if p.enabled}


def find_by_uid(uid: str) -> Optional[GamePreset]:
    """这个 UID 是哪个**启用中**游戏的官方号？不是则返回 None（不受白名单过滤）。"""
    target = str(uid or "").strip()
    if not target:
        return None
    for preset in active_presets().values():
        if target in preset.uids:
            return preset
    return None


def find_by_name(text: str) -> Optional[GamePreset]:
    """按规则名 / 游戏名 / 官方号名字找（含停用的，给启停命令用）。"""
    target = str(text or "").strip()
    if not target:
        return None
    presets = load()
    if target in presets:
        return presets[target]
    for preset in presets.values():
        if target == preset.name or target in preset.uids.values():
            return preset
    return None


def filter_notice(uid: str) -> str:
    """订阅时的提示：这个 UP 的动态要走游戏规则过滤（口径按该规则自己的）。"""
    preset = find_by_uid(uid)
    if preset is None:
        return ""
    if preset.is_blacklist:
        return (
            f"\n注意：这是《{preset.name}》官方号，动态按**黑名单**过滤——"
            "默认照常推，只有命中屏蔽词（日常公告这类）才不推；投稿视频照常自动推送"
        )
    return (
        f"\n注意：这是《{preset.name}》官方号，动态按**白名单**过滤——"
        "命中固定栏目才推群，其它（推广、联动、抽奖等）过滤掉；投稿视频照常自动推送"
    )


def status_line() -> str:
    """一行状态（``bili配置`` / 控制台摘要用）：哪些启用、哪些停用。"""
    presets = load()
    if not presets:
        return "（无规则文件）"
    return "、".join(
        p.name if p.enabled else f"{p.name}（停用）" for p in presets.values()
    )


def summary_lines() -> List[str]:
    """``bili过滤规则`` 展示用：有哪些游戏规则、白名单里有什么。"""
    presets = load()
    if not presets:
        return [f"（{rules_dir()} 里还没有任何规则文件）"]
    lines: List[str] = []
    for key, preset in presets.items():
        state = "启用" if preset.enabled else "停用"
        lines.append(
            f"《{preset.name}》［{state}·{preset.mode_text}］  "
            f"{preset.source or key + '.json'}"
        )
        uids = "、".join(f"{name}（{uid}）" for uid, name in preset.uids.items())
        lines.append(f"  官方号：{uids}")
        lines.append(f"  放行栏目：{'、'.join(preset.pass_keywords) or '（无）'}")
        if preset.block_keywords:
            lines.append(f"  屏蔽关键词：{'、'.join(preset.block_keywords)}")
        if preset.note:
            lines.append(f"  说明：{preset.note}")
        if preset.is_blacklist:
            lines.append("  口径：默认全推；命中屏蔽关键词 → 丢弃（不推、不通知）")
        else:
            lines.append("  口径：命中放行栏目 → 直接推群；其余 → 过滤（不开待定队列就不推）")
    return lines
