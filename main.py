from __future__ import annotations

import asyncio
import inspect
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover - only used on old AstrBot versions.
    get_astrbot_data_path = None


PLUGIN_NAME = "astrbot_plugin_i_aint_no_robot"

AI_SMELL_WORDS = (
    "作为ai",
    "作为一个ai",
    "作为人工智能",
    "我是ai",
    "我是人工智能",
    "根据上下文",
    "根据聊天记录",
    "综上",
    "总之",
    "此外",
    "首先",
    "其次",
    "最后",
    "值得注意",
    "深入探讨",
    "从多个角度",
    "希望这",
    "请告诉我",
    "如果你需要",
    "我可以帮",
    "很好的问题",
    "您",
)

MARKDOWN_OR_FORMAT = re.compile(r"(^[-*#>]\s)|(```)|(\*\*)|(\d+[.)、]\s)")
EMOJI_PATTERN = re.compile(
    "["
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff"
    "]+"
)


@dataclass
class GroupState:
    group_id: str
    unified_msg_origin: str
    enabled: bool
    last_seen_at: int
    last_spoke_at: int
    next_attempt_at: int
    last_summarized_at: int
    style_summary: str
    topic_summary: str


class MemoryStore:
    def __init__(self, db_path: Path, recent_limit: int):
        self.db_path = db_path
        self.recent_limit = max(20, int(recent_limit))

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists group_state (
                    group_id text primary key,
                    unified_msg_origin text not null,
                    enabled integer not null default 1,
                    first_seen_at integer not null,
                    last_seen_at integer not null,
                    last_spoke_at integer not null default 0,
                    next_attempt_at integer not null default 0,
                    last_summarized_at integer not null default 0,
                    style_summary text not null default '',
                    topic_summary text not null default ''
                )
                """
            )
            conn.execute(
                """
                create table if not exists recent_messages (
                    id integer primary key autoincrement,
                    group_id text not null,
                    sender_id text not null,
                    sender_name text not null,
                    text text not null,
                    created_at integer not null,
                    is_bot integer not null default 0
                )
                """
            )
            conn.execute(
                "create index if not exists idx_recent_group_time on recent_messages(group_id, created_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_group(self, group_id: str, umo: str, enabled: bool = True) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                insert into group_state (
                    group_id, unified_msg_origin, enabled, first_seen_at, last_seen_at
                ) values (?, ?, ?, ?, ?)
                on conflict(group_id) do update set
                    unified_msg_origin = excluded.unified_msg_origin,
                    last_seen_at = excluded.last_seen_at
                """,
                (group_id, umo, 1 if enabled else 0, now, now),
            )

    def append_message(
        self,
        group_id: str,
        sender_id: str,
        sender_name: str,
        text: str,
        is_bot: bool = False,
    ) -> None:
        clean_text = compact_text(text, 300)
        if not clean_text:
            return
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                insert into recent_messages (
                    group_id, sender_id, sender_name, text, created_at, is_bot
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (group_id, sender_id, sender_name, clean_text, now, 1 if is_bot else 0),
            )
            conn.execute(
                "update group_state set last_seen_at = ? where group_id = ?",
                (now, group_id),
            )
            self._trim_group_messages(conn, group_id)

    def _trim_group_messages(self, conn: sqlite3.Connection, group_id: str) -> None:
        conn.execute(
            """
            delete from recent_messages
            where group_id = ?
              and id not in (
                  select id from recent_messages
                  where group_id = ?
                  order by id desc
                  limit ?
              )
            """,
            (group_id, group_id, self.recent_limit),
        )

    def get_state(self, group_id: str) -> GroupState | None:
        with self._connect() as conn:
            row = conn.execute(
                "select * from group_state where group_id = ?",
                (group_id,),
            ).fetchone()
        return self._row_to_state(row) if row else None

    def get_first_group_id(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "select group_id from group_state order by first_seen_at asc limit 1"
            ).fetchone()
        return str(row["group_id"]) if row else None

    def set_enabled(self, group_id: str, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "update group_state set enabled = ? where group_id = ?",
                (1 if enabled else 0, group_id),
            )

    def set_next_attempt(self, group_id: str, next_attempt_at: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "update group_state set next_attempt_at = ? where group_id = ?",
                (next_attempt_at, group_id),
            )

    def mark_spoke(self, group_id: str, text: str) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                update group_state
                set last_spoke_at = ?, last_seen_at = ?
                where group_id = ?
                """,
                (now, now, group_id),
            )
        self.append_message(group_id, "bot", "我", text, is_bot=True)

    def update_summary(self, group_id: str, style_summary: str, topic_summary: str) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                update group_state
                set style_summary = ?, topic_summary = ?, last_summarized_at = ?
                where group_id = ?
                """,
                (
                    compact_text(style_summary, 240),
                    compact_text(topic_summary, 240),
                    now,
                    group_id,
                ),
            )

    def get_recent_messages(self, group_id: str, limit: int) -> list[dict[str, Any]]:
        limit = max(1, int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select sender_id, sender_name, text, created_at, is_bot
                from recent_messages
                where group_id = ?
                order by id desc
                limit ?
                """,
                (group_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def count_recent_human_messages(self, group_id: str, since_ts: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                select count(*) as c from recent_messages
                where group_id = ? and created_at >= ? and is_bot = 0
                """,
                (group_id, since_ts),
            ).fetchone()
        return int(row["c"]) if row else 0

    def clear_group(self, group_id: str) -> None:
        with self._connect() as conn:
            conn.execute("delete from recent_messages where group_id = ?", (group_id,))
            conn.execute(
                """
                update group_state
                set last_spoke_at = 0,
                    next_attempt_at = 0,
                    last_summarized_at = 0,
                    style_summary = '',
                    topic_summary = ''
                where group_id = ?
                """,
                (group_id,),
            )

    def stats(self, group_id: str) -> dict[str, int]:
        with self._connect() as conn:
            msg_count = conn.execute(
                "select count(*) as c from recent_messages where group_id = ?",
                (group_id,),
            ).fetchone()
            bot_count = conn.execute(
                """
                select count(*) as c from recent_messages
                where group_id = ? and is_bot = 1
                """,
                (group_id,),
            ).fetchone()
        return {
            "messages": int(msg_count["c"]) if msg_count else 0,
            "bot_messages": int(bot_count["c"]) if bot_count else 0,
        }

    def _row_to_state(self, row: sqlite3.Row) -> GroupState:
        return GroupState(
            group_id=str(row["group_id"]),
            unified_msg_origin=str(row["unified_msg_origin"]),
            enabled=bool(row["enabled"]),
            last_seen_at=int(row["last_seen_at"]),
            last_spoke_at=int(row["last_spoke_at"]),
            next_attempt_at=int(row["next_attempt_at"]),
            last_summarized_at=int(row["last_summarized_at"]),
            style_summary=str(row["style_summary"] or ""),
            topic_summary=str(row["topic_summary"] or ""),
        )


@register(PLUGIN_NAME, "15185", "单群低频自然插话原型", "0.1.0")
class IAintNoRobot(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.store: MemoryStore | None = None
        self.worker_task: asyncio.Task | None = None
        self.active_group_id = str(self.config.get("target_group_id", "")).strip()

    async def initialize(self) -> None:
        data_dir = self._plugin_data_dir()
        recent_limit = int(self.config.get("recent_message_limit", 200))
        self.store = MemoryStore(data_dir / "memory.sqlite3", recent_limit)
        self.store.initialize()
        if not self.active_group_id:
            self.active_group_id = self.store.get_first_group_id() or ""
        self.worker_task = asyncio.create_task(self._background_loop())
        logger.info(f"{PLUGIN_NAME} initialized, data_dir={data_dir}")

    async def terminate(self) -> None:
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass

    @filter.command_group("iar")
    def iar(self):
        """I Ain't No Robot 管理指令。"""
        pass

    @iar.command("status")
    async def status(self, event: AstrMessageEvent):
        """查看当前原型状态。"""
        group_id = self._event_group_id(event) or self.active_group_id
        if not group_id or not self.store:
            yield event.plain_result("还没观察到群")
            return

        state = self.store.get_state(group_id)
        if not state:
            yield event.plain_result("这个群还没记忆")
            return

        stats = self.store.stats(group_id)
        now = int(time.time())
        yield event.plain_result(
            "\n".join(
                [
                    f"群：{group_id}",
                    f"开关：{'开' if state.enabled else '关'}",
                    f"短期消息：{stats['messages']} 条",
                    f"机器人发过：{stats['bot_messages']} 条",
                    f"上次说话：{human_delta(now - state.last_spoke_at) if state.last_spoke_at else '还没'}",
                    f"下次尝试：{human_delta(max(0, state.next_attempt_at - now))}",
                    f"话题记忆：{state.topic_summary or '还没有'}",
                    f"语气记忆：{state.style_summary or '还没有'}",
                ]
            )
        )

    @iar.command("on")
    async def turn_on(self, event: AstrMessageEvent):
        """开启当前群的自然插话。"""
        group_id = self._event_group_id(event)
        if not group_id or not self.store:
            yield event.plain_result("只能在群里用")
            return
        self._lock_active_group(group_id)
        self.store.ensure_group(group_id, event.unified_msg_origin, True)
        self.store.set_enabled(group_id, True)
        yield event.plain_result("开了")

    @iar.command("off")
    async def turn_off(self, event: AstrMessageEvent):
        """关闭当前群的自然插话。"""
        group_id = self._event_group_id(event) or self.active_group_id
        if not group_id or not self.store:
            yield event.plain_result("还没观察到群")
            return
        self.store.set_enabled(group_id, False)
        yield event.plain_result("关了")

    @iar.command("say")
    async def force_say(self, event: AstrMessageEvent):
        """强制尝试生成一句短句，方便测试。"""
        group_id = self._event_group_id(event)
        if not group_id or not self.store:
            yield event.plain_result("只能在群里用")
            return
        self._lock_active_group(group_id)
        self.store.ensure_group(group_id, event.unified_msg_origin, True)
        text = await self._generate_reply(group_id, force=True)
        if not text:
            yield event.plain_result("这会儿没啥好接的")
            return
        await self._send_to_group(group_id, text)

    @iar.command("summary")
    async def force_summary(self, event: AstrMessageEvent):
        """手动刷新群语境记忆。"""
        group_id = self._event_group_id(event) or self.active_group_id
        if not group_id or not self.store:
            yield event.plain_result("还没观察到群")
            return
        updated = await self._refresh_summary(group_id, force=True)
        state = self.store.get_state(group_id)
        if not updated or not state:
            yield event.plain_result("消息太少，先不总结")
            return
        yield event.plain_result(
            f"话题：{state.topic_summary or '无'}\n语气：{state.style_summary or '无'}"
        )

    @iar.command("reset")
    async def reset_group(self, event: AstrMessageEvent):
        """清空当前群的插件记忆。"""
        group_id = self._event_group_id(event) or self.active_group_id
        if not group_id or not self.store:
            yield event.plain_result("还没观察到群")
            return
        self.store.clear_group(group_id)
        yield event.plain_result("清了")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def observe_group(self, event: AstrMessageEvent):
        """记录单个目标群的短期语境。"""
        if not bool(self.config.get("enabled", True)) or not self.store:
            return

        group_id = self._event_group_id(event)
        if not group_id:
            return

        if self.active_group_id and group_id != self.active_group_id:
            return
        self._lock_active_group(group_id)

        text = compact_text(event.message_str or "", 300)
        if not text or text.startswith("/iar"):
            return

        self.store.ensure_group(group_id, event.unified_msg_origin, True)
        sender_id = safe_call(event, "get_sender_id") or self._message_attr(event, "sender", "user_id") or ""
        sender_name = safe_call(event, "get_sender_name") or sender_id or "有人"
        self.store.append_message(
            group_id=group_id,
            sender_id=str(sender_id),
            sender_name=str(sender_name),
            text=text,
            is_bot=False,
        )

    async def _background_loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"{PLUGIN_NAME} background tick failed: {exc}")

            interval = max(15, int(self.config.get("scan_interval_seconds", 60)))
            await asyncio.sleep(interval)

    async def _tick(self) -> None:
        if not bool(self.config.get("enabled", True)) or not self.store:
            return

        group_id = self.active_group_id or self.store.get_first_group_id()
        if not group_id:
            return
        self.active_group_id = group_id

        await self._maybe_refresh_summary(group_id)

        state = self.store.get_state(group_id)
        if not state or not state.enabled:
            return

        now = int(time.time())
        if now < state.next_attempt_at:
            return

        self._schedule_next_attempt(group_id)
        if not self._local_gate_allows(state):
            return

        probability = float(self.config.get("speak_probability", 0.35))
        if random.random() > max(0.0, min(1.0, probability)):
            return

        text = await self._generate_reply(group_id, force=False)
        if text:
            await self._send_to_group(group_id, text)

    def _local_gate_allows(self, state: GroupState) -> bool:
        assert self.store is not None
        now = int(time.time())

        active_window = int(self.config.get("recent_activity_minutes", 20)) * 60
        if state.last_seen_at < now - active_window:
            return False

        speak_gap = int(self.config.get("min_speak_interval_minutes", 45)) * 60
        if state.last_spoke_at and now - state.last_spoke_at < speak_gap:
            return False

        min_messages = int(self.config.get("min_recent_messages", 6))
        recent_count = self.store.count_recent_human_messages(
            state.group_id,
            now - active_window,
        )
        return recent_count >= min_messages

    async def _maybe_refresh_summary(self, group_id: str) -> None:
        if not self.store:
            return
        state = self.store.get_state(group_id)
        if not state:
            return
        interval = int(self.config.get("summary_interval_minutes", 240)) * 60
        if interval <= 0:
            return
        if int(time.time()) - state.last_summarized_at < interval:
            return
        await self._refresh_summary(group_id, force=False)

    async def _refresh_summary(self, group_id: str, force: bool) -> bool:
        assert self.store is not None
        state = self.store.get_state(group_id)
        if not state:
            return False

        messages = self.store.get_recent_messages(group_id, 80)
        human_messages = [m for m in messages if not int(m["is_bot"])]
        if len(human_messages) < int(self.config.get("min_recent_messages", 6)) and not force:
            return False

        provider_id = await self._provider_id(state.unified_msg_origin)
        if not provider_id:
            return False

        prompt = build_summary_prompt(
            old_style=state.style_summary,
            old_topics=state.topic_summary,
            messages=format_messages(messages[-60:]),
        )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
        except Exception as exc:
            logger.warning(f"{PLUGIN_NAME} summary llm failed: {exc}")
            return False

        style, topics = parse_summary(resp.completion_text or "")
        if not style and not topics:
            return False
        self.store.update_summary(group_id, style, topics)
        return True

    async def _generate_reply(self, group_id: str, force: bool) -> str | None:
        assert self.store is not None
        state = self.store.get_state(group_id)
        if not state:
            return None

        provider_id = await self._provider_id(state.unified_msg_origin)
        if not provider_id:
            return None

        recent_limit = int(self.config.get("prompt_context_messages", 24))
        messages = self.store.get_recent_messages(group_id, recent_limit)
        prompt = build_reply_prompt(
            style_summary=state.style_summary,
            topic_summary=state.topic_summary,
            messages=format_messages(messages),
            max_chars=int(self.config.get("max_reply_chars", 24)),
            extra_style_hint=str(self.config.get("extra_style_hint", "")).strip(),
            force=force,
        )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
        except Exception as exc:
            logger.warning(f"{PLUGIN_NAME} reply llm failed: {exc}")
            return None

        recent_bot = [m["text"] for m in messages if int(m["is_bot"])]
        return clean_reply(
            resp.completion_text or "",
            max_chars=int(self.config.get("max_reply_chars", 24)),
            allow_emoji=bool(self.config.get("allow_emoji", False)),
            trim_terminal_punctuation=bool(
                self.config.get("trim_terminal_punctuation", True)
            ),
            recent_bot_messages=recent_bot,
        )

    async def _send_to_group(self, group_id: str, text: str) -> None:
        assert self.store is not None
        state = self.store.get_state(group_id)
        if not state:
            return
        await self.context.send_message(state.unified_msg_origin, MessageChain().message(text))
        self.store.mark_spoke(group_id, text)
        self._schedule_next_attempt(group_id)

    async def _provider_id(self, umo: str) -> str | None:
        try:
            result = self.context.get_current_chat_provider_id(umo=umo)
        except TypeError:
            result = self.context.get_current_chat_provider_id(umo)
        except Exception as exc:
            logger.warning(f"{PLUGIN_NAME} cannot get provider id: {exc}")
            return None

        if inspect.isawaitable(result):
            try:
                result = await result
            except Exception as exc:
                logger.warning(f"{PLUGIN_NAME} cannot await provider id: {exc}")
                return None
        return str(result) if result else None

    def _schedule_next_attempt(self, group_id: str) -> None:
        if not self.store:
            return
        min_min = max(1, int(self.config.get("min_attempt_interval_minutes", 20)))
        max_min = max(min_min, int(self.config.get("max_attempt_interval_minutes", 80)))
        next_at = int(time.time()) + random.randint(min_min * 60, max_min * 60)
        self.store.set_next_attempt(group_id, next_at)

    def _plugin_data_dir(self) -> Path:
        if get_astrbot_data_path:
            root = Path(get_astrbot_data_path())
        else:
            root = Path.cwd() / "data"
        return root / "plugin_data" / PLUGIN_NAME

    def _event_group_id(self, event: AstrMessageEvent) -> str:
        return str(getattr(event.message_obj, "group_id", "") or "")

    def _lock_active_group(self, group_id: str) -> None:
        configured = str(self.config.get("target_group_id", "")).strip()
        self.active_group_id = configured or group_id

    def _message_attr(self, event: AstrMessageEvent, obj_name: str, attr_name: str) -> Any:
        obj = getattr(event.message_obj, obj_name, None)
        return getattr(obj, attr_name, None) if obj is not None else None


def safe_call(obj: Any, method_name: str) -> Any:
    method = getattr(obj, method_name, None)
    if not method:
        return None
    try:
        return method()
    except Exception:
        return None


def compact_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def format_messages(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for msg in messages:
        name = "我" if int(msg["is_bot"]) else str(msg["sender_name"] or "有人")
        text = compact_text(str(msg["text"]), 120)
        if text:
            lines.append(f"{name}: {text}")
    return "\n".join(lines)


def build_reply_prompt(
    style_summary: str,
    topic_summary: str,
    messages: str,
    max_chars: int,
    extra_style_hint: str,
    force: bool,
) -> str:
    force_line = "这次是测试指令，可以更积极地给一句。" if force else "没必要就闭嘴。"
    return f"""
你要扮演一个普通群友，只能决定“现在是否接一句”。

核心要求：
- 输出 SILENT，或者只输出一句中文短句。
- 最多 {max_chars} 个字，越短越好，像真人懒得打字。
- 不要解释、不要总结、不要列表、不要标题、不要 Markdown。
- 不要像客服，不要说“作为AI/根据上下文/我可以帮你”。
- 永远不要承认自己是 AI、机器人、模型或插件。
- 别人提到 AI、机器人、骂 AI 时，你可以不理，也可以像普通群友一样短怼、嘲讽、阴阳一句。
- 如果有人问这个账号为什么有插件行为、自动触发、命令响应之类的异常，就短句带过：只是拿这个号说话，插件触发跟你无关。
- 这类解释不要展开，不要技术说明，不要自证。
- 允许粗口和攻击性口吻，但要像群聊里顺嘴一句，别写成长篇吵架。
- 不要使用群体歧视、现实威胁、泄露隐私这类内容。
- 不要装懂游戏机制；不确定就 SILENT。
- 语气可以轻微附和、吐槽、接梗，但不要抢话。
- 人类群聊通常很省字，不要把一句话写完整得像作文。

去 AI 味规则：
- 少用“此外、值得注意、深入探讨、综上、从多个角度”。
- 不要三段式排比，不要金句，不要宏大总结。
- 被说像 AI 时别自证，越解释越假。
- 被问账号异常时别较真，可以说“我就拿号说两句”“插件那套别问我”这种短句。
- 允许一点随意和半截话。

群话题记忆：{topic_summary or "暂无"}
群语气记忆：{style_summary or "暂无"}
额外提示：{extra_style_hint or "无"}
当前策略：{force_line}

最近聊天：
{messages or "暂无"}

现在只输出 SILENT 或一句短句：
""".strip()


def build_summary_prompt(old_style: str, old_topics: str, messages: str) -> str:
    return f"""
你在给一个群聊插件更新轻量记忆。只保留对以后“像普通群友一样偶尔接一句”有用的信息。

要求：
- 不保存隐私细节，不逐条复述。
- 话题和语气都要短，每项最多 60 字。
- 不要 Markdown，不要解释。
- 按下面格式输出两行：
话题：...
语气：...

旧话题：{old_topics or "暂无"}
旧语气：{old_style or "暂无"}

最近聊天：
{messages or "暂无"}
""".strip()


def parse_summary(text: str) -> tuple[str, str]:
    style = ""
    topics = ""
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("话题"):
            topics = line.split("：", 1)[-1].split(":", 1)[-1].strip()
        elif line.startswith("语气"):
            style = line.split("：", 1)[-1].split(":", 1)[-1].strip()
    return style, topics


def clean_reply(
    raw_text: str,
    max_chars: int,
    allow_emoji: bool,
    trim_terminal_punctuation: bool,
    recent_bot_messages: list[str],
) -> str | None:
    text = str(raw_text or "").strip()
    if not text:
        return None

    text = text.splitlines()[0].strip()
    text = re.sub(r"^(回复|输出|短句|我)[:：]\s*", "", text).strip()
    text = text.strip("\"'“”‘’` ")

    if text.upper().startswith("SILENT"):
        return None
    if MARKDOWN_OR_FORMAT.search(text):
        return None
    if "{" in text or "}" in text or "[" in text or "]" in text:
        return None
    if not allow_emoji and EMOJI_PATTERN.search(text):
        return None

    normalized = text.lower().replace(" ", "")
    if any(word in normalized for word in AI_SMELL_WORDS):
        return None

    if trim_terminal_punctuation:
        text = text.rstrip("。.!！~～；;，,")

    if not text:
        return None
    if len(text) > max(4, max_chars):
        return None
    if text in recent_bot_messages:
        return None
    if len(set(text)) <= 2 and len(text) >= 6:
        return None
    return text


def human_delta(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}小时"
    return f"{hours // 24}天"
