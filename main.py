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
    from astrbot.api.web import error_response, json_response, request as web_request
except Exception:  # pragma: no cover - Pages are optional on older AstrBot versions.
    error_response = None
    json_response = None
    web_request = None

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
    last_slang_scanned_at: int
    style_summary: str
    topic_summary: str


@dataclass
class SlangTerm:
    term: str
    meaning: str
    confidence: float
    status: str
    source: str
    updated_at: int


class MemoryStore:
    def __init__(self, db_path: Path, recent_limit: int, max_slang_terms: int):
        self.db_path = db_path
        self.recent_limit = max(20, int(recent_limit))
        self.max_slang_terms = max(10, int(max_slang_terms))

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
                    last_slang_scanned_at integer not null default 0,
                    style_summary text not null default '',
                    topic_summary text not null default ''
                )
                """
            )
            self._ensure_column(conn, "group_state", "last_slang_scanned_at", "integer not null default 0")
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
            conn.execute(
                """
                create table if not exists slang_terms (
                    group_id text not null,
                    term text not null,
                    meaning text not null default '',
                    confidence real not null default 0,
                    status text not null default 'uncertain',
                    source text not null default 'llm',
                    last_seen_at integer not null,
                    updated_at integer not null,
                    primary key (group_id, term)
                )
                """
            )
            conn.execute(
                """
                create table if not exists insider_questions (
                    id integer primary key autoincrement,
                    group_id text not null,
                    term text not null,
                    question text not null,
                    status text not null default 'pending',
                    created_at integer not null,
                    answered_at integer not null default 0
                )
                """
            )
            conn.execute(
                "create index if not exists idx_insider_status on insider_questions(status, created_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_def: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute(f"pragma table_info({table_name})").fetchall()
        }
        if column_name not in columns:
            conn.execute(f"alter table {table_name} add column {column_name} {column_def}")

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

    def mark_slang_scanned(self, group_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "update group_state set last_slang_scanned_at = ? where group_id = ?",
                (int(time.time()), group_id),
            )

    def upsert_slang(
        self,
        group_id: str,
        term: str,
        meaning: str,
        confidence: float,
        status: str,
        source: str,
    ) -> None:
        term = compact_text(term, 40)
        meaning = compact_text(meaning, 160)
        if not term:
            return
        confidence = max(0.0, min(1.0, float(confidence)))
        status = "confirmed" if status == "confirmed" else "uncertain"
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                insert into slang_terms (
                    group_id, term, meaning, confidence, status, source, last_seen_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(group_id, term) do update set
                    meaning = case
                        when excluded.status = 'confirmed' or slang_terms.status != 'confirmed'
                        then excluded.meaning
                        else slang_terms.meaning
                    end,
                    confidence = max(slang_terms.confidence, excluded.confidence),
                    status = case
                        when excluded.status = 'confirmed' then 'confirmed'
                        else slang_terms.status
                    end,
                    source = excluded.source,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (group_id, term, meaning, confidence, status, source, now, now),
            )
            self._trim_slang_terms(conn, group_id)

    def _trim_slang_terms(self, conn: sqlite3.Connection, group_id: str) -> None:
        conn.execute(
            """
            delete from slang_terms
            where group_id = ?
              and term not in (
                  select term from slang_terms
                  where group_id = ?
                  order by
                      case status when 'confirmed' then 1 else 0 end desc,
                      updated_at desc
                  limit ?
              )
            """,
            (group_id, group_id, self.max_slang_terms),
        )

    def get_slang_terms(
        self,
        group_id: str,
        status: str | None = None,
        limit: int = 30,
    ) -> list[SlangTerm]:
        limit = max(1, int(limit))
        sql = """
            select term, meaning, confidence, status, source, updated_at
            from slang_terms
            where group_id = ?
        """
        args: list[Any] = [group_id]
        if status:
            sql += " and status = ?"
            args.append(status)
        sql += " order by updated_at desc limit ?"
        args.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [
            SlangTerm(
                term=str(row["term"]),
                meaning=str(row["meaning"] or ""),
                confidence=float(row["confidence"]),
                status=str(row["status"]),
                source=str(row["source"]),
                updated_at=int(row["updated_at"]),
            )
            for row in rows
        ]

    def add_insider_question(self, group_id: str, term: str, question: str) -> int:
        now = int(time.time())
        with self._connect() as conn:
            cur = conn.execute(
                """
                insert into insider_questions (group_id, term, question, status, created_at)
                values (?, ?, ?, 'pending', ?)
                """,
                (group_id, compact_text(term, 40), compact_text(question, 500), now),
            )
            return int(cur.lastrowid)

    def get_recent_insider_question(self, group_id: str, term: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                select created_at from insider_questions
                where group_id = ? and term = ?
                order by created_at desc
                limit 1
                """,
                (group_id, term),
            ).fetchone()
        return int(row["created_at"]) if row else 0

    def get_pending_insider_questions(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select id, group_id, term, question, created_at
                from insider_questions
                where status = 'pending'
                order by created_at desc
                limit ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_insider_answered(self, question_id: int) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                update insider_questions
                set status = 'answered', answered_at = ?
                where id = ?
                """,
                (now, question_id),
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
            conn.execute("delete from slang_terms where group_id = ?", (group_id,))
            conn.execute("delete from insider_questions where group_id = ?", (group_id,))
            conn.execute(
                """
                update group_state
                set last_spoke_at = 0,
                    next_attempt_at = 0,
                    last_summarized_at = 0,
                    last_slang_scanned_at = 0,
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
            slang_count = conn.execute(
                "select count(*) as c from slang_terms where group_id = ?",
                (group_id,),
            ).fetchone()
            confirmed_slang_count = conn.execute(
                """
                select count(*) as c from slang_terms
                where group_id = ? and status = 'confirmed'
                """,
                (group_id,),
            ).fetchone()
        return {
            "messages": int(msg_count["c"]) if msg_count else 0,
            "bot_messages": int(bot_count["c"]) if bot_count else 0,
            "slang_terms": int(slang_count["c"]) if slang_count else 0,
            "confirmed_slang_terms": int(confirmed_slang_count["c"]) if confirmed_slang_count else 0,
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
            last_slang_scanned_at=int(row["last_slang_scanned_at"]),
            style_summary=str(row["style_summary"] or ""),
            topic_summary=str(row["topic_summary"] or ""),
        )


@register(PLUGIN_NAME, "15185", "单群低频自然插话原型", "0.2.0")
class IAintNoRobot(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.store: MemoryStore | None = None
        self.worker_task: asyncio.Task | None = None
        self.active_group_id = str(self.config.get("target_group_id", "")).strip()
        self._register_page_apis(context)

    async def initialize(self) -> None:
        data_dir = self._plugin_data_dir()
        recent_limit = int(self.config.get("recent_message_limit", 200))
        max_slang_terms = int(self.config.get("max_slang_terms", 80))
        self.store = MemoryStore(data_dir / "memory.sqlite3", recent_limit, max_slang_terms)
        self.store.initialize()
        self.active_group_id = self._selected_group_id()
        if not self.active_group_id and not self._managed_groups():
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
                    f"已添加群：{'是' if self._is_group_allowed(group_id) else '否'}",
                    f"开关：{'开' if state.enabled else '关'}",
                    f"短期消息：{stats['messages']} 条",
                    f"机器人发过：{stats['bot_messages']} 条",
                    f"黑话记忆：{stats['confirmed_slang_terms']}/{stats['slang_terms']} 条",
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
        self._ensure_managed_group(
            group_id,
            safe_call(event, "get_group_name") or f"群 {group_id}",
            enabled=True,
        )
        await self._save_config()
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

    @iar.command("addgroup")
    async def add_group(self, event: AstrMessageEvent):
        """把当前群加入 WebUI 可选群聊。"""
        group_id = self._event_group_id(event)
        if not group_id:
            yield event.plain_result("只能在群里用")
            return
        name = safe_call(event, "get_group_name") or f"群 {group_id}"
        added = self._ensure_managed_group(group_id, name, enabled=True)
        await self._save_config()
        yield event.plain_result("加好了" if added else "已经在列表里了")

    @iar.command("groups")
    async def list_groups(self, event: AstrMessageEvent):
        """查看 WebUI 已添加群聊。"""
        groups = self._managed_groups(include_disabled=True)
        if not groups:
            yield event.plain_result("还没添加群聊")
            return
        selected = self._selected_group_id()
        lines = []
        for group in groups:
            mark = "*" if group["group_id"] == selected else "-"
            enabled = "开" if group["enabled"] else "关"
            lines.append(f"{mark} {group['name']} / {group['group_id']} / {enabled}")
        yield event.plain_result("\n".join(lines))

    @iar.command("select")
    async def select_group(self, event: AstrMessageEvent, group_id: str):
        """选择目标群号，必须来自已添加群聊。"""
        group_id = str(group_id).strip()
        if not self._managed_group_exists(group_id, enabled_only=True):
            yield event.plain_result("这个群没添加，先 /iar addgroup")
            return
        self.config["target_group_id"] = group_id
        self.active_group_id = group_id
        await self._save_config()
        yield event.plain_result("选好了")

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

    @iar.command("slang")
    async def show_slang(self, event: AstrMessageEvent):
        """查看当前群已理解的黑话。"""
        group_id = self._event_group_id(event) or self.active_group_id
        if not group_id or not self.store:
            yield event.plain_result("还没观察到群")
            return
        terms = self.store.get_slang_terms(group_id, limit=12)
        if not terms:
            yield event.plain_result("还没学到啥黑话")
            return
        lines = []
        for term in terms:
            mark = "✓" if term.status == "confirmed" else "?"
            meaning = term.meaning or "还不确定"
            lines.append(f"{mark} {term.term}：{meaning}")
        yield event.plain_result("\n".join(lines))

    @iar.command("learn")
    async def force_learn_slang(self, event: AstrMessageEvent):
        """手动扫描当前群黑话。"""
        group_id = self._event_group_id(event) or self.active_group_id
        if not group_id or not self.store:
            yield event.plain_result("还没观察到群")
            return
        updated = await self._refresh_slang(group_id, force=True)
        if not updated:
            yield event.plain_result("没学到新的")
            return
        yield event.plain_result("记了一点")

    @iar.command("reset")
    async def reset_group(self, event: AstrMessageEvent):
        """清空当前群的插件记忆。"""
        group_id = self._event_group_id(event) or self.active_group_id
        if not group_id or not self.store:
            yield event.plain_result("还没观察到群")
            return
        self.store.clear_group(group_id)
        yield event.plain_result("清了")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=-100)
    async def handle_mention(self, event: AstrMessageEvent):
        """接管群内直接艾特，避免落到默认标准 LLM 回复。"""
        if (
            not bool(self.config.get("enabled", True))
            or not bool(self.config.get("handle_mentions", True))
            or not self.store
        ):
            return

        group_id = self._event_group_id(event)
        if not group_id or not self._is_group_allowed(group_id):
            return
        if not self._is_wake_up(event):
            return

        text = compact_text(event.message_str or "", 300)
        if not text or text.startswith("/iar"):
            return
        if self._should_passthrough_mention(text):
            return

        self._lock_active_group(group_id)
        self.store.ensure_group(group_id, event.unified_msg_origin, True)

        reply = await self._generate_reply(
            group_id,
            force=True,
            mode="mention",
            current_message=text,
        )
        if bool(self.config.get("stop_default_mention_reply", True)):
            self._stop_event(event)

        if reply:
            yield event.plain_result(reply)
            self.store.mark_spoke(group_id, reply)

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
        if not self._is_group_allowed(group_id):
            return
        self._lock_active_group(group_id)

        text = compact_text(event.message_str or "", 300)
        if not text or text.startswith("/iar"):
            return

        self.store.ensure_group(group_id, event.unified_msg_origin, True)
        self._remember_event_message(group_id, event, text)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def observe_private(self, event: AstrMessageEvent):
        """尝试把内线私信回复写回黑话记忆。"""
        if not self.store or not bool(self.config.get("enable_insider", False)):
            return
        insider_qq = str(self.config.get("insider_qq", "")).strip()
        if not insider_qq:
            return
        sender_id = str(safe_call(event, "get_sender_id") or "").strip()
        if sender_id != insider_qq:
            return
        answer = compact_text(event.message_str or "", 240)
        if not answer:
            return
        pending = self.store.get_pending_insider_questions(limit=1)
        if not pending:
            return
        question = pending[0]
        meaning = await self._condense_insider_answer(
            question["group_id"],
            question["term"],
            answer,
        )
        if not meaning:
            meaning = answer
        self.store.upsert_slang(
            question["group_id"],
            question["term"],
            meaning,
            confidence=0.92,
            status="confirmed",
            source="insider",
        )
        self.store.mark_insider_answered(int(question["id"]))

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
        if not self._is_group_allowed(group_id):
            self.active_group_id = self._selected_group_id()
            return
        self.active_group_id = group_id

        await self._maybe_refresh_summary(group_id)
        await self._maybe_refresh_slang(group_id)

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

    async def _maybe_refresh_slang(self, group_id: str) -> None:
        if not bool(self.config.get("learn_slang", True)):
            return
        if not self.store:
            return
        interval = int(self.config.get("slang_scan_interval_minutes", 180)) * 60
        if interval <= 0:
            return
        state = self.store.get_state(group_id)
        if not state:
            return
        if int(time.time()) - state.last_slang_scanned_at < interval:
            return
        await self._refresh_slang(group_id, force=False)

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

    async def _refresh_slang(self, group_id: str, force: bool) -> bool:
        assert self.store is not None
        if not bool(self.config.get("learn_slang", True)):
            return False

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

        known_terms = self.store.get_slang_terms(group_id, limit=30)
        prompt = build_slang_prompt(
            messages=format_messages(messages[-60:]),
            known_slang=format_slang_terms(known_terms),
            has_visual_context=messages_mention_visual_context(messages),
        )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
        except Exception as exc:
            logger.warning(f"{PLUGIN_NAME} slang llm failed: {exc}")
            return False
        self.store.mark_slang_scanned(group_id)

        parsed = parse_slang_items(resp.completion_text or "")
        if not parsed:
            return False

        changed = False
        for item in parsed:
            status = "confirmed" if item["confidence"] >= 0.72 else "uncertain"
            self.store.upsert_slang(
                group_id=group_id,
                term=item["term"],
                meaning=item["meaning"],
                confidence=item["confidence"],
                status=status,
                source="llm",
            )
            changed = True
            if status == "uncertain":
                await self._maybe_ask_insider_about_slang(
                    group_id,
                    item["term"],
                    item["meaning"],
                    messages[-20:],
                )
        return changed

    async def _generate_reply(
        self,
        group_id: str,
        force: bool,
        mode: str = "ambient",
        current_message: str = "",
    ) -> str | None:
        assert self.store is not None
        state = self.store.get_state(group_id)
        if not state:
            return None

        provider_id = await self._provider_id(state.unified_msg_origin)
        if not provider_id:
            return None

        recent_limit = int(self.config.get("prompt_context_messages", 24))
        messages = self.store.get_recent_messages(group_id, recent_limit)
        confirmed_slang = self.store.get_slang_terms(group_id, status="confirmed", limit=20)
        uncertain_slang = self.store.get_slang_terms(group_id, status="uncertain", limit=20)
        prompt = build_reply_prompt(
            style_summary=state.style_summary,
            topic_summary=state.topic_summary,
            confirmed_slang=format_slang_terms(confirmed_slang),
            uncertain_slang=", ".join(term.term for term in uncertain_slang) or "无",
            messages=format_messages(messages),
            max_chars=self._reply_max_chars(mode),
            extra_style_hint=str(self.config.get("extra_style_hint", "")).strip(),
            allow_self_start=bool(self.config.get("allow_self_start", True)),
            self_start_probability=float(self.config.get("self_start_probability", 0.18)),
            self_start_examples=str(self.config.get("self_start_style_examples", "")).strip(),
            mode=mode,
            current_message=current_message,
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
            max_chars=self._reply_max_chars(mode),
            allow_emoji=bool(self.config.get("allow_emoji", False)),
            trim_terminal_punctuation=bool(
                self.config.get("trim_terminal_punctuation", True)
            ),
            recent_bot_messages=recent_bot,
        )

    async def _maybe_ask_insider_about_slang(
        self,
        group_id: str,
        term: str,
        guessed_meaning: str,
        messages: list[dict[str, Any]],
    ) -> None:
        assert self.store is not None
        if not bool(self.config.get("enable_insider", False)):
            return
        insider_qq = str(self.config.get("insider_qq", "")).strip()
        if not insider_qq:
            return

        cooldown = int(self.config.get("insider_question_cooldown_minutes", 60)) * 60
        last_asked_at = self.store.get_recent_insider_question(group_id, term)
        if last_asked_at and int(time.time()) - last_asked_at < cooldown:
            return

        question = await self._generate_insider_question(
            group_id=group_id,
            term=term,
            guessed_meaning=guessed_meaning,
            messages=messages,
        )
        if not question:
            return
        self.store.add_insider_question(group_id, term, question)
        sent = await self._send_private_message(insider_qq, question, group_id)
        if not sent:
            logger.info(f"{PLUGIN_NAME} queued insider question but private send failed: {term}")

    async def _generate_insider_question(
        self,
        group_id: str,
        term: str,
        guessed_meaning: str,
        messages: list[dict[str, Any]],
    ) -> str | None:
        assert self.store is not None
        state = self.store.get_state(group_id)
        if not state:
            return None
        provider_id = await self._provider_id(state.unified_msg_origin)
        if not provider_id:
            return None
        prompt = build_insider_question_prompt(
            term=term,
            guessed_meaning=guessed_meaning,
            messages=format_messages(messages),
        )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
        except Exception as exc:
            logger.warning(f"{PLUGIN_NAME} insider question llm failed: {exc}")
            return None
        return clean_private_message(resp.completion_text or "", max_chars=90)

    async def _condense_insider_answer(self, group_id: str, term: str, answer: str) -> str | None:
        assert self.store is not None
        state = self.store.get_state(group_id)
        if not state:
            return None
        provider_id = await self._provider_id(state.unified_msg_origin)
        if not provider_id:
            return None
        prompt = build_insider_answer_prompt(term, answer)
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
        except Exception as exc:
            logger.warning(f"{PLUGIN_NAME} insider answer llm failed: {exc}")
            return None
        text = compact_text(resp.completion_text or "", 120)
        if text.upper().startswith("SILENT"):
            return None
        return text.strip("\"'“”‘’` ")

    async def _send_private_message(self, user_id: str, message: str, group_id: str) -> bool:
        """Best-effort private send for QQ/OneBot-like adapters."""
        candidates = [self.context]
        platform_manager = getattr(self.context, "platform_manager", None)
        if platform_manager is not None:
            candidates.append(platform_manager)

        if self.store:
            state = self.store.get_state(group_id)
            umo = state.unified_msg_origin if state else ""
            for getter_name in ("get_platform_by_umo", "get_platform", "get_platform_inst"):
                getter = getattr(platform_manager, getter_name, None) if platform_manager else None
                if getter and umo:
                    platform = await self._maybe_call(getter, umo)
                    if platform is not None:
                        candidates.append(platform)

        user_arg = int(user_id) if user_id.isdigit() else user_id
        for target in list(candidates):
            for attr in ("send_private_msg", "send_private_message"):
                method = getattr(target, attr, None)
                if method and await self._try_call(method, user_id=user_arg, message=message):
                    return True
            for attr in ("call_api", "call_action"):
                method = getattr(target, attr, None)
                if not method:
                    continue
                attempts = (
                    (("send_private_msg",), {"user_id": user_arg, "message": message}),
                    (("send_private_msg", {"user_id": user_arg, "message": message}), {}),
                    ((), {"action": "send_private_msg", "params": {"user_id": user_arg, "message": message}}),
                    (("send_msg",), {"message_type": "private", "user_id": user_arg, "message": message}),
                )
                for args, kwargs in attempts:
                    if await self._try_call(method, *args, **kwargs):
                        return True
        return False

    async def _maybe_call(self, method: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            result = method(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception:
            return None

    async def _try_call(self, method: Any, *args: Any, **kwargs: Any) -> bool:
        try:
            result = method(*args, **kwargs)
            if inspect.isawaitable(result):
                await result
            return True
        except TypeError:
            return False
        except Exception as exc:
            logger.debug(f"{PLUGIN_NAME} adapter call failed: {exc}")
            return False

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

    def _remember_event_message(
        self,
        group_id: str,
        event: AstrMessageEvent,
        text: str,
    ) -> None:
        if not self.store:
            return
        sender_id = (
            safe_call(event, "get_sender_id")
            or self._message_attr(event, "sender", "user_id")
            or ""
        )
        sender_name = safe_call(event, "get_sender_name") or sender_id or "有人"
        self.store.append_message(
            group_id=group_id,
            sender_id=str(sender_id),
            sender_name=str(sender_name),
            text=text,
            is_bot=False,
        )

    def _is_wake_up(self, event: AstrMessageEvent) -> bool:
        value = safe_call(event, "is_wake_up")
        if value is not None:
            return bool(value)
        value = getattr(event, "is_wake_up", None)
        if isinstance(value, bool):
            return value
        return False

    def _should_passthrough_mention(self, text: str) -> bool:
        text = strip_mention_noise(text)
        if not text:
            return False
        patterns = str(self.config.get("mention_passthrough_patterns", "")).splitlines()
        for raw_pattern in patterns:
            pattern = raw_pattern.strip()
            if not pattern:
                continue
            try:
                if re.search(pattern, text, flags=re.IGNORECASE):
                    return True
            except re.error:
                logger.warning(f"{PLUGIN_NAME} invalid mention passthrough pattern: {pattern}")
        return False

    def _stop_event(self, event: AstrMessageEvent) -> None:
        for method_name in ("stop_event", "stop_propagation"):
            method = getattr(event, method_name, None)
            if not method:
                continue
            try:
                method()
                return
            except Exception as exc:
                logger.debug(f"{PLUGIN_NAME} stop event failed: {exc}")

    def _reply_max_chars(self, mode: str) -> int:
        if mode == "mention":
            return int(self.config.get("mention_reply_max_chars", 36))
        return int(self.config.get("max_reply_chars", 24))

    def _register_page_apis(self, context: Context) -> None:
        register_api = getattr(context, "register_web_api", None)
        if not register_api or not json_response:
            return
        routes = (
            ("/iar/groups", self._api_groups, ["GET"]),
            ("/iar/groups/add", self._api_add_group, ["POST"]),
            ("/iar/groups/select", self._api_select_group, ["POST"]),
            ("/iar/groups/remove", self._api_remove_group, ["POST"]),
        )
        for path, handler, methods in routes:
            try:
                register_api(path, handler, methods=methods)
            except TypeError:
                try:
                    register_api(path, handler)
                except Exception as exc:
                    logger.debug(f"{PLUGIN_NAME} web api register failed {path}: {exc}")
            except Exception as exc:
                logger.debug(f"{PLUGIN_NAME} web api register failed {path}: {exc}")

    async def _api_groups(self, *args: Any):
        return json_response(
            {
                "selected": self._selected_group_id(),
                "groups": self._managed_groups(include_disabled=True),
            }
        )

    async def _api_add_group(self, *args: Any):
        payload = await self._request_json(args[0] if args else None)
        group_id = compact_text(payload.get("group_id", ""), 40)
        name = compact_text(payload.get("name", ""), 80) or f"群 {group_id}"
        if not group_id:
            return self._web_error("群号不能为空")
        self._ensure_managed_group(group_id, name, enabled=True)
        await self._save_config()
        return json_response({"ok": True, "groups": self._managed_groups(include_disabled=True)})

    async def _api_select_group(self, *args: Any):
        payload = await self._request_json(args[0] if args else None)
        group_id = compact_text(payload.get("group_id", ""), 40)
        if not self._managed_group_exists(group_id, enabled_only=True):
            return self._web_error("只能选择已添加且启用的群聊")
        self.config["target_group_id"] = group_id
        self.active_group_id = group_id
        await self._save_config()
        return json_response({"ok": True, "selected": group_id})

    async def _api_remove_group(self, *args: Any):
        payload = await self._request_json(args[0] if args else None)
        group_id = compact_text(payload.get("group_id", ""), 40)
        groups = [
            group
            for group in self._managed_groups(include_disabled=True)
            if group["group_id"] != group_id
        ]
        self.config["managed_groups"] = groups
        if str(self.config.get("target_group_id", "")).strip() == group_id:
            self.config["target_group_id"] = ""
            self.active_group_id = self._first_enabled_managed_group_id()
        await self._save_config()
        return json_response(
            {
                "ok": True,
                "selected": self._selected_group_id(),
                "groups": self._managed_groups(include_disabled=True),
            }
        )

    async def _request_json(self, req: Any = None) -> dict[str, Any]:
        target = req or web_request
        if not target:
            return {}
        try:
            json_method = getattr(target, "json", None)
            if not json_method:
                return {}
            payload = json_method()
            if inspect.isawaitable(payload):
                payload = await payload
        except Exception:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _web_error(self, message: str):
        if error_response:
            return error_response(message)
        return json_response({"ok": False, "message": message})

    def _managed_groups(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        raw_groups = self.config.get("managed_groups", [])
        if not isinstance(raw_groups, list):
            return []
        groups: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_groups:
            if not isinstance(raw, dict):
                continue
            group_id = compact_text(raw.get("group_id", ""), 40)
            if not group_id or group_id in seen:
                continue
            enabled = bool(raw.get("enabled", True))
            if not include_disabled and not enabled:
                continue
            name = compact_text(raw.get("name", ""), 80) or f"群 {group_id}"
            groups.append({"group_id": group_id, "name": name, "enabled": enabled})
            seen.add(group_id)
        return groups

    def _managed_group_exists(self, group_id: str, enabled_only: bool = False) -> bool:
        group_id = str(group_id).strip()
        return any(
            group["group_id"] == group_id and (group["enabled"] or not enabled_only)
            for group in self._managed_groups(include_disabled=True)
        )

    def _ensure_managed_group(self, group_id: str, name: str, enabled: bool) -> bool:
        groups = self._managed_groups(include_disabled=True)
        for group in groups:
            if group["group_id"] == group_id:
                group["name"] = group["name"] or name or f"群 {group_id}"
                group["enabled"] = group["enabled"] or enabled
                self.config["managed_groups"] = groups
                return False
        groups.append(
            {
                "group_id": group_id,
                "name": name or f"群 {group_id}",
                "enabled": enabled,
            }
        )
        self.config["managed_groups"] = groups
        return True

    def _first_enabled_managed_group_id(self) -> str:
        groups = self._managed_groups(include_disabled=False)
        return groups[0]["group_id"] if groups else ""

    def _selected_group_id(self) -> str:
        configured = str(self.config.get("target_group_id", "")).strip()
        if self._managed_groups(include_disabled=True):
            if self._managed_group_exists(configured, enabled_only=True):
                return configured
            return self._first_enabled_managed_group_id()
        return configured

    def _is_group_allowed(self, group_id: str) -> bool:
        group_id = str(group_id).strip()
        if not group_id:
            return False
        if self._managed_groups(include_disabled=True):
            return self._managed_group_exists(group_id, enabled_only=True)
        configured = str(self.config.get("target_group_id", "")).strip()
        return not configured or group_id == configured

    async def _save_config(self) -> None:
        save = getattr(self.config, "save_config", None)
        if not save:
            return
        try:
            result = save()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning(f"{PLUGIN_NAME} config save failed: {exc}")

    def _plugin_data_dir(self) -> Path:
        if get_astrbot_data_path:
            root = Path(get_astrbot_data_path())
        else:
            root = Path.cwd() / "data"
        return root / "plugin_data" / PLUGIN_NAME

    def _event_group_id(self, event: AstrMessageEvent) -> str:
        return str(getattr(event.message_obj, "group_id", "") or "")

    def _lock_active_group(self, group_id: str) -> None:
        selected = self._selected_group_id()
        self.active_group_id = selected or group_id

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


def strip_mention_noise(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"\[CQ:at,qq=(?:all|\d+)\]", " ", text)
    text = re.sub(r"@\S+", " ", text)
    return compact_text(text, 300)


def format_messages(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for msg in messages:
        name = "我" if int(msg["is_bot"]) else str(msg["sender_name"] or "有人")
        text = compact_text(str(msg["text"]), 120)
        if text:
            lines.append(f"{name}: {text}")
    return "\n".join(lines)


def format_slang_terms(terms: list[SlangTerm]) -> str:
    if not terms:
        return "无"
    lines = []
    for term in terms:
        meaning = term.meaning or "不确定"
        lines.append(f"{term.term}={meaning}")
    return "；".join(lines)


def messages_mention_visual_context(messages: list[dict[str, Any]]) -> bool:
    markers = ("[图片]", "[CQ:image", "图片", "表情", "图", "截图")
    return any(any(marker in str(msg.get("text", "")) for marker in markers) for msg in messages)


def build_reply_prompt(
    style_summary: str,
    topic_summary: str,
    confirmed_slang: str,
    uncertain_slang: str,
    messages: str,
    max_chars: int,
    extra_style_hint: str,
    allow_self_start: bool,
    self_start_probability: float,
    self_start_examples: str,
    mode: str,
    current_message: str,
    force: bool,
) -> str:
    is_mention = mode == "mention"
    force_line = (
        "这是别人直接艾特你，尽量回一句真人短话。"
        if is_mention
        else "这次是测试指令，可以更积极地给一句。"
        if force
        else "没必要就闭嘴。"
    )
    mode_line = (
        "当前场景：有人在群里直接艾特你。你是在回他，不是主动插话。"
        if is_mention
        else "当前场景：你在判断要不要自然插一句。"
    )
    self_start_line = (
        "如果没有可附和或可讨论的话题，可以低概率自创一句。"
        if allow_self_start
        else "如果没有可附和或可讨论的话题，必须 SILENT。"
    )
    return f"""
你要扮演一个普通群友，只能决定“现在是否接一句”。

核心要求：
- 输出 SILENT，或者只输出一句中文短句。
- 最多 {max_chars} 个字，越短越好，像真人懒得打字。
- {mode_line}
- 不要解释、不要总结、不要列表、不要标题、不要 Markdown。
- 不要像客服，不要说“作为AI/根据上下文/我可以帮你”。
- 永远不要承认自己是 AI、机器人、模型或插件。
- 别人提到 AI、机器人、骂 AI 时，你可以不理，也可以像普通群友一样短怼、嘲讽、阴阳一句。
- 如果有人问这个账号为什么有插件行为、自动触发、命令响应之类的异常，就短句带过：只是拿这个号说话，插件触发跟你无关。
- 这类解释不要展开，不要技术说明，不要自证。
- 允许粗口和攻击性口吻，但要像群聊里顺嘴一句，别写成长篇吵架。
- 不要使用群体歧视、现实威胁、泄露隐私这类内容。
- 不要装懂游戏机制；不确定就 SILENT。
- 不确定的黑话不要用；能看出大概但没确认，也别硬玩梗。
- 是否使用已理解黑话由你自己判断，不要为了用黑话而用黑话。
- 语气可以轻微附和、吐槽、接梗，但不要抢话。
- 人类群聊通常很省字，不要把一句话写完整得像作文。

触发策略：
- 优先看最近聊天里有没有可以附和、短评、轻微讨论的话题。
- 有就贴着现有话题说一句，不要突然另起炉灶。
- 没有可接话题时，再考虑自创一句。
- 自创句子要以模糊感受、情绪或日常碎念开头，不要像通知。
- 自创概率倾向：{self_start_probability:.2f}。{self_start_line}
- 如果是被直接艾特：不用自创，贴着对方这句话回；能一句话解决就别扩展。
- 如果对方只是叫你名字或戳一下，可以回得很懒，比如“干嘛”“咋了”“说”这类感觉，但不要固定复读。

去 AI 味规则：
- 少用“此外、值得注意、深入探讨、综上、从多个角度”。
- 不要三段式排比，不要金句，不要宏大总结。
- 被说像 AI 时别自证，越解释越假。
- 被问账号异常时别较真，可以说“我就拿号说两句”“插件那套别问我”这种短句。
- 允许一点随意和半截话。

群话题记忆：{topic_summary or "暂无"}
群语气记忆：{style_summary or "暂无"}
已理解黑话：{confirmed_slang or "无"}
不确定黑话，避开别用：{uncertain_slang or "无"}
额外提示：{extra_style_hint or "无"}
当前策略：{force_line}
这次直接相关消息：{current_message or "无"}
自创短句示例：
{self_start_examples or "ok今天已到账五个大饼"}

最近聊天：
{messages or "暂无"}

现在只输出 SILENT 或一句短句：
""".strip()


def build_slang_prompt(messages: str, known_slang: str, has_visual_context: bool) -> str:
    visual_line = "最近可能有图片/表情包上下文，模型不能识图，不确定就降置信度。" if has_visual_context else "最近没有明显图片上下文。"
    return f"""
你在帮群聊插件学习这个群的黑话、缩写、梗词。

要求：
- 只提取这个群里可能有特殊含义的词，不要提取普通词。
- 能从文本里确定含义才高置信度；不确定就低置信度。
- 如果含义依赖图片、表情包、游戏机制、群内历史，不能硬猜。
- 最多输出 5 条。
- 没有发现就输出 NONE。
- 不要 Markdown，不要解释。
- 每行格式：词|含义|置信度
- 置信度是 0 到 1 的小数。

已知黑话：{known_slang or "无"}
视觉上下文：{visual_line}

最近聊天：
{messages or "暂无"}

现在输出：
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


def parse_slang_items(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip().strip("-* ")
        if not line or line.upper().startswith("NONE"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 3:
            continue
        term, meaning, confidence_text = parts
        term = compact_text(term, 40)
        meaning = compact_text(meaning, 160)
        if not term or not meaning:
            continue
        try:
            confidence = float(confidence_text)
        except ValueError:
            confidence = 0.35
        if confidence <= 0:
            continue
        items.append(
            {
                "term": term,
                "meaning": meaning,
                "confidence": max(0.0, min(1.0, confidence)),
            }
        )
    return items[:5]


def build_insider_question_prompt(
    term: str,
    guessed_meaning: str,
    messages: str,
) -> str:
    return f"""
你要给一个熟人内线发私信，问一个群里的黑话是什么意思。

要求：
- 只输出要发给内线的一条私信。
- 像真人随手问一句，不要像工单、报告、模板。
- 最多 90 字。
- 不要 Markdown，不要列表，不要解释自己是谁。
- 可以提一下你猜的大概意思，但别装懂。
- 语气随意，不需要礼貌过头；对方不回也没关系。

不确定的词：{term}
你目前猜测：{guessed_meaning or "不确定"}

相关聊天：
{messages or "暂无"}

现在写这条私信：
""".strip()


def build_insider_answer_prompt(term: str, answer: str) -> str:
    return f"""
把内线对黑话的解释压缩成一句短释义。

要求：
- 只输出释义，不要解释过程。
- 最多 40 字。
- 不要 Markdown。
- 如果对方没回答含义，输出 SILENT。

黑话：{term}
内线回复：{answer}
""".strip()


def clean_private_message(raw_text: str, max_chars: int) -> str | None:
    text = str(raw_text or "").strip()
    if not text:
        return None
    text = text.splitlines()[0].strip()
    text = re.sub(r"^(私信|消息|问题|输出)[:：]\s*", "", text).strip()
    text = text.strip("\"'“”‘’` ")
    if not text or text.upper().startswith("SILENT"):
        return None
    if MARKDOWN_OR_FORMAT.search(text):
        return None
    if len(text) > max_chars:
        return None
    normalized = text.lower().replace(" ", "")
    if any(word in normalized for word in AI_SMELL_WORDS):
        return None
    return text


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
