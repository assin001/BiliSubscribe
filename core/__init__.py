# BiliSubscribe - 订阅 B站 UP 主的视频与动态更新，推送到群聊
# Copyright (C) 2026  Dowond (bilibili-subscribe)
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

"""业务内核：配置、订阅数据、运行时状态与动态过滤规则。

注意两处**依赖导入时机**的地方，别改成惰性导入：

- ``models.py`` 会被 ``storage.py`` 在导入时连带导入；
- ``game_rules_db.py`` 由本文件导入。

两者都在模块顶层注册控制台的「数据库管理」页（``@site.register_admin``，
它靠调用栈认插件目录），import 晚了就赶不上核心启动时的建表与菜单拉取。
"""
from . import game_rules_db  # noqa: F401  （导入即注册，见上面的说明）
