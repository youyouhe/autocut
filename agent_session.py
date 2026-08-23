# agent_session.py — SDK SQLiteSession 会话记忆 + chats.db 展示镜像 + 历史迁移 + compaction
#
# 双写架构 (plan: robust-cooking-wigderson.md):
#   - LLM 上下文: agents.memory.SQLiteSession (agent_sessions.db, 每会话一个 session_id)
#   - 前端展示/会话列表: chats.db 原有结构不动 (messages JSON + toolDetails 卡片)
#   - 旧会话首次使用时把 chats.db 历史迁移灌入 session (幂等)
#   - 会话过长时 compaction: 旧 items 摘要成一段 system 前缀, 保留最近若干条
import json
import os

from agents.memory import SQLiteSession

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_DB = os.environ.get('AGENT_SESSION_DB', os.path.join(HERE, 'agent_sessions.db'))

# 关键字段: 任务依赖的结构化数据优先全量保留 (get_transcript 的 segments/srt 被 300 字
# 截断切掉曾导致 agent 每轮重查同一工具 —— 见 render_server 旧历史回灌注释)
KEY_FIELDS = ('srt', 'segments', 'transcript', 'tags', 'tracks', 'resources', 'shots')
KEY_BUDGET = 1200
WINDOW_ITEMS = 40       # 超过触发 compaction
KEEP_RECENT = 15        # compaction 后保留的最近条数


def build_receipt_text(td):
    """工具回执 → 紧凑文本 (关键字段优先, 原自 render_server 历史回灌逻辑)."""
    res = td.get('result')
    if isinstance(res, dict):
        budget, parts = KEY_BUDGET, []
        for k in KEY_FIELDS:
            if k in res:
                try:
                    kv = json.dumps({k: res[k]}, ensure_ascii=False)
                except Exception:
                    kv = str({k: res[k]})
                if len(kv) <= budget:
                    parts.append(kv[:budget])
                    budget -= len(kv)
        if parts:
            rest = {k: str(v)[:150] for k, v in res.items() if k not in KEY_FIELDS}
            txt = '; '.join(parts)
            if rest:
                txt += '; 其余字段: ' + json.dumps(rest, ensure_ascii=False)[:300]
            return txt
        return json.dumps(res, ensure_ascii=False)[:600]
    return str(res)[:300]


def get_session(conversation_id: str) -> SQLiteSession:
    """取 (或建) 某会话的 SDK session."""
    return SQLiteSession(session_id=conversation_id, db_path=SESSION_DB)


def session_has_items(session: SQLiteSession) -> bool:
    """同步判断 session 是否已有 items (内部 asyncio.run, 只在请求线程调用)."""
    import asyncio
    try:
        return len(asyncio.run(session.get_items())) > 0
    except Exception:
        return False


def migrate_history(conversation_id: str, prior_messages):
    """旧会话 (chats.db 已有历史但 SDK session 为空) → 把历史灌成 SDK input items.
    user/assistant 原样; tool 卡片转成回执文本 (user 角色, 连续合并).
    幂等: session 已有 items 则跳过. 返回是否执行了迁移."""
    session = get_session(conversation_id)
    if session_has_items(session):
        return False
    items, pend = [], []

    def flush():
        nonlocal pend
        if pend:
            items.append({'role': 'user', 'content': '\n'.join(pend)})
            pend = []

    for m in prior_messages or []:
        role = m.get('role')
        if role in ('user', 'assistant') and m.get('content'):
            flush()
            items.append({'role': role, 'content': m['content']})
        elif role == 'tool' and m.get('toolDetails'):
            td = m['toolDetails']
            pend.append(f"[工具 {td.get('tool')} 结果回执] {build_receipt_text(td)}")
    flush()
    if not items:
        return False
    try:
        import asyncio
        async def _add():
            await session.add_items(items)
        asyncio.run(_add())
        return True
    except Exception as e:
        print(f'[agent_session] 迁移历史失败 {conversation_id}: {e}', flush=True)
        return False


def estimate_chars(items) -> int:
    return sum(len(str(i)) for i in items)


def _is_orphan_output(item) -> bool:
    """条目是否为"工具输出"类 (function_call_output / role=tool) —— 若它出现在
    保留窗口的开头, 其对应的 tool_calls 调用已被切掉, 会触发 DeepSeek 400:
    Messages with role 'tool' must be a response to a preceding message with 'tool_calls'."""
    t = str(getattr(item, 'type', None) or (item.get('type') if isinstance(item, dict) else '') or '')
    role = str(getattr(item, 'role', None) or (item.get('role') if isinstance(item, dict) else '') or '')
    return 'function_call_output' in t or 'tool_output' in t or role == 'tool'


def _trim_leading_orphans(items):
    """丢掉窗口开头的孤儿工具输出 (及其前面的 reasoning 残段), 直到非孤儿条目."""
    out = list(items)
    while out and (_is_orphan_output(out[0]) or str(getattr(out[0], 'type', '') or (out[0].get('type', '') if isinstance(out[0], dict) else '')).startswith('reasoning')):
        out.pop(0)
    return out


async def sanitize_session(session: SQLiteSession) -> bool:
    """修复已损坏的会话历史: 开头若为孤儿工具输出, 丢弃后整体重写. 返回是否修复."""
    items = await session.get_items()
    trimmed = _trim_leading_orphans(items)
    if len(trimmed) == len(items):
        return False
    await session.clear_session()
    if trimmed:
        await session.add_items(trimmed)
    return True


async def maybe_compact(session: SQLiteSession, conversation_id: str, uid: str) -> str:
    """会话超长时压缩: 旧 items → flash 模型摘要 + 最近 KEEP_RECENT 条.
    返回摘要文本 (未触发返回 ''). 摘要同时回写 chats.db 一条特殊 assistant 条目."""
    items = await session.get_items()
    if len(items) <= WINDOW_ITEMS and estimate_chars(items) < 25000:
        return ''
    old, recent = items[:-KEEP_RECENT], items[-KEEP_RECENT:]
    # 切口不许落在孤儿工具输出上 (其 tool_calls 在 old 段会被摘要掉, 留下孤儿触发 400)
    recent = _trim_leading_orphans(recent)
    old = items[:len(items) - len(recent)]
    old_text = '\n'.join(
        f"[{i.get('role', '?')}] {str(i.get('content', ''))[:600]}" for i in old
    )[:12000]
    prompt = f"""把以下视频编辑助手的对话历史压缩成结构化摘要, 供后续继续工作使用。必须保留:
1. 用户的任务目标与所有明确要求
2. 已确认的事实 (素材内容/转录要点/时间点/草稿现状)
3. 已完成的操作 (建了什么草稿/加了什么素材/渲染状态)
4. 待办事项
直接输出摘要正文, 不要客套。

对话历史:
{old_text}"""
    try:
        from openai import AsyncOpenAI
        from agent_runtime import resolve_provider
        base_url, api_key, model = resolve_provider()
        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        r = await client.chat.completions.create(model=model, messages=[
            {'role': 'user', 'content': prompt}])
        summary = (r.choices[0].message.content or '').strip()
    except Exception as e:
        print(f'[agent_session] compaction 摘要失败: {e}', flush=True)
        return ''
    if not summary:
        return ''
    # 重置 session: 摘要 + 最近条目
    await session.clear_session()
    await session.add_items([{'role': 'user', 'content': f'【前期对话摘要】\n{summary}'}] + recent)
    # 镜像写 chats.db (前端可见)
    try:
        import chat_store
        conv = chat_store.get(conversation_id, user_id=uid)
        if conv:
            msgs = conv['messages']
            msgs.append({'role': 'assistant', 'content': f'【前期对话摘要】\n{summary}'})
            chat_store.save_messages(conversation_id, msgs, conv.get('draft_id'), user_id=uid)
    except Exception as e:
        print(f'[agent_session] 摘要镜像写 chats.db 失败: {e}', flush=True)
    print(f'[agent_session] 会话 {conversation_id} 已压缩: {len(items)} → {KEEP_RECENT + 1} 条', flush=True)
    return summary
