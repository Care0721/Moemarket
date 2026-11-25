import asyncio
import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register


@register(
    "mengzhai_market",
    "care",
    "监听萌宅角色卡市场新卡并推送（支持图片，签名参数用户自填）",
    "1.1.1",
)
class MengzhaiMarketPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path("data/plugin_data/mengzhai_market")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.data_dir / "images"
        self.cache_dir.mkdir(exist_ok=True)
        self.seen_file = self.data_dir / "seen_ids.json"
        self.token_file = self.data_dir / "token.json"

        self.seen_ids: Set[str] = self._load_seen()
        self.token: Optional[str] = None
        self.token_expire: int = 0
        self._load_token()

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None

        if self.config.get("enable_on_start", True):
            self.start_monitor()

    # ---------- 配置读取（全部由用户填写，无硬编码默认值） ----------
    @property
    def api_base(self) -> str:
        return (self.config.get("api_base") or "").rstrip("/")

    @property
    def sign_secret(self) -> bytes:
        return (self.config.get("sign_secret") or "").encode()

    @property
    def app_pkg(self) -> str:
        return self.config.get("app_pkg") or ""

    @property
    def app_ver(self) -> str:
        return self.config.get("app_ver") or ""

    def _check_config(self) -> Optional[str]:
        """返回错误信息，通过则返回 None"""
        if not self.api_base:
            return "未配置 API 地址 (api_base)"
        if not self.sign_secret:
            return "未配置签名密钥 (sign_secret)"
        if not self.app_pkg:
            return "未配置 X-App-Pkg (app_pkg)"
        if not self.app_ver:
            return "未配置 X-App-Ver (app_ver)"
        account = (self.config.get("account") or "").strip()
        password = (self.config.get("password") or "").strip()
        if not account or not password:
            return "未配置账号或密码"
        return None

    # ---------- 持久化 ----------
    def _load_seen(self) -> Set[str]:
        if self.seen_file.exists():
            try:
                data = json.loads(self.seen_file.read_text(encoding="utf-8"))
                return set(data) if isinstance(data, list) else set()
            except Exception:
                pass
        return set()

    def _save_seen(self):
        ids = list(self.seen_ids)
        if len(ids) > 5000:
            ids = ids[-5000:]
            self.seen_ids = set(ids)
        self.seen_file.write_text(json.dumps(ids, ensure_ascii=False), encoding="utf-8")

    def _load_token(self):
        if self.token_file.exists():
            try:
                data = json.loads(self.token_file.read_text(encoding="utf-8"))
                self.token = data.get("token")
                exp = int(data.get("expiresAt", 0))
                self.token_expire = exp // 1000 if exp > 1e12 else exp
            except Exception:
                pass

    def _save_token(self, token: str, expires_at: int):
        self.token = token
        self.token_expire = expires_at // 1000 if expires_at > 1e12 else expires_at
        self.token_file.write_text(
            json.dumps({"token": token, "expiresAt": expires_at}, ensure_ascii=False),
            encoding="utf-8",
        )

    # ---------- 签名头 ----------
    def _make_headers(self, method: str, path: str, token: Optional[str] = None) -> Dict[str, str]:
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex
        zeros = "0" * 64
        pure_path = path.split("?")[0]
        data = "\n".join([
            self.app_pkg,
            zeros,
            self.app_ver,
            ts,
            nonce,
            method.upper(),
            pure_path,
        ])
        auth = hmac.new(self.sign_secret, data.encode(), hashlib.sha256).hexdigest()
        headers = {
            "User-Agent": "Dart/3.5 (dart:io)",
            "X-App-Pkg": self.app_pkg,
            "X-App-Sig": zeros,
            "X-App-Ver": self.app_ver,
            "X-App-Ts": ts,
            "X-App-Nonce": nonce,
            "X-App-Auth": auth,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    # ---------- HTTP ----------
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )
        return self._session

    async def _login(self) -> bool:
        err = self._check_config()
        if err:
            logger.warning(f"[萌宅市场] {err}")
            return False

        path = "/api/auth/login"
        headers = self._make_headers("POST", path)
        payload = {
            "account": (self.config.get("account") or "").strip(),
            "password": (self.config.get("password") or "").strip(),
        }

        try:
            session = await self._get_session()
            async with session.post(self.api_base + path, headers=headers, json=payload) as resp:
                data = await resp.json(content_type=None)
                if data.get("success") and data.get("token"):
                    self._save_token(data["token"], data.get("expiresAt", 0))
                    logger.info("[萌宅市场] 登录成功")
                    return True
                logger.warning(f"[萌宅市场] 登录失败: {data.get('error') or data}")
                return False
        except Exception as e:
            logger.error(f"[萌宅市场] 登录异常: {e}")
            return False

    async def _ensure_token(self) -> bool:
        now = int(time.time())
        if self.token and self.token_expire - now > 3600:
            return True
        return await self._login()

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        retry: bool = True,
    ) -> Optional[dict]:
        if not await self._ensure_token():
            return None
        headers = self._make_headers(method, path, self.token)
        try:
            session = await self._get_session()
            async with session.request(
                method, self.api_base + path, headers=headers, params=params
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 401 and retry:
                    if await self._login():
                        return await self._request(method, path, params, retry=False)
                return data
        except Exception as e:
            logger.error(f"[萌宅市场] 请求异常 {path}: {e}")
            return None

    async def _fetch_market(self, limit: int = 30) -> List[Dict[str, Any]]:
        data = await self._request(
            "GET",
            "/api/ai/market",
            {"limit": limit, "offset": 0, "sort": "newest"},
        )
        if not data or not data.get("success"):
            if data:
                logger.warning(f"[萌宅市场] 拉取失败: {data.get('error') or data}")
            return []
        return data.get("cards") or []

    # ---------- 图片 ----------
    async def _download_image(self, url: str, card_id: str) -> Optional[Path]:
        if not url:
            return None
        lower = url.lower()
        if ".jpg" in lower or ".jpeg" in lower:
            ext = ".jpg"
        elif ".webp" in lower:
            ext = ".webp"
        elif ".gif" in lower:
            ext = ".gif"
        else:
            ext = ".png"
        path = self.cache_dir / f"{card_id}{ext}"
        if path.exists() and path.stat().st_size > 512:
            return path
        try:
            session = await self._get_session()
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                content = await resp.read()
                if len(content) < 512:
                    return None
                path.write_bytes(content)
                return path
        except Exception as e:
            logger.warning(f"[萌宅市场] 下载图片失败: {e}")
            return None

    def _pick_image_url(self, card: Dict[str, Any]) -> str:
        prefer_avatar = self.config.get("prefer_avatar", True)
        avatar = (card.get("avatarUrl") or "").strip()
        bg = (card.get("backgroundUrl") or card.get("previewUrl") or "").strip()
        return (avatar or bg) if prefer_avatar else (bg or avatar)

    # ---------- 推送 ----------
    def _format_text(self, card: Dict[str, Any]) -> str:
        tpl = self.config.get("message_template") or (
            "🆕 萌宅新角色卡\n名称：{name}\n作者：{author}\n分类：{category}\n简介：{summary}\n导入数：{imports}"
        )
        summary = (card.get("summary") or "")[:180]
        try:
            return tpl.format(
                name=card.get("name") or "未知",
                author=card.get("authorName") or "未知",
                category=card.get("category") or "其他",
                summary=summary,
                imports=card.get("imports") or 0,
                id=card.get("id") or "",
                rating=card.get("rating") or 0,
            )
        except Exception:
            return f"🆕 {card.get('name') or '新角色卡'}\n{summary}"

    async def _build_chain(self, card: Dict[str, Any]) -> MessageChain:
        text = self._format_text(card)
        components = [Plain(text)]

        if self.config.get("push_image", True):
            img_url = self._pick_image_url(card)
            if img_url:
                local = await self._download_image(
                    img_url, str(card.get("id") or uuid.uuid4().hex)
                )
                if local and local.exists():
                    components.append(Image.fromFileSystem(str(local)))
                else:
                    try:
                        components.append(Image.fromURL(img_url))
                    except Exception:
                        pass

        return MessageChain(components)

    async def _push_cards(self, cards: List[Dict[str, Any]]):
        targets = self.config.get("push_targets") or []
        if not targets:
            logger.info("[萌宅市场] 有新卡但未配置 push_targets")
            return

        for card in cards:
            chain = await self._build_chain(card)
            for target in targets:
                try:
                    await self.context.send_message(target, chain)
                    logger.info(f"[萌宅市场] 已推送「{card.get('name')}」-> {target}")
                except Exception as e:
                    logger.error(f"[萌宅市场] 推送失败 {target}: {e}")

    # ---------- 轮询 ----------
    async def _poll_once(self):
        cards = await self._fetch_market(limit=40)
        if not cards:
            return

        allow_cats = {
            c.strip() for c in (self.config.get("categories") or []) if c and c.strip()
        }
        new_cards = []
        for c in cards:
            cid = str(c.get("id") or "")
            if not cid or cid in self.seen_ids:
                continue
            cat = (c.get("category") or "").strip()
            if allow_cats and cat not in allow_cats:
                self.seen_ids.add(cid)
                continue
            self.seen_ids.add(cid)
            new_cards.append(c)

        if not new_cards:
            return

        self._save_seen()
        max_n = max(1, int(self.config.get("max_new_per_push") or 5))
        await self._push_cards(new_cards[:max_n])

    async def _monitor_loop(self):
        interval = max(60, int(self.config.get("poll_interval") or 300))
        logger.info(f"[萌宅市场] 监听启动，间隔 {interval}s")

        # 启动时记录当前已有卡片，避免首次刷屏
        try:
            cards = await self._fetch_market(limit=50)
            for c in cards:
                cid = str(c.get("id") or "")
                if cid:
                    self.seen_ids.add(cid)
            self._save_seen()
            logger.info(f"[萌宅市场] 初始化已记录 {len(self.seen_ids)} 张卡")
        except Exception as e:
            logger.warning(f"[萌宅市场] 初始化 seen 失败: {e}")

        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[萌宅市场] 轮询异常: {e}")
            await asyncio.sleep(interval)

    def start_monitor(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

    def stop_monitor(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        logger.info("[萌宅市场] 监听已停止")

    # ---------- 命令 ----------
    @filter.command("萌宅市场状态")
    async def cmd_status(self, event: AstrMessageEvent):
        err = self._check_config()
        cfg_status = err if err else "配置完整"
        status = "运行中" if self._running else "已停止"
        now = int(time.time())
        if self.token and self.token_expire > now:
            left = (self.token_expire - now) // 3600
            token_status = f"有效（约剩{left}小时）"
        else:
            token_status = "无效/未登录"
        yield event.plain_result(
            "萌宅角色卡市场监听\n"
            f"配置：{cfg_status}\n"
            f"状态：{status}\n"
            f"Token：{token_status}\n"
            f"已记录卡片：{len(self.seen_ids)}\n"
            f"轮询间隔：{self.config.get('poll_interval', 300)}s\n"
            f"推送目标：{len(self.config.get('push_targets') or [])} 个"
        )

    @filter.command("萌宅市场开启")
    async def cmd_start(self, event: AstrMessageEvent):
        err = self._check_config()
        if err:
            yield event.plain_result(f"无法开启：{err}")
            return
        self.start_monitor()
        yield event.plain_result("已开启萌宅市场监听")

    @filter.command("萌宅市场关闭")
    async def cmd_stop(self, event: AstrMessageEvent):
        self.stop_monitor()
        yield event.plain_result("已关闭萌宅市场监听")

    @filter.command("萌宅市场登录")
    async def cmd_login(self, event: AstrMessageEvent):
        ok = await self._login()
        yield event.plain_result("登录成功" if ok else "登录失败，请检查 WebUI 配置")

    @filter.command("萌宅市场拉取")
    async def cmd_fetch(self, event: AstrMessageEvent):
        err = self._check_config()
        if err:
            yield event.plain_result(f"无法拉取：{err}")
            return
        yield event.plain_result("正在拉取最新角色卡…")
        cards = await self._fetch_market(limit=8)
        if not cards:
            yield event.plain_result("拉取失败或无数据，请先确认登录")
            return
        lines = [f"最近 {len(cards)} 张："]
        for i, c in enumerate(cards, 1):
            lines.append(
                f"{i}. {c.get('name')} | {c.get('category')} | "
                f"作者:{c.get('authorName')} | 导入:{c.get('imports')}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("萌宅市场测试推送")
    async def cmd_test_push(self, event: AstrMessageEvent):
        err = self._check_config()
        if err:
            yield event.plain_result(f"无法推送：{err}")
            return
        cards = await self._fetch_market(limit=1)
        if not cards:
            yield event.plain_result("没有可测试的卡片")
            return
        await self._push_cards(cards[:1])
        yield event.plain_result("已尝试推送最新一张到配置的目标会话")

    async def terminate(self):
        self.stop_monitor()
        if self._session and not self._session.closed:
            await self._session.close()