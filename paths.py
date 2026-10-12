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

"""插件根目录与随插件分发的资源路径（唯一真源）。

只依赖标准库，任何模块都能安全导入它：``render/card.py`` 这类渲染模块不必为了
拿一个路径去 import ``config.py``（那会连带把配置后端也初始化一遍），
也不需要在每个文件里各写一遍 ``Path(__file__).parents[1]``。

目录约定（``gsuid_core/plugins/BiliSubscribe/``）::

    paths.py       ← 本文件，路径唯一真源
    ICON.png       ← 核心硬编码按 ``plugins/<插件名>/ICON.png`` 取，必须留在根目录
    assets/        ← 帮助图
    fonts/         ← 卡片字体
    game_rules/    ← 游戏白名单规则（一个游戏一个 JSON，随插件分发）
    data/          ← 核心没有 get_res_path 时的本地兜底数据目录
"""

from pathlib import Path

#: 插件根目录，即 ``gsuid_core/plugins/BiliSubscribe/``
PLUGIN_ROOT = Path(__file__).resolve().parent

#: 随插件分发的静态资源（帮助图等）
ASSETS_DIR = PLUGIN_ROOT / "assets"

#: 卡片字体目录；``render/card.py`` 会递归扫描插件目录下所有 ttf/ttc/otf，放哪一层都认
FONTS_DIR = PLUGIN_ROOT / "fonts"

#: 游戏白名单规则目录：**一个游戏一个 JSON**（见 ``core/games.py``）。
#: 随插件一起分发，打开插件目录就能看到、直接改；某个游戏没了就删掉它的 JSON。
#: 要新增游戏：照抄 ``异环.json`` 加一个文件即可，不用动代码、也不用重启
GAME_RULES_DIR = PLUGIN_ROOT / "game_rules"

#: 核心没有 ``gsuid_core.data_store.get_res_path`` 时的本地兜底数据目录
LOCAL_DATA_DIR = PLUGIN_ROOT / "data"

#: 群聊帮助图（``bili帮助`` 发这张；图不在就退回文字帮助）
HELP_IMAGE = ASSETS_DIR / "bilihelp.jpg"

#: 管理员帮助图（``bili管理帮助`` 发这张；图不在就退回文字帮助）
HELP_ADMIN_IMAGE = ASSETS_DIR / "biliadminhelp.jpg"
