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
/steam 好友 [关键词]    看名单和序号（群聊打码，私聊完整）

【绑定】
/steam 绑定 #12 @小王   把序号 12 绑给小王
/steam 绑定 #12 123456  按用户 ID 绑给指定成员
/steam 绑定 #12         绑自己
/steam 绑定 7656119...  用 SteamID64 绑自己（不必是好友）
/steam 绑定 https://steamcommunity.com/id/xxx   主页链接也行
/steam 绑定 我          管理员：绑配置里的 owner_steamid
/steam 绑定列表 [页码]   管理员：看谁绑了谁
/steam 解绑            解绑自己
/steam 我              看自己绑的是谁

【查询】
/steam 查              查自己
/steam 查 @小王         查别人
/steam 查 #12           按序号查

【推送】
/steam 订阅            本群接收上线/开玩通知
/steam 订阅 全部        管理员：所有记录过的群一次开启
/steam 订阅 列表        看哪些群已订阅
/steam 取订            关闭本群推送
/steam 取订 全部        管理员：一次全部关闭

【本群显示设置】
/steam 设置            看本群当前设置
/steam 设置 昵称 masked 管理员：昵称 masked|full|hidden
/steam 设置 ID hidden   管理员：SteamID hidden|masked|full
/steam 设置 重置        管理员：恢复跟随插件配置

/steam 状态            管理员：看运行情况

【注意】
好友名单来自你的 Steam 好友列表，不含你自己——
绑自己用 /steam 绑定 我 或直接给 SteamID64 / 主页链接，别在名单里找自己。

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


def mask_name(name: str) -> str:
    """打码：多字昵称留**前 2 后 1**，中间全换成 *；短名字逐级退化。

    =========  ==================  ==========
    原昵称      打码后              规则
    =========  ==================  ==========
    Hanpaopao   Ha******o           前 2 后 1
    谷雨茶丶     谷雨*丶              前 2 后 1
    谷雨茶       谷*茶                三字凑不出中间段，退成前 1 后 1
    谷雨         谷*                  两字只留前 1
    谷          谷                    单字无从隐藏
    =========  ==================  ==========

    中文按字符算，不按字节。
    """
    text = str(name or "").strip()
    if not text:
        return "***"
    length = len(text)
    if length >= 4:
        return text[:2] + "*" * (length - 3) + text[-1]
    if length == 3:
        # 前 2 后 1 会覆盖全部三个字，改成首尾各留一个
        return text[0] + "*" + text[-1]
    if length == 2:
        return text[0] + "*"
    return text[0]


def mask_steamid(steamid: str) -> str:
    """SteamID64 打码：留前 5 位和后 4 位，中间用 * 补足原长度。

    17 位 ID 变成 ``76561********6703``。够认出是谁，又不能直接拿去用。
    """
    text = str(steamid or "").strip()
    if not text:
        return "***"
    if len(text) <= 9:
        return text[:2] + "*" * max(1, len(text) - 2)
    return text[:5] + "*" * (len(text) - 9) + text[-4:]


def roster_text(
    items: list[tuple[str, dict[str, Any]]],
    *,
    page: int,
    page_size: int,
    bound_count: int,
    title: str = "好友名单",
    name_mode: str = "full",
    id_mode: str = "hidden",
) -> str:
    """``items`` 已按序号排好序。

    ``name_mode``：``masked`` 昵称打码 / ``full`` 全名。
    ``id_mode``：``hidden`` 不显示 SteamID / ``masked`` 打码 / ``full`` 全量。
    两个维度互相独立，群里和私聊可以各配一套。
    """
    total = len(items)
    if total == 0:
        return "名单是空的。先让管理员执行 /steam 导入。"

    page_size = max(5, page_size)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    start = (page - 1) * page_size
    chunk = items[start : start + page_size]

    lines = [f"{title} {page}/{pages}（共 {total} 人，已绑定 {bound_count}）"]
    for sid, rec in chunk:
        index = rec.get("index", "?")
        name = str(rec.get("name") or "?").strip() or "?"
        if name_mode == "masked":
            name = mask_name(name)
        line = f"#{index:<3} {name}"
        if sid and id_mode == "full":
            line += f"  {sid}"
        elif sid and id_mode == "masked":
            line += f"  {mask_steamid(sid)}"
        lines.append(line)

    if pages > 1:
        lines.append(f"翻页：/steam 好友 {page + 1 if page < pages else 1}")
    if id_mode == "hidden":
        lines.append("绑定：/steam 绑定 #序号 @用户（SteamID 见私聊）")
    else:
        lines.append("绑定：/steam 绑定 #序号 @用户")
    return "\n".join(lines)


def bindings_text(
    rows: list[tuple[str, dict[str, Any]]],
    *,
    page: int,
    page_size: int,
    name_mode: str = "masked",
    id_mode: str = "hidden",
    subscribed: int = 0,
) -> str:
    """已绑定列表。``rows`` 是 ``(平台:用户ID, 绑定记录)``，已排好序。

    和好友名单一样受本群显示设置约束：群里默认打码且不给 SteamID。
    """
    total = len(rows)
    if total == 0:
        return "还没有任何绑定。\n用 /steam 绑定 <SteamID64 或 #序号> 生成第一条。"

    page_size = max(5, page_size)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    start = (page - 1) * page_size
    chunk = rows[start : start + page_size]

    lines = [f"已绑定 {page}/{pages}（共 {total} 个账号 · 订阅会话 {subscribed} 个）"]
    for offset, (key, rec) in enumerate(chunk, start=start + 1):
        platform, _, uid = key.partition(":")
        raw_name = str(rec.get("label") or "").strip()
        if not raw_name:
            name = "（未设昵称）"
        elif name_mode == "masked":
            name = mask_name(raw_name)
        else:
            name = raw_name
        steamid = str(rec.get("steamid") or "")
        line = f"{offset:>3}. {name} · {platform.upper()} {uid}"
        if id_mode == "full":
            line += f" · {steamid}"
        elif id_mode == "masked":
            line += f" · {mask_steamid(steamid)}"
        lines.append(line)

    if pages > 1:
        lines.append(f"翻页：/steam 绑定列表 {page + 1 if page < pages else 1}")
    lines.append(f"最近绑定：{fmt_rel_time(rows[0][1].get('updated'))}")
    if id_mode == "hidden":
        lines.append("（SteamID 已隐藏，私聊机器人发 /steam 绑定列表 可看全量）")
    return "\n".join(lines)


def subscriptions_text(
    items: list[tuple[str, dict[str, Any]]],
    *,
    sessions: list[str],
    page: int = 1,
    page_size: int = 20,
) -> str:
    """推送订阅清单。``items`` 是见过的会话，``sessions`` 是已订阅的 umo。"""
    total = len(items)
    if total == 0:
        return "还没有记录到任何群。先在群里发一次 /steam 任意指令。"

    subscribed = set(sessions)
    page_size = max(5, page_size)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    start = (page - 1) * page_size
    chunk = items[start : start + page_size]

    lines = [f"推送订阅 {page}/{pages}（共 {total} 个会话 · 已订阅 {len(subscribed)} 个）"]
    for umo, rec in chunk:
        mark = "✓" if umo in subscribed else "·"
        label = str(rec.get("label") or umo)
        platform = str(rec.get("platform") or "").upper()
        lines.append(f"  {mark} {label}" + (f" · {platform}" if platform else ""))

    if pages > 1:
        lines.append(f"翻页：/steam 订阅 列表 {page + 1 if page < pages else 1}")

    lines.extend(
        [
            "",
            "/steam 订阅         本群开启",
            "/steam 取订         本群关闭",
            "/steam 订阅 全部     管理员：所有记录过的群一次开启",
            "/steam 取订 全部     管理员：一次全部关闭",
        ]
    )
    return "\n".join(lines)


NAME_LABELS = {
    "masked": "打码（Ha******o 这样）",
    "full": "全名",
    "hidden": "不显示名单",
}
ID_LABELS = {
    "hidden": "不显示",
    "masked": "打码（76561********6703）",
    "full": "完整 17 位",
}


def settings_text(
    *,
    chat_label: str,
    name_mode: str,
    id_mode: str,
    name_overridden: bool,
    id_overridden: bool,
    private_id_mode: str,
) -> str:
    """本群显示设置的现状 + 怎么改。``*_overridden`` 表示该群自己改过。"""

    def src(overridden: bool) -> str:
        return "本群设置" if overridden else "插件默认"

    lines = [
        "名单显示设置",
        f"  会话：{chat_label}",
        f"  昵称：{NAME_LABELS.get(name_mode, name_mode)} · {src(name_overridden)}",
        f"  SteamID：{ID_LABELS.get(id_mode, id_mode)} · {src(id_overridden)}",
        "",
        "修改（管理员）：",
        "  /steam 设置 昵称 masked|full|hidden",
        "  /steam 设置 ID hidden|masked|full",
        "  /steam 设置 重置          回到插件默认",
        "",
        "只影响本群。私聊始终显示全名，SteamID 按插件配置里的私聊设置"
        f"（当前 {ID_LABELS.get(private_id_mode, private_id_mode)}）。",
    ]
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
