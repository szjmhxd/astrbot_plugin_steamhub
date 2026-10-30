"""Steam Web API 客户端。

只封装本插件真正用到的接口，全部需要 API Key：

- ``ISteamUser/GetFriendList/v1``            拉好友名单（要求好友列表公开）
- ``ISteamUser/GetPlayerSummaries/v2``       批量取昵称/头像/在线状态/当前游戏，一次 100 个
- ``IPlayerService/GetOwnedGames/v1``        游戏库与总时长
- ``IPlayerService/GetRecentlyPlayedGames/v1`` 近两周游玩
- ``ISteamUser/GetPlayerBans/v1``            VAC / 游戏封禁
- ``ISteamUser/ResolveVanityURL/v1``         自定义主页名 → SteamID64

几个容易踩的点
--------------
1. 私密资料什么都不返回。``GetFriendList`` 会直接少人（对方资料私密时 Valve
   不会把他列进你的好友列表），这不是 bug，得在指令输出里说明。
2. ``GetPlayerSummaries`` 的数组参数是逗号拼字符串，一批最多 100 个。
3. 429 / 5xx 要退避重试，401 / 403 重试没用，直接报给用户。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from typing import Any

try:  # AstrBot 本体已内置 aiohttp；脱离运行环境做离线测试时允许缺失
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

try:  # 单测环境没有 astrbot
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

# aiohttp 缺失时仍能抛出有意义的错误，而不是在 import 阶段就炸
_NET_ERRORS: tuple[type[BaseException], ...] = (asyncio.TimeoutError, OSError)
if aiohttp is not None:
    _NET_ERRORS = _NET_ERRORS + (aiohttp.ClientError,)

BASE_URL = "https://api.steampowered.com"
BATCH_SIZE = 100

PERSONA_STATE = {
    0: "离线",
    1: "在线",
    2: "忙碌",
    3: "离开",
    4: "打盹",
    5: "想交易",
    6: "想玩游戏",
}

_RETRY_STATUS = (429, 500, 502, 503, 504)


class SteamError(Exception):
    """可以原样展示给用户的错误。"""


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _is_steamid64(value: str) -> bool:
    return value.isdigit() and len(value) == 17 and value.startswith("7656")


class SteamClient:
    """极简 Steam API 封装，生命周期由插件管理。"""

    def __init__(
        self,
        api_key: str = "",
        *,
        proxy: str = "",
        timeout: int = 12,
        retries: int = 3,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._proxy = (proxy or "").strip() or None
        self._timeout = max(3, timeout)
        self._retries = max(0, retries)
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------ #
    # 底层
    # ------------------------------------------------------------------ #

    def set_api_key(self, key: str) -> None:
        self._api_key = (key or "").strip()

    async def _sess(self) -> aiohttp.ClientSession:
        if aiohttp is None:
            raise SteamError("缺少依赖 aiohttp，请在 AstrBot 环境中运行本插件")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )
        return self._session

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._api_key:
            raise SteamError("没有配置 Steam Web API Key，请先在插件配置里填写")
        payload = dict(params)
        payload["key"] = self._api_key
        payload["format"] = "json"

        last = "未知错误"
        for attempt in range(self._retries + 1):
            try:
                session = await self._sess()
                async with session.get(
                    f"{BASE_URL}{path}", params=payload, proxy=self._proxy
                ) as resp:
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    if resp.status in (401, 403):
                        raise SteamError(f"API Key 无效或没有该接口权限（HTTP {resp.status}）")
                    if resp.status == 404:
                        raise SteamError("Steam 接口地址不存在（HTTP 404）")
                    last = f"HTTP {resp.status}"
                    if resp.status not in _RETRY_STATUS:
                        raise SteamError(f"Steam 接口返回 {last}")
            except SteamError:
                raise
            except _NET_ERRORS as exc:
                last = f"{type(exc).__name__}: {exc}"
                logger.debug(f"[Steam] {path} 第 {attempt + 1} 次请求失败：{last}")

            if attempt < self._retries:
                await asyncio.sleep(1.2 * (2**attempt))

        raise SteamError(f"访问 Steam 失败（{last}），检查代理或稍后再试")

    # ------------------------------------------------------------------ #
    # 业务接口
    # ------------------------------------------------------------------ #

    async def resolve_vanity(self, vanity: str) -> str:
        """自定义主页名 → SteamID64。"""
        vanity = vanity.strip().rstrip("/")
        data = await self._get("/ISteamUser/ResolveVanityURL/v0001/", {"vanityurl": vanity})
        body = data.get("response") or {}
        if body.get("success") != 1 or not body.get("steamid"):
            raise SteamError(f"解析不到「{vanity}」对应的 Steam 账号（自定义名写错或已改名）")
        return str(body["steamid"])

    async def resolve_id(self, raw: str) -> str:
        """把用户给的任意形式解析成 SteamID64。

        支持 17 位 SteamID64、``/profiles/<id>``、``/id/<vanity>``、纯自定义名。
        """
        text = (raw or "").strip()
        if not text:
            raise SteamError("没有给出 Steam 账号")

        if _is_steamid64(text):
            return text

        if "/profiles/" in text:
            tail = text.split("/profiles/", 1)[1].split("/")[0].split("?")[0]
            if _is_steamid64(tail):
                return tail

        if "/id/" in text:
            tail = text.split("/id/", 1)[1].split("/")[0].split("?")[0]
            if tail:
                return await self.resolve_vanity(tail)

        if text.isdigit() and len(text) <= 12:
            raise SteamError("这是 SteamID32/账号 ID，不是 SteamID64。请用 17 位 ID 或主页链接")

        return await self.resolve_vanity(text)

    async def friends(self, steamid: str) -> list[dict[str, Any]]:
        """好友名单。资料私密的好友不会出现在返回里。"""
        data = await self._get(
            "/ISteamUser/GetFriendList/v0001/",
            {"steamid": steamid, "relationship": "friend"},
        )
        friends = (data.get("friendslist") or {}).get("friends") or []
        return [f for f in friends if f.get("steamid")]

    async def summaries(self, steamids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """批量玩家摘要，按 100 个一批。返回 ``{steamid: player}``。"""
        ids = [str(s) for s in steamids if s]
        result: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(ids, BATCH_SIZE):
            data = await self._get(
                "/ISteamUser/GetPlayerSummaries/v0002/",
                {"steamids": ",".join(chunk)},
            )
            for player in (data.get("response") or {}).get("players") or []:
                sid = str(player.get("steamid") or "")
                if sid:
                    result[sid] = player
        return result

    async def owned_games(self, steamid: str) -> list[dict[str, Any]]:
        """全部已购游戏，含 ``playtime_forever`` / ``rtime_last_played``。"""
        data = await self._get(
            "/IPlayerService/GetOwnedGames/v0001/",
            {
                "steamid": steamid,
                "include_appinfo": 1,
                "include_played_free_games": 1,
            },
        )
        games = list((data.get("response") or {}).get("games") or [])
        games.sort(key=lambda g: g.get("playtime_forever", 0), reverse=True)
        return games

    async def recent_games(self, steamid: str, count: int = 5) -> list[dict[str, Any]]:
        """近两周游玩，含 ``playtime_2weeks``。"""
        data = await self._get(
            "/IPlayerService/GetRecentlyPlayedGames/v0001/",
            {"steamid": steamid, "count": max(1, count)},
        )
        return list((data.get("response") or {}).get("games") or [])

    async def bans(self, steamids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """VAC / 游戏封禁。"""
        ids = [str(s) for s in steamids if s]
        result: dict[str, dict[str, Any]] = {}
        for chunk in _chunks(ids, BATCH_SIZE):
            data = await self._get("/ISteamUser/GetPlayerBans/v1/", {"steamids": ",".join(chunk)})
            for player in data.get("players") or []:
                sid = str(player.get("SteamId") or "")
                if sid:
                    result[sid] = player
        return result

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


def state_text(personastate: Any) -> str:
    try:
        return PERSONA_STATE.get(int(personastate), "未知")
    except (TypeError, ValueError):
        return "未知"
