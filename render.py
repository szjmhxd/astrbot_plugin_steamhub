"""纯文本排版。

刻意不做图片：这套东西的价值在「序号 + 绑定」的数据流，不在视觉。要图的话
把这里的函数换成 HTML 模板渲染即可，数据层不用动。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .steam import state_text

HELP_TEXT = """Steam 好友中心

【一次性准备】
/steam 导入            用你的 Key 拉一遍好友名单（管理员）
/steam 好友 [关键词]    看看名单和序号

【绑定】
/steam 绑定 #12 @小王   把序号 12 绑给小王
/steam 绑定 #12 123456  按用户 ID 绑
/steam 绑定 我          绑自己（用配置里的 owner_steamid）
/steam 绑定 7656119...  直接给 SteamID64 / 主页链接
/steam 解绑            解绑自己
/steam 我              看自己绑的是谁

【查询】
/steam 查              查自己
/steam 查 @小王         查别人
/steam 查 #12           按序号查

【推送】
/steam 订阅            本群接收上线/开玩通知
/steam 取订            关闭本群推送

/steam 状态            管理员：看运行情况

【平台支持】
KOOK 已实测通过。QQ / 微信 / 飞书等其它平台未经验证，
绑定依赖各平台的用户 ID 取值，换平台请先跑一次 /steam 绑定 确认回执里的账号对得上。
"""


def fmt_minutes(minutes: Any) -> str:
    try:
        value = int(minutes or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return "0 分钟"
    if value < 60:
        return f"{value} 分钟"
    hours = value / 60
    if hours >= 100:
        return f"{hours:,.0f} 小时"
    text = f"{hours:.1f}".rstrip("0").rstrip(".")
    return f"{text} 小时"


def fmt_rel_time(timestamp: Any) -> str:
    try:
        ts = int(timestamp or 0)
    except (TypeError, ValueError):
        return "未知"
    if ts <= 0:
        return "未知"
    diff = int(time.time()) - ts
    if diff < 60:
        return "刚刚"
    if diff < 3600:
        return f"{diff // 60} 分钟前"
    if diff < 86400:
        return f"{diff // 3600} 小时前"
    if diff < 86400 * 30:
        return f"{diff // 86400} 天前"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def fmt_clock(timestamp: Any = None) -> str:
    ts = int(timestamp or time.time())
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _playing(player: dict[str, Any]) -> str:
    game = str(player.get("gameextrainfo") or "").strip()
    if game:
        appid = player.get("gameid")
        return f"正在玩 {game}" + (f"（appid {appid}）" if appid else "")
    return ""


def roster_text(
    items: list[tuple[str, dict[str, Any]]],
    *,
    page: int,
    page_size: int,
    bound_count: int,
    title: str = "好友名单",
) -> str:
    """``items`` 已按序号排好序。"""
    total = len(items)
    if total == 0:
        return "名单是空的。先让管理员执行 /steam 导入。"

    page_size = max(5, page_size)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    start = (page - 1) * page_size
    chunk = items[start : start + page_size]

    lines = [f"{title} {page}/{pages}（共 {total} 人，已绑定 {bound_count}）"]
    for _sid, rec in chunk:
        index = rec.get("index", "?")
        name = str(rec.get("name") or "?").strip() or "?"
        lines.append(f"#{index:<4} {name}")

    if pages > 1:
        lines.append(f"翻页：/steam 好友 {page + 1 if page < pages else 1}")
    lines.append("绑定：/steam 绑定 #序号 @用户")
    return "\n".join(lines)


def profile_text(
    *,
    player: dict[str, Any],
    owned: list[dict[str, Any]] | None,
    recent: list[dict[str, Any]] | None,
    ban: dict[str, Any] | None,
    show_recent: int = 5,
) -> str:
    if not player:
        return "查不到这个账号。资料私密或 SteamID 不存在。"

    name = str(player.get("personaname") or "未知")
    state = state_text(player.get("personastate"))
    playing = _playing(player)

    head = f"{name}｜{state}"
    if playing:
        head += f"｜{playing}"

    lines = [head, "─" * 20]

    if owned:
        total_minutes = sum(int(g.get("playtime_forever") or 0) for g in owned)
        lines.append(f"游戏库：{len(owned)} 款 / 累计 {fmt_minutes(total_minutes)}")

    if recent:
        two_week = sum(int(g.get("playtime_2weeks") or 0) for g in recent)
        lines.append(f"近两周：{fmt_minutes(two_week)}")
        lines.append("最近在玩：")
        for game in recent[:show_recent]:
            game_name = str(game.get("name") or f"appid {game.get('appid')}")
            week = fmt_minutes(game.get("playtime_2weeks"))
            total = fmt_minutes(game.get("playtime_forever"))
            lines.append(f"  · {game_name} —— 近两周 {week} / 总 {total}")

    if owned:
        # 已经在「最近在玩」里列过的就不重复出现在榜首
        recent_ids = {game.get("appid") for game in (recent or [])}
        top = [
            game
            for game in owned
            if int(game.get("playtime_forever") or 0) > 0 and game.get("appid") not in recent_ids
        ][:3]
        if top:
            lines.append("时长榜首：")
            for game in top:
                game_name = str(game.get("name") or f"appid {game.get('appid')}")
                lines.append(f"  · {game_name} —— {fmt_minutes(game.get('playtime_forever'))}")

    if ban:
        vac = int(ban.get("NumberOfVACBans") or 0)
        game_bans = int(ban.get("NumberOfGameBans") or 0)
        if ban.get("VACBanned") or vac or game_bans:
            parts = []
            if vac:
                parts.append(f"VAC {vac} 次")
            if game_bans:
                parts.append(f"游戏封禁 {game_bans} 次")
            if ban.get("CommunityBanned"):
                parts.append("社区封禁")
            if ban.get("EconomyBan") and ban.get("EconomyBan") != "none":
                parts.append(f"经济封禁 {ban.get('EconomyBan')}")
            lines.append(f"封禁：{' / '.join(parts)}")
        else:
            lines.append("封禁：无")

    last_off = player.get("lastlogoff")
    if last_off and not playing:
        lines.append(f"上次离线：{fmt_rel_time(last_off)}")

    profile_url = str(player.get("profileurl") or "").strip()
    if profile_url:
        lines.append(profile_url)

    return "\n".join(lines)


def notify_playing(*, name: str, game: str, appid: Any = None) -> str:
    suffix = f"（appid {appid}）" if appid else ""
    return f"🎮 {name} 开始玩了\n{game}{suffix}\n{fmt_clock()}"


def notify_stop(*, name: str, game: str, seconds: int = 0) -> str:
    text = f"⏹ {name} 结束游戏\n{game}"
    if seconds >= 60:
        text += f"\n本次约 {fmt_minutes(seconds // 60)}"
    return text


def notify_online(name: str) -> str:
    return f"🟢 {name} 上线了"


def notify_offline(name: str) -> str:
    return f"⚪ {name} 下线了"
