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

"""进程内运行状态，供状态卡片读取。

不落盘：重启即清零。定时任务与命令都跑在同一个事件循环里，直接读写这个 dict 就够了。
"""
from typing import Any, Dict

STATE: Dict[str, Any] = {
    "running": False,       # 是否有检查正在跑
    "checks": 0,            # 累计检查轮数
    "last_check_ts": 0.0,   # 上次检查完成的时间戳
    "last_updates": 0,      # 上一轮有更新的 UP 数
    "pushed": 0,            # 累计推送的消息条数（群 × 条）
    "last_push_ts": 0.0,    # 上次真正推送出去的时间戳
    "filtered": 0,          # 白名单外被过滤掉的动态条数（不推群）
}
