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

"""把推送内容画成一张卡片图（按原设计稿的 600px 模板渲染）。

模板是 600px 设计稿，卡片按 **1.5 倍**放大到 900px（字号/间距同步放大），对应关系：

    ┌────────────────────────────────┐
    │  B站订阅更新            [粉底]  │  优设标题黑 28→42，字距 1px
    │  异环 · 投稿了文章              │  18→27
    ├────────────────────────────────┤
    │  海特洛特别资讯 | 曳尔沐         │  文章标题 24→36 粗体 #222
    │  #异环# #异环曳尔沐#            │  标签 16→24 霞鹜文楷 #0057AE
    │  「你好呀~又见面了，我的朋友~？」 │  引语 18→27 #444
    │  正文……（行高 1.6）https://…   │  正文 16→24 #222，链接 #7B68EE
    │  ┌──────────────────────────┐  │  配图铺满宽度、高 300→450（cover 裁切）
    │  └──────────────────────────┘  │  圆角 4→6
    │  10-04 11:00      BiliSubscribe │  底部 14→21 #666
    └────────────────────────────────┘

正文会按模板**拆成三块**再分别上色：``#标签#``（整行都是标签时）/ ``「引语」`` /
其余正文；正文里的链接单独用蓝紫色画（模板里的 ``<a class="link">``）。

字体按模板分角色找（``_ROLE_KEYS``）：优设标题黑→头部、思源黑体→标题与正文、
霞鹜文楷→标签、Orbitron→时间/链接。**PIL 读不了 woff2**，所以要么系统里装了对应的
ttf/otf，要么把字体文件丢进插件目录（``BiliSubscribe/``，文件名带角色关键字即可）。
实在找不到就用华文细黑/黑体兜底，绝不会因为缺字体而画不出来。
"""
import re
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from gsuid_core.logger import logger

from ..paths import PLUGIN_ROOT

# ---------------------------------------------------------------- 尺寸（模板 ×1.5）
CARD_W = 900
PAD = 30  # 模板 content padding 20px
CARD_RADIUS = 18  # 模板 12px（只有上面两个角圆）
HEADER_PAD_X = 30
IMG_H = 450  # 模板 .image-box 300px，cover 裁切
IMG_RADIUS = 6  # 模板 4px
MAX_BODY_LINES = 18  # 正文最多画这么多行，超了截断（点链接看原文）
MAX_FULL_H = 4096  # 整卡最高（有些客户端发不出去超长图）

# ---------------------------------------------------------------- 配色（照模板）
PINK = (251, 114, 153, 255)  # --bili-pink #FB7299
WHITE = (255, 255, 255, 255)
TEXT_C = (34, 34, 34, 255)  # --text-main #222
SUB_C = (102, 102, 102, 255)  # --text-sub #666
QUOTE_C = (68, 68, 68, 255)  # .quote #444
TAG_C = (0, 87, 174, 255)  # .tags #0057AE
LINK_C = (123, 104, 238, 255)  # .link #7B68EE

# ---------------------------------------------------------------- 头部样式
# 头像 + UP 名 + UID 胶囊 + "订阅更新"，右侧一个指向**原文链接**的二维码
HEADER_H = 200  # 头部高度
AVATAR_D = 104  # 头像直径
AVATAR_RING = 4  # 头像外白圈粗细（让头像"浮"在粉底上）
QR_BOX = 100  # 二维码本身边长（块里再留一圈白边）
QR_PAD = 14  # 二维码外面那圈白底的内边距（就是"留点边框"）
QR_RADIUS = 16  # 白底方块的圆角
QR_BORDER = (255, 214, 232, 255)  # 白底方块的描边（淡粉底上纯白不够显眼）
PILL_H = 34  # UID 胶囊高度
SZ_NAME = 34  # UP 名
SZ_NOTE = 24  # "订阅更新"
SZ_PILL = 19  # UID 胶囊里的字
HEADER_BG = (255, 234, 243, 255)  # 淡粉（左边略深）
HEADER_BG2 = (255, 249, 252, 255)  # 更淡（右边近白）
NAME_C = (242, 86, 140, 255)  # UP 名：粉
NOTE_C = (244, 133, 167, 255)  # "订阅更新"
PILL_BG = (255, 226, 238, 255)
PILL_BORDER = (255, 175, 205, 255)
PILL_TEXT_C = (230, 74, 126, 255)
QR_DARK = "#F2568C"  # 二维码前景色（粉）

# 字号（模板 ×1.5）
SZ_SUB = 27  # .header-sub 18
SZ_TITLE = 36  # .article-title 24
SZ_TAG = 24  # .tags 16
SZ_QUOTE = 27  # .quote 18
SZ_DESC = 24  # .desc 16
SZ_LINK = 22  # .link 15
SZ_FOOT = 21  # .footer 14
# 行高（模板 line-height）
LH_TITLE = 47  # 1.3
LH_TAG = 34
LH_QUOTE = 40
LH_DESC = 38  # 1.6
LH_FOOT = 30

# ---------------------------------------------------------------- 字体
# 字体按**插件根目录**递归找：放在 fonts/（或根下任意子目录）都认
_PLUGIN_DIR = PLUGIN_ROOT
# 想换字体：把 .ttf/.ttc/.otf 丢进 fonts/ 即可，文件名带关键字就认
_ROLE_KEYS: Dict[str, Tuple[str, ...]] = {
    # 关键词顺序 = 优先级
    "header": ("youshebiaotihei", "youshebiaoti", "优设"),
    "title": (
        # 静态粗体优先；可变字体（VariableFont）也算：下面 _apply_weight 会把字重拉到 Bold
        "sourcehansanssc-bold",
        "sourcehansanssc-semibold",
        "sourcehansanssc",
        "notosanssc-bold",
        "notosanssc",
        "notosanscjk-bold",
        "notosanscjk",
        "msyhbd",
    ),
    "body": (
        "sourcehansanssc",
        "sourcehansans",
        "notosanssc",
        "notosanscjk",
        "xihei",  # 华文细黑
        "stheiti",
        "simhei",
        "msyh",
    ),
    "tags": ("lxgwwenkai", "wenkai", "霞鹜"),
    "mono": ("orbitron",),
}
# 角色找不到时的兜底顺序（华文细黑 → 黑体 → 雅黑）
_ROLE_FALLBACK: Tuple[str, ...] = (
    "C:/Windows/Fonts/STXIHEI.TTF",  # 华文细黑
    "C:/Windows/Fonts/STXihei.ttf",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "C:/Windows/Fonts/simhei.ttf",  # 黑体
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
_FONT_ROOTS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "C:/Windows/Fonts",
)
_BOLDISH = ("bold", "heavy", "black", "semibold")

_font_cache: Dict[str, Optional[str]] = {}


def _local_fonts() -> List[str]:
    """插件目录里自己带的字体，**含子目录**（就是 ``fonts/``）。

    PIL 只吃 ttf/ttc/otf，woff2 不认（那要先转格式）。
    """
    out: List[str] = []
    for pattern in ("*.ttf", "*.ttc", "*.otf"):
        out.extend(str(p) for p in sorted(_PLUGIN_DIR.rglob(pattern)) if p.is_file())
    return list(dict.fromkeys(out))  # Windows 上大小写不敏感，去个重


_sys_fonts_cache: Optional[List[str]] = None


def _system_fonts() -> List[str]:
    """系统里所有字体文件（只扫一次，缓存住）。"""
    global _sys_fonts_cache
    if _sys_fonts_cache is not None:
        return _sys_fonts_cache
    import glob

    found: List[str] = []
    for root in _FONT_ROOTS:
        if not Path(root).is_dir():
            continue
        for pattern in ("*.ttf", "*.otf", "*.ttc"):
            found.extend(glob.glob(f"{root}/**/{pattern}", recursive=True))
    _sys_fonts_cache = list(dict.fromkeys(found))
    return _sys_fonts_cache


# 某个角色没有专用字体时，跟着哪个角色的字体走（保证整卡同一套字形）
_ROLE_PARENT: Dict[str, str] = {
    "header": "body",
    "title": "body",
    "tags": "body",
    "mono": "body",
}


def _search_font(role: str) -> Optional[str]:
    """按角色找字体：**关键词按优先级**（插件目录优先）→ 复用正文 → 系统兜底。

    关键词顺序就是优先级：正文写成 ``思源黑体 → Noto → 华文细黑 → 黑体``，
    所以装了思源黑体就用它，没装退华文细黑（默认观感），都没有才用系统黑体。
    """
    keys = _ROLE_KEYS.get(role, ())
    pool = _local_fonts() + _system_fonts()
    for key in keys:
        for path in pool:
            if key in path.lower():
                return path
    parent = _ROLE_PARENT.get(role)
    if parent:
        got = font_path(parent)
        if got:
            return got
    for path in _ROLE_FALLBACK:
        if Path(path).is_file():
            return path
    return pool[0] if pool else None


def font_path(role: str = "body") -> Optional[str]:
    """当前这个角色用的字体文件（排查字体问题时看这个）。

    第一次解析时把「角色 → 字体」打一行日志：字体没认上（文件名不匹配、woff2 没
    转格式…）能当场看出来，不用等卡片画歪了才发现。
    """
    if role not in _font_cache:
        _font_cache[role] = _search_font(role)
        path = _font_cache[role]
        if (role, path) not in _LOGGED_FONTS:
            _LOGGED_FONTS.add((role, path))
            if path:
                extra = "（可变字重）" if _is_variable(path) else ""
                logger.info(f"B站订阅：卡片字体 {role} → {Path(path).name}{extra}")
            else:
                logger.warning(f"B站订阅：卡片字体 {role} 没找到，退回 PIL 默认字体")
    return _font_cache[role]


# 可变字体（VariableFont）按角色调字重：标题/头部要粗，正文常规
_VAR_WEIGHT: Dict[str, str] = {"title": "Bold", "header": "Bold", "body": "Regular"}


def _is_variable(path: Optional[str]) -> bool:
    """是不是可变字体（这类字体自带多档字重，不用靠描边假装加粗）。"""
    return bool(path) and "variable" in str(path).lower()


_LOGGED_FONTS: set = set()  # 哪些「角色 → 字体」组合已经报过日志了


# 这一次渲染里，各角色是否**真的**调到了可变字重（没调到就要靠描边补）
_var_applied: Dict[str, bool] = {}


def _apply_weight(font, role: str):
    """可变字体按轴调字重（Pillow 支持才行）；静态字体原样返回。"""
    want = _VAR_WEIGHT.get(role)
    if font is None or not want:
        return font
    try:
        font.set_variation_by_name(want)
        _var_applied[role] = True
    except Exception:
        try:
            font.set_variation_by_axes([700 if want == "Bold" else 400])
            _var_applied[role] = True
        except Exception:
            _var_applied[role] = False
    return font


def _font(size: int, role: str = "body"):
    """按角色取字体；角色字体缺失就复用正文字体、再退核心自带字体、最后 PIL 默认。"""
    try:
        from PIL import ImageFont
    except Exception:
        return None
    path = font_path(role)
    if path:
        try:
            return _apply_weight(ImageFont.truetype(path, size), role)
        except Exception as e:
            logger.warning(f"B站订阅：字体 {path} 加载失败（{e}），换兜底")
    try:
        from gsuid_core.utils.fonts.fonts import core_font

        return core_font(size, "bold" in role)
    except Exception:
        pass
    try:
        return ImageFont.load_default()
    except Exception:
        return None


# ---------------------------------------------------------------- 文本工具
_TAG_RE = re.compile(r"#[^#\s]{1,30}#")
_URL_RE = re.compile(r"https?://[^\s，。；）】」]+")
_QUOTE_HEAD = ("「", "“", '"', "『")


def _text_w(draw, text: str, font) -> float:
    try:
        return draw.textlength(text, font=font)
    except Exception:
        return float(len(text) * 14)


def _wrap(draw, text: str, font, max_w: float) -> List[str]:
    """按像素宽度折行（中文没空格可断，逐字符量；显式换行保留）。"""
    lines: List[str] = []
    for raw in str(text or "").split("\n"):
        raw = raw.rstrip()
        if not raw:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        cur = ""
        for ch in raw:
            if _text_w(draw, cur + ch, font) <= max_w or not cur:
                cur += ch
            else:
                lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _split_body(text: str) -> Dict[str, List[str]]:
    """把正文按模板拆成 标签 / 引语 / 正文 三块（模板里样式不同）。"""
    tags: List[str] = []
    quotes: List[str] = []
    desc: List[str] = []
    for raw in str(text or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _TAG_RE.search(line) and not _TAG_RE.sub("", line).strip():
            tags.append(line)  # 整行都是 #标签#
        elif line.startswith(_QUOTE_HEAD):
            quotes.append(line)  # 「引语」
        else:
            desc.append(line)
    return {"tags": tags, "quotes": quotes, "desc": desc}


def _split_links(line: str) -> List[Tuple[str, bool]]:
    """一行正文 → ``[(片段, 是否链接)]``，链接单独上色。"""
    out: List[Tuple[str, bool]] = []
    pos = 0
    for m in _URL_RE.finditer(line):
        if m.start() > pos:
            out.append((line[pos : m.start()], False))
        out.append((m.group(0), True))
        pos = m.end()
    if pos < len(line):
        out.append((line[pos:], False))
    return out or [(line, False)]


# ---------------------------------------------------------------- 图片
def _load_cover(data: Optional[bytes]):
    """封面 bytes → PIL 图（动图取首帧）。"""
    if not data:
        return None
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        img = Image.open(BytesIO(data))
        try:
            img.seek(0)
        except Exception:
            pass
        return img.convert("RGB")
    except Exception as e:
        logger.warning(f"B站订阅：卡片配图打不开（{e}）")
        return None


def _cover_crop(img, w: int, h: int):
    """等比放大后居中裁切到 (w, h)，对应模板的 ``background: center/cover``。"""
    from PIL import Image

    scale = max(w / max(1, img.width), h / max(1, img.height))
    nw = max(w, int(img.width * scale + 0.5))
    nh = max(h, int(img.height * scale + 0.5))
    resized = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return resized.crop((left, top, left + w, top + h))


# ---------------------------------------------------------------- 头部素材
def _header_bg(width: int, height: int):
    """头部底色：淡粉，横向从略深渐变到近白（新模板那种很浅的粉）。"""
    from PIL import Image

    strip = Image.new("RGBA", (width, 1))
    px = strip.load()
    for x in range(width):
        t = x / max(1, width - 1)
        px[x, 0] = tuple(int(a + (b - a) * t) for a, b in zip(HEADER_BG, HEADER_BG2))
    return strip.resize((width, height))


def _circle_avatar(data: Optional[bytes], size: int):
    """头像 bytes → 圆形图（先按 cover 裁成正方形再套圆）；拿不到就返回 None。"""
    if not data:
        return None
    try:
        from PIL import Image, ImageDraw

        img = Image.open(BytesIO(data)).convert("RGBA")
    except Exception:
        return None
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _qr_image(text: str, box: int):
    """把**原文链接**画成二维码（粉色前景、白底）。

    用的是 Python 的 ``qrcode``（插件依赖里已有；网页那套 QRCode.js 在这里用不上，
    卡片是服务端 PIL 画的）。参数对应关系：前景/背景色 → ``fill_color/back_color``、
    容错级别 → ``error_correction``、边距 → ``border``、块大小 → ``box_size``。
    没装库或生成失败就返回 None，卡片那格留空，不影响其它内容。
    """
    text = str(text or "").strip()
    if not text:
        return None
    try:
        import qrcode
        from PIL import Image
        from qrcode.constants import ERROR_CORRECT_M
    except Exception as e:
        logger.warning(f"B站订阅：没有 qrcode 库（{e}），卡片不画二维码")
        return None
    try:
        qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=8, border=1)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color=QR_DARK, back_color="white")
        pil = img.get_image().convert("RGBA") if hasattr(img, "get_image") else img.convert("RGBA")
    except Exception as e:
        logger.warning(f"B站订阅：二维码生成失败（{e}）")
        return None
    return pil.resize((box, box), Image.NEAREST)


def _pill_size(draw, text: str, font, height: int = PILL_H) -> Tuple[int, int]:
    """量一下 UID 胶囊要占多大（只量不画，用来给名字留出宽度）。"""
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0] + 26, height


def _pill(draw, x: float, y: float, text: str, font, height: int = PILL_H) -> int:
    """画一个圆角胶囊（UID 那种），返回它占的宽度。"""
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    width = tw + 26
    draw.rounded_rectangle(
        (x, y, x + width, y + height),
        radius=height // 2,
        fill=PILL_BG,
        outline=PILL_BORDER,
        width=2,
    )
    draw.text(
        (x + 13 - bbox[0], y + (height - th) / 2 - bbox[1]),
        text,
        font=font,
        fill=PILL_TEXT_C,
    )
    return width


MAX_CARD_BYTES = 300 * 1024  # 推送图体积上限（移动端/流量友好）


def _encode_card(card, has_photo: bool) -> bytes:
    """编码卡片：带图走 JPEG（照片压得小），纯文字走 PNG（小字更锐利）。

    封面已经按固定尺寸裁过，正常 100~200KB；万一超了 ``MAX_CARD_BYTES``
    （封面是细节极多的照片那类）就降质量重编，别把推送图顶得太肥 ——
    注意别去压字体：字体是本机渲染用的，**不会**跟着图片发出去。
    """
    rgb = card.convert("RGB")
    best = b""
    for quality in (92, 85, 75):
        out = BytesIO()
        try:
            if has_photo or quality < 92:
                rgb.save(out, format="JPEG", quality=quality, subsampling=0, optimize=True)
            else:
                rgb.save(out, format="PNG")
        except Exception:
            continue
        data = out.getvalue()
        if not best or len(data) < len(best):
            best = data
        if len(data) <= MAX_CARD_BYTES:
            return data
    return best


# ---------------------------------------------------------------- 渲染
def render(
    *,
    up_name: str = "",
    action: str = "",
    title: str = "",
    body: str = "",
    time_text: str = "",
    cover: Optional[bytes] = None,
    avatar: Optional[bytes] = None,
    uid: str = "",
    qr_text: str = "",
    subtitle: str = "订阅更新",
    note: str = "",
) -> Optional[bytes]:
    """画一张卡片，返回图片 bytes；画不出来返回 None（调用方退回文字发法）。

    头部是"头像 + UP 名 + UID 胶囊 + 订阅更新 + 二维码"那版：

    - ``avatar``：UP 头像 bytes（拿不到就画个粉圆 + 名字首字）；
    - ``uid``：UP 的 UID，画进名字右边的胶囊（空着不画）；
    - ``qr_text``：二维码内容 —— **原文链接**（扫了直接打开这条动态/视频/直播间）；
    - ``subtitle``：名字下面那行小字，默认「订阅更新」，开播卡用「开播了」；
    - ``action`` / ``note``：新头部里没有它们的位置，保留参数只为兼容调用方。
    """
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        logger.warning(f"B站订阅：没有 PIL，卡片画不了（{e}），这条退回文字推送")
        return None

    f_sub = _font(SZ_SUB, "body")
    f_title = _font(SZ_TITLE, "title")
    f_tag = _font(SZ_TAG, "tags")
    f_quote = _font(SZ_QUOTE, "body")
    f_desc = _font(SZ_DESC, "body")
    f_link = _font(SZ_LINK, "mono")
    f_foot = _font(SZ_FOOT, "mono")
    if f_desc is None:
        return None

    inner_w = CARD_W - PAD * 2
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))

    parts = _split_body(body)
    title_lines = _wrap(probe, title, f_title, inner_w) if title else []
    tag_lines: List[str] = []
    for t in parts["tags"]:
        tag_lines.extend(_wrap(probe, t, f_tag, inner_w))
    quote_lines: List[str] = []
    for q in parts["quotes"]:
        quote_lines.extend(_wrap(probe, q, f_quote, inner_w))
    desc_runs: List[List[Tuple[str, bool]]] = [
        _split_links(d) for d in parts["desc"]
    ]

    # 先按块量高度（PIL 没有自动排版，只能自己算）：起点要和下面绘制时一致
    header_h = HEADER_H
    y = header_h + 20
    if title_lines:
        y += len(title_lines) * LH_TITLE + 12
    if tag_lines:
        y += len(tag_lines) * LH_TAG + 10
    if quote_lines:
        y += len(quote_lines) * LH_QUOTE + 16
    desc_h = 0
    desc_line_count = 0
    for runs in desc_runs:
        for _text, is_link in runs:
            lines = _wrap(probe, _text, f_link if is_link else f_desc, inner_w)
            desc_h += len(lines) * LH_DESC
            desc_line_count += len(lines)
    clipped = desc_line_count > MAX_BODY_LINES
    if clipped:  # 正文太长：只画前 MAX_BODY_LINES 行
        desc_h = MAX_BODY_LINES * LH_DESC
    if desc_h:
        y += desc_h + 16

    cover_img = _load_cover(cover)
    if cover_img is not None:
        y += IMG_H + 16
    y += LH_FOOT + 8  # 底部一行

    total_h = min(max(y, 200), MAX_FULL_H)
    card = Image.new("RGBA", (CARD_W, total_h), WHITE)
    draw = ImageDraw.Draw(card)

    # 头部：淡粉底（上面两角圆，模板 border-radius: 12px 12px 0 0）
    draw.rounded_rectangle((0, 0, CARD_W - 1, total_h - 1), radius=CARD_RADIUS, fill=WHITE)
    header_img = _header_bg(CARD_W, header_h)
    # 蒙版必须和 paste 的图同尺寸：矩形的下边故意画到蒙版外面，好让底部两角是直角
    header_mask = Image.new("L", (CARD_W, header_h), 0)
    ImageDraw.Draw(header_mask).rounded_rectangle(
        (0, 0, CARD_W - 1, header_h + CARD_RADIUS), radius=CARD_RADIUS, fill=255
    )
    card.paste(header_img, (0, 0), header_mask)

    def _text(xy, txt, font, color, stroke: int = 0):
        if stroke:
            draw.text(xy, txt, font=font, fill=color, stroke_width=stroke, stroke_fill=color)
        else:
            draw.text(xy, txt, font=font, fill=color)

    # 没找到"标题专用字体"时（都退回正文字体），描 1px 边补一点字重；
    # 但如果可变字重真的调上了（标题已 Bold），就不描，免得过重
    title_path, body_path = font_path("title"), font_path("body")
    title_stroke = 1 if (title_path == body_path and not _var_applied.get("title")) else 0

    # ---- 头像（外圈白色，让它"浮"在粉底上）；没有头像就用粉圆 + 名字首字 ----
    av_x, av_y = HEADER_PAD_X, (header_h - AVATAR_D) // 2
    draw.ellipse(
        (
            av_x - AVATAR_RING,
            av_y - AVATAR_RING,
            av_x + AVATAR_D + AVATAR_RING - 1,
            av_y + AVATAR_D + AVATAR_RING - 1,
        ),
        fill=WHITE,
    )
    av_img = _circle_avatar(avatar, AVATAR_D)
    if av_img is not None:
        card.paste(av_img, (av_x, av_y), av_img)
    else:
        draw.ellipse((av_x, av_y, av_x + AVATAR_D - 1, av_y + AVATAR_D - 1), fill=PINK)
        ch = str(up_name or "B")[:1]
        f_av = _font(int(AVATAR_D * 0.46), "title")
        if f_av is not None and ch:
            bbox = draw.textbbox((0, 0), ch, font=f_av)
            _text(
                (
                    av_x + (AVATAR_D - (bbox[2] - bbox[0])) / 2 - bbox[0],
                    av_y + (AVATAR_D - (bbox[3] - bbox[1])) / 2 - bbox[1],
                ),
                ch,
                f_av,
                WHITE,
            )

    # ---- 名字 + UID 胶囊 + "订阅更新"（整块在头部垂直居中）----
    f_name = _font(SZ_NAME, "title")
    f_pill = _font(SZ_PILL, "body")
    f_note = _font(SZ_NOTE, "body")
    text_x = av_x + AVATAR_D + 22
    qr_img = _qr_image(qr_text, QR_BOX)
    # 名字的可用宽度 = 到"二维码/右边距"为止，**再扣掉 UID 胶囊**（不然长名字会把
    # 胶囊/二维码挤重叠 —— 探针渲出来过）
    right_edge = (
        CARD_W - HEADER_PAD_X - (QR_BOX + QR_PAD + 18) if qr_img is not None else CARD_W - HEADER_PAD_X
    )
    pill_w = 0
    if uid:
        pill_w = _pill_size(draw, f"UID {uid}", f_pill)[0] + 14
    raw_name = str(up_name or "未知UP")
    name_w = max(120, right_edge - text_x - pill_w - 10)
    name_lines = _wrap(draw, raw_name, f_name, name_w)
    if len(name_lines) <= 1:
        name = name_lines[0] if name_lines else raw_name
    else:
        # 要截断：折行时得**先给省略号留出宽度**，否则"名字 + …"会超出去把 UID 挤到二维码上
        ell_w = _text_w(draw, "…", f_name)
        name_lines = _wrap(draw, raw_name, f_name, max(80, name_w - ell_w - 2))
        name = ((name_lines[0] if name_lines else "") or raw_name).rstrip() + "…"
    block_h = SZ_NAME + 10 + SZ_NOTE
    name_y = (header_h - block_h) // 2
    _text((text_x, name_y), name, f_name, NAME_C, stroke=title_stroke)
    if uid:
        # 胶囊按名字**实际字形**的垂直中心对齐 —— 按字号框算的话，CJK 字形没占满框，
        # 看着就是"名字和 UID 不在一条水平线上"（用户圈出来过）
        nb = draw.textbbox((text_x, name_y), name, font=f_name, stroke_width=title_stroke)
        _pill(
            draw,
            text_x + _text_w(draw, name, f_name) + 14,
            (nb[1] + nb[3]) / 2 - PILL_H / 2,
            f"UID {uid}",
            f_pill,
        )
    _text((text_x, name_y + SZ_NAME + 10), str(subtitle or "订阅更新"), f_note, NOTE_C)

    # ---- 右上角二维码（内容是原文链接；生成不出来就不画）----
    if qr_img is not None:
        qx = CARD_W - HEADER_PAD_X - QR_BOX
        qy = (header_h - QR_BOX) // 2
        draw.rounded_rectangle(
            (qx - QR_PAD, qy - QR_PAD, qx + QR_BOX + QR_PAD - 1, qy + QR_BOX + QR_PAD - 1),
            radius=QR_RADIUS,
            fill=WHITE,
            outline=QR_BORDER,
            width=2,
        )
        card.paste(qr_img, (qx, qy))

    y = header_h + 20
    for line in title_lines:
        _text((PAD, y), line, f_title, TEXT_C, stroke=title_stroke)
        y += LH_TITLE
    if title_lines:
        y += 12
    for line in tag_lines:
        _text((PAD, y), line, f_tag, TAG_C)
        y += LH_TAG
    if tag_lines:
        y += 10
    for line in quote_lines:
        _text((PAD, y), line, f_quote, QUOTE_C)
        y += LH_QUOTE
    if quote_lines:
        y += 16

    drawn = 0
    for runs in desc_runs:
        for text, is_link in runs:
            font = f_link if is_link else f_desc
            color = LINK_C if is_link else TEXT_C
            for line in _wrap(draw, text, font, inner_w):
                if drawn >= MAX_BODY_LINES:
                    break
                _text((PAD, y), line, font, color)
                y += LH_DESC
                drawn += 1
        if clipped and drawn >= MAX_BODY_LINES:
            break
    if clipped:
        # 提示用正文字体画：Orbitron 这类英文字体没有中文字形，会画成豆腐块
        more = f"……（共 {desc_line_count} 行，点链接看全文）"
        _text((PAD, y), more, f_sub, SUB_C)
    if desc_runs:
        y += 16

    if cover_img is not None:
        shot = _cover_crop(cover_img, inner_w, IMG_H)
        mask = Image.new("L", (inner_w, IMG_H), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, inner_w - 1, IMG_H - 1), radius=IMG_RADIUS, fill=255
        )
        card.paste(shot, (PAD, y), mask)
        y += IMG_H + 16

    # 底部：左时间、右署名（模板 .footer 是 content 里面一行，不加底色）
    _text((PAD, y), time_text or "", f_foot, SUB_C)
    sig = "BiliSubscribe"
    _text((CARD_W - PAD - _text_w(draw, sig, f_foot), y), sig, f_foot, SUB_C)

    return _encode_card(card, has_photo=cover_img is not None)


# ---------------------------------------------------------------- 附：动图压缩
# 注：以前这里还有个 grid_images()（把多张静态图拼成一张网格图）。
# 已按需求删掉 —— 拼图要把图下载再合成，动图一过手就变静态图，观感也不如原图，
# 而各张图放同一条消息里 QQ 自己会排版。现在配图一律按原链接一张张发。


def shrink_gif(
    data: bytes,
    max_w: int = 480,
    max_h: int = 1600,
    max_frames: int = 30,
) -> Optional[bytes]:
    """把大动图压小（**保留动画**）：限宽高、限帧数、降到 128 色。

    B站的动图单张能到 7.8MB，个别平台遇到这么大的会当静态图发；压几百 KB 更容易动起来。
    非动图、单帧或压缩失败都返回 None（调用方按原图发）。
    """
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        src = Image.open(BytesIO(data))
        frames_total = int(getattr(src, "n_frames", 1) or 1)
        if frames_total <= 1:
            return None
        frames = []
        durations = []
        for i in range(min(frames_total, max_frames)):
            src.seek(i)
            frame = src.convert("RGB")
            w, h = frame.size
            scale = min(1.0, max_w / max(1, w), max_h / max(1, h))
            if scale < 1.0:
                frame = frame.resize(
                    (max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS
                )
            frames.append(frame.convert("P", palette=Image.ADAPTIVE, colors=128))
            durations.append(max(20, int(src.info.get("duration") or 80)))
        out = BytesIO()
        frames[0].save(
            out,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=durations,
            loop=0,
            optimize=True,
            disposal=2,
        )
        return out.getvalue()
    except Exception as e:
        logger.warning(f"B站订阅：动图压缩失败（{e}），按原图发")
        return None
