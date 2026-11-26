import asyncio
import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register


@register(
    "mengzhai_market",
    "care",
    "萌宅角色卡市场监听推送（全链路重试/Markdown/图片兜底）",
    "1.3.0",
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
        self._login_lock = asyncio.Lock()
        self._last_error: str = ""

        if self.config.get("enable_on_start", True):
            self.start_monitor()

    # =========================
    # 配置
    # =========================
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

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default) or default)
        except Exception:
            return default

    def _check_config(self) -> Optional[str]:
        if not self.api_base:
            return "未配置 API 地址 (api_base)"
        if not self.sign_secret:
            return "未配置签名密钥 (sign_secret)"
        if not self.app_pkg:
            return "未配置 X-App-Pkg (app_pkg)"
        if not self.app_ver:
            return "未配置 X-App-Ver (app_ver)"
        if not (self.config.get("account") or "").strip():
            return "未配置账号"
        if not (self.config.get("password") or "").strip():
            return "未配置密码"
        return None

    # =========================
    # 持久化
    # =========================
    def _load_seen(self) -> Set[str]:
        if not self.seen_file.exists():
            return set()
        try:
            data = json.loads(self.seen_file.read_text(encoding="utf-8"))
            return set(data) if isinstance(data, list) else set()
        except Exception as e:
            logger.warning(f"[萌宅市场] 读取 seen 失败: {e}")
            return set()

    def _save_seen(self) -> None:
        ids = list(self.seen_ids)
        if len(ids) > 8000:
            ids = ids[-5000:]
            self.seen_ids = set(ids)
        try:
            self.seen_file.write_text(
                json.dumps(ids, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[萌宅市场] 保存 seen 失败: {e}")

    def _load_token(self) -> None:
        if not self.token_file.exists():
            return
        try:
            data = json.loads(self.token_file.read_text(encoding="utf-8"))
            self.token = data.get("token")
            exp = int(data.get("expiresAt", 0) or 0)
            self.token_expire = exp // 1000 if exp > 1e12 else exp
        except Exception as e:
            logger.warning(f"[萌宅市场] 读取 token 失败: {e}")

    def _save_token(self, token: str, expires_at: int) -> None:
        self.token = token
        self.token_expire = expires_at // 1000 if expires_at > 1e12 else expires_at
        try:
            self.token_file.write_text(
                json.dumps(
                    {"token": token, "expiresAt": expires_at},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[萌宅市场] 保存 token 失败: {e}")

    # =========================
    # 签名
    # =========================
    def _make_headers(
        self, method: str, path: str, token: Optional[str] = None
    ) -> Dict[str, str]:
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex
        zeros = "0" * 64
        pure_path = path.split("?")[0]
        raw = "\n".join(
            [
                self.app_pkg,
                zeros,
                self.app_ver,
                ts,
                nonce,
                method.upper(),
                pure_path,
            ]
        )
        auth = hmac.new(self.sign_secret, raw.encode(), hashlib.sha256).hexdigest()
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

    # =========================
    # HTTP / 重试
    # =========================
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=25)
            )
        return self._session

    async def _backoff(self, attempt: int, base: float = 2.0) -> None:
        delay = min(float(base) * (2 ** max(0, attempt - 1)), 20.0)
        await asyncio.sleep(delay)

    async def _login(self) -> bool:
        err = self._check_config()
        if err:
            self._last_error = err
            logger.warning(f"[萌宅市场] {err}")
            return False

        async with self._login_lock:
            now = int(time.time())
            if self.token and self.token_expire - now > 3600:
                return True

            path = "/api/auth/login"
            payload = {
                "account": (self.config.get("account") or "").strip(),
                "password": (self.config.get("password") or "").strip(),
            }
            retries = max(1, self._cfg_int("request_retry_times", 3))
            base_delay = max(1, self._cfg_int("request_retry_delay", 2))

            for attempt in range(1, retries + 1):
                try:
                    headers = self._make_headers("POST", path)
                    session = await self._get_session()
                    async with session.post(
                        self.api_base + path, headers=headers, json=payload
                    ) as resp:
                        data = await resp.json(content_type=None)
                        if data.get("success") and data.get("token"):
                            self._save_token(
                                data["token"], int(data.get("expiresAt", 0) or 0)
                            )
                            self._last_error = ""
                            logger.info("[萌宅市场] 登录成功")
                            return True
                        msg = str(data.get("error") or data)
                        self._last_error = f"登录失败: {msg}"
                        logger.warning(
                            f"[萌宅市场] 登录失败({attempt}/{retries}): {msg}"
                        )
                except Exception as e:
                    self._last_error = f"登录异常: {e}"
                    logger.warning(
                        f"[萌宅市场] 登录异常({attempt}/{retries}): {e}"
                    )
                if attempt < retries:
                    await self._backoff(attempt, base_delay)
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
        allow_relogin: bool = True,
    ) -> Optional[dict]:
        if not await self._ensure_token():
            return None

        retries = max(1, self._cfg_int("request_retry_times", 3))
        base_delay = max(1, self._cfg_int("request_retry_delay", 2))

        for attempt in range(1, retries + 1):
            try:
                headers = self._make_headers(method, path, self.token)
                session = await self._get_session()
                async with session.request(
                    method,
                    self.api_base + path,
                    headers=headers,
                    params=params,
                ) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status == 401 and allow_relogin:
                        if await self._login():
                            return await self._request(
                                method, path, params, allow_relogin=False
                            )
                        return data
                    if resp.status >= 500:
                        raise RuntimeError(f"HTTP {resp.status}: {data}")
                    self._last_error = ""
                    return data
            except Exception as e:
                self._last_error = f"请求异常 {path}: {e}"
                logger.warning(
                    f"[萌宅市场] 请求异常 {path} ({attempt}/{retries}): {e}"
                )
                if attempt < retries:
                    await self._backoff(attempt, base_delay)
        return None

    async def _fetch_market(self, limit: int = 30) -> List[Dict[str, Any]]:
        sort = (self.config.get("sort") or "newest").strip() or "newest"
        data = await self._request(
            "GET",
            "/api/ai/market",
            {"limit": int(limit), "offset": 0, "sort": sort},
        )
        if not data:
            return []
        if not data.get("success"):
            self._last_error = str(data.get("error") or data)
            logger.warning(f"[萌宅市场] 拉取失败: {self._last_error}")
            return []
        cards = data.get("cards") or []
        return cards if isinstance(cards, list) else []

    # =========================
    # 过滤
    # =========================
    def _match_card(self, card: Dict[str, Any]) -> bool:
        allow_cats = {
            str(c).strip()
            for c in (self.config.get("categories") or [])
            if c and str(c).strip()
        }
        cat = (card.get("category") or "").strip()
        if allow_cats and cat not in allow_cats:
            return False

        min_imports = self._cfg_int("min_imports", 0)
        if min_imports > 0 and int(card.get("imports") or 0) < min_imports:
            return False

        text = f"{card.get('name') or ''} {card.get('summary') or ''}".lower()
        includes = [
            str(x).strip().lower()
            for x in (self.config.get("keyword_include") or [])
            if x and str(x).strip()
        ]
        excludes = [
            str(x).strip().lower()
            for x in (self.config.get("keyword_exclude") or [])
            if x and str(x).strip()
        ]
        if includes and not any(k in text for k in includes):
            return False
        if excludes and any(k in text for k in excludes):
            return False
        return True

    # =========================
    # 图片
    # =========================
    def _pick_image_url(self, card: Dict[str, Any]) -> str:
        prefer_avatar = bool(self.config.get("prefer_avatar", True))
        avatar = (card.get("avatarUrl") or "").strip()
        bg = (
            card.get("backgroundUrl")
            or card.get("previewUrl")
            or ""
        ).strip()
        return (avatar or bg) if prefer_avatar else (bg or avatar)

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

        retries = max(1, self._cfg_int("request_retry_times", 3))
        for attempt in range(1, retries + 1):
            try:
                session = await self._get_session()
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    content = await resp.read()
                    if len(content) < 512:
                        return None
                    path.write_bytes(content)
                    return path
            except Exception as e:
                logger.warning(
                    f"[萌宅市场] 下载图片失败({attempt}/{retries}): {e}"
                )
                if attempt < retries:
                    await self._backoff(attempt, 1.5)
        return None

    # =========================
    # 消息构建（加强 + 兜底）
    # =========================
    def _safe_format(self, tpl: str, card: Dict[str, Any], markdown: bool) -> str:
        summary = (card.get("summary") or "暂无简介").strip()
        if len(summary) > 220:
            summary = summary[:220] + "…"
        values = {
            "name": card.get("name") or "未知角色",
            "author": card.get("authorName") or "未知作者",
            "category": card.get("category") or "其他",
            "summary": summary,
            "imports": card.get("imports") or 0,
            "id": card.get("id") or "",
            "rating": card.get("rating") or 0,
        }
        try:
            return tpl.format(**values)
        except Exception:
            if markdown:
                return (
                    f"## 🆕 {values['name']}\n\n"
                    f"- 作者：{values['author']}\n"
                    f"- 分类：{values['category']}\n"
                    f"- 导入：{values['imports']}\n\n"
                    f"> {values['summary']}"
                )
            return (
                f"🆕 {values['name']}\n"
                f"作者：{values['author']}\n"
                f"分类：{values['category']}\n"
                f"导入：{values['imports']}\n"
                f"简介：{values['summary']}"
            )

    def _build_text(self, card: Dict[str, Any], markdown: bool) -> str:
        if markdown:
            tpl = self.config.get("message_template") or (
                "## 🆕 萌宅新角色卡\n\n"
                "**{name}**\n\n"
                "- 作者：{author}\n"
                "- 分类：{category}\n"
                "- 导入：{imports}\n"
                "- 评分：{rating}\n\n"
                "> {summary}\n\n"
                "***\n"
                "ID：`{id}`"
            )
            return self._safe_format(tpl, card, True)

        tpl = self.config.get("plain_message_template") or (
            "🆕 萌宅新角色卡\n"
            "名称：{name}\n"
            "作者：{author}\n"
            "分类：{category}\n"
            "导入：{imports}\n"
            "评分：{rating}\n"
            "简介：{summary}\n"
            "ID：{id}"
        )
        return self._safe_format(tpl, card, False)

    async def _build_chain(
        self, card: Dict[str, Any], force_plain: bool = False
    ) -> MessageChain:
        use_md = (not force_plain) and bool(self.config.get("use_markdown", True))
        text = self._build_text(card, markdown=use_md)
        components: List[Any] = [Plain(text)]

        if self.config.get("push_image", True):
            img_url = self._pick_image_url(card)
            if img_url:
                local = await self._download_image(
                    img_url, str(card.get("id") or uuid.uuid4().hex)
                )
                if local and local.exists():
                    components.append(Image.fromFileSystem(str(local)))
                else:
                    # URL 兜底
                    try:
                        components.append(Image.fromURL(img_url))
                    except Exception:
                        pass

        chain = MessageChain(components)
        if use_md:
            try:
                if hasattr(chain, "use_markdown"):
                    chain = chain.use_markdown(True)
            except Exception:
                # 不支持则忽略，按普通消息发
                pass
        return chain

    async def _send_with_retry(self, target: str, card: Dict[str, Any]) -> bool:
        """推送重试：先 Markdown，失败再纯文本兜底。"""
        retries = max(1, self._cfg_int("push_retry_times", 2))
        last_err = None

        # 第一轮：按配置（可能 Markdown）
        chain = await self._build_chain(card, force_plain=False)
        for attempt in range(1, retries + 1):
            try:
                await self.context.send_message(target, chain)
                return True
            except Exception as e:
                last_err = e
                logger.warning(
                    f"[萌宅市场] 推送失败 {target} ({attempt}/{retries}): {e}"
                )
                if attempt < retries:
                    await self._backoff(attempt, 1.5)

        # 第二轮兜底：强制纯文本 + 尽量带图
        try:
            plain_chain = await self._build_chain(card, force_plain=True)
            await self.context.send_message(target, plain_chain)
            logger.info(f"[萌宅市场] 纯文本兜底推送成功 -> {target}")
            return True
        except Exception as e:
            last_err = e
            logger.error(f"[萌宅市场] 兜底推送仍失败 {target}: {e}")

        self._last_error = f"推送失败: {last_err}"
        return False

    async def _push_cards(self, cards: List[Dict[str, Any]]) -> None:
        targets = self.config.get("push_targets") or []
        if not targets:
            logger.info("[萌宅市场] 有新卡但未配置 push_targets")
            return

        for card in cards:
            name = card.get("name") or "未知"
            for target in targets:
                ok = await self._send_with_retry(target, card)
                if ok:
                    logger.info(f"[萌宅市场] 已推送「{name}」-> {target}")
                else:
                    logger.error(f"[萌宅市场] 推送最终失败「{name}」-> {target}")

    # =========================
    # 轮询
    # =========================
    async def _poll_once(self) -> None:
        cards = await self._fetch_market(limit=40)
        if not cards:
            return

        new_cards: List[Dict[str, Any]] = []
        for c in cards:
            cid = str(c.get("id") or "")
            if not cid or cid in self.seen_ids:
                continue
            if not self._match_card(c):
                self.seen_ids.add(cid)
                continue
            self.seen_ids.add(cid)
            new_cards.append(c)

        if not new_cards:
            return

        self._save_seen()
        max_n = max(1, self._cfg_int("max_new_per_push", 5))
        await self._push_cards(new_cards[:max_n])

    async def _monitor_loop(self) -> None:
        interval = max(60, self._cfg_int("poll_interval", 300))
        logger.info(f"[萌宅市场] 监听启动，间隔 {interval}s")

        if self.config.get("init_seen_on_start", True):
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
                self._last_error = str(e)
                logger.error(f"[萌宅市场] 轮询异常: {e}")
            await asyncio.sleep(interval)

    def start_monitor(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

    def stop_monitor(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        logger.info("[萌宅市场] 监听已停止")

    # =========================
    # 命令
    # =========================
    @filter.command("萌宅市场状态")
    async def cmd_status(self, event: AstrMessageEvent):
        err = self._check_config()
        cfg = err if err else "配置完整"
        status = "运行中" if self._running else "已停止"
        now = int(time.time())
        if self.token and self.token_expire > now:
            left_h = max(0, (self.token_expire - now) // 3600)
            token_status = f"有效（约剩{left_h}小时）"
        else:
            token_status = "无效/未登录"
        md = "开" if self.config.get("use_markdown", True) else "关"
        last = self._last_error or "无"
        yield event.plain_result(
            "萌宅角色卡市场监听\n"
            f"配置：{cfg}\n"
            f"状态：{status}\n"
            f"Token：{token_status}\n"
            f"Markdown：{md}\n"
            f"已记录卡片：{len(self.seen_ids)}\n"
            f"轮询间隔：{self._cfg_int('poll_interval', 300)}s\n"
            f"推送目标：{len(self.config.get('push_targets') or [])} 个\n"
            f"最近错误：{last}"
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
        if ok:
            yield event.plain_result("登录成功")
        else:
            yield event.plain_result(
                f"登录失败：{self._last_error or '请检查 WebUI 配置'}"
            )

    @filter.command("萌宅市场拉取")
    async def cmd_fetch(self, event: AstrMessageEvent):
        err = self._check_config()
        if err:
            yield event.plain_result(f"无法拉取：{err}")
            return
        yield event.plain_result("正在拉取最新角色卡…")
        cards = await self._fetch_market(limit=8)
        if not cards:
            yield event.plain_result(
                f"拉取失败或无数据。{self._last_error or ''}".strip()
            )
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
        cards = await self._fetch_market(limit=5)
        if not cards:
            yield event.plain_result(
                f"没有可测试的卡片。{self._last_error or ''}".strip()
            )
            return
        target_card = None
        for c in cards:
            if self._match_card(c):
                target_card = c
                break
        if not target_card:
            target_card = cards[0]
        await self._push_cards([target_card])
        yield event.plain_result(
            f"已尝试推送「{target_card.get('name')}」到配置的目标会话"
        )

    @filter.command("萌宅市场清空记录")
    async def cmd_clear_seen(self, event: AstrMessageEvent):
        self.seen_ids.clear()
        self._save_seen()
        yield event.plain_result(
            "已清空本地已推送记录。下次轮询可能把当前市场上的卡当作新卡推送。"
        )

    async def terminate(self):
        self.stop_monitor()
        if self._session and not self._session.closed:
            await self._session.close()