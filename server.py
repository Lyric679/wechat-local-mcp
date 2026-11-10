# -*- coding: utf-8 -*-
"""微信本地聊天 MCP 服务。

启动方式（Claude Code 会自己拉起）：
    <python> server.py

数据来自解密后的本地库，只在本机读写，不联网。
"""
import logging
import sys
import time

sys.stderr.reconfigure(encoding="utf-8", errors="replace")   # 日志走 stderr
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="[微信MCP] %(levelname)s %(message)s")

from mcp.server.fastmcp import FastMCP

import wxcore
from wxquery import INDEX_CACHE, Store

mcp = FastMCP("wechat-local")
_store = None


def store():
    global _store
    if _store is None:
        _store = Store()
    return _store


def _ensure_data():
    """库没解过就提示先刷新。"""
    d = wxcore.DEC_DIR / "message"
    if not d.exists() or not any(d.glob("message_*.db")):
        raise RuntimeError(
            "还没有解密后的数据。请先在命令行运行：\n"
            f'  python "{wxcore.__file__.replace("wxcore.py", "refresh.py")}"')


def _fmt_msg(m, with_chat=False):
    who = m["sender"]
    head = f"{m['time']}  "
    if with_chat:
        head += f"[{m['chat']}] "
    body = m["text"] or f"[{m['type']}]"
    if m["type"] == "语音" and m.get("seconds"):
        body = f"（{m['seconds']:g}秒）{body}"
    if "\n" in body:                     # 集录 / 带本地路径的文件卡：保留换行
        lines = body.split("\n")
        if len(lines) > 14:
            lines = lines[:14] + [f"  ……还有 {len(lines) - 14} 行"]
        return head + who + ":\n" + "\n".join("    " + ln for ln in lines)
    if len(body) > 300:
        body = body[:300] + "…"
    return f"{head}{who}: {body}"


@mcp.tool()
def list_recent_chats(days: float = 0, limit: int = 20) -> str:
    """列出最近有消息的微信会话（含群），按最后活跃时间倒序。

    参数：
      days — 只看这段时间内有消息的会话；0 表示不限
      limit — 返回条数上限
    """
    _ensure_data()
    days = days or None
    rows = store().recent_chats(limit=limit, days=days)
    if not rows:
        return "没有符合条件的会话。"
    lines = [f"最近 {len(rows)} 个会话（共 {len(store().index())} 个有消息的会话）："]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i:>2}. {r['last']}  {r['count']:>5} 条  {r['name']}")
    return "\n".join(lines)


@mcp.tool()
def read_chat(who: str, limit: int = 30, days: float = 0, offset: int = 0) -> str:
    """看某个人或某个群的聊天记录（倒序取最近的，再按时间正序显示）。

    参数：
      who     — 昵称 / 备注名 / 群名 / wxid，支持片段模糊匹配；
               匹配到多个时会列出来让你选
      limit   — 取多少条
      days    — 只要这段时间内的；0 表示不限
      offset  — 翻页用，跳过最近多少条
    """
    _ensure_data()
    s = store()
    us = s._find_username(who)
    if len(us) > 1:
        shows = [f"  - {s.display(u)}（{u}，{s._table_for(u)['count'] if s._table_for(u) else 0} 条）"
                 for u in us[:8]]
        return f"「{who}」匹配到 {len(us)} 个会话，请说得更具体：\n" + "\n".join(shows)
    if not us:
        return f"没找到「{who}」的会话。可以先用 list_recent_chats 看看都有谁。"
    meta, msgs = s.chat_messages(who, limit=limit, days=days or None, offset=offset)
    if not msgs:
        return f"{meta['name']} 在这段时间内没有消息（该会话共 {meta['count']} 条）。"
    lines = [f"=== {meta['name']}（{meta['username']}，总计 {meta['count']} 条）"
             f"最近 {len(msgs)} 条 ==="]
    lines += [_fmt_msg(m) for m in msgs]
    return "\n".join(lines)


@mcp.tool()
def voice_transcripts(who: str, limit: int = 500, days: float = 0) -> str:
    """只取某个会话里「语音」的转写文字，按时间正序——用来给别的微信索引补语音。

    别人的索引（比如 im-mcp）把语音一律存成「[语音]」，因为转写藏在
    packed_info_data 里没人解。这个工具只挑语音出来，比重读整个会话便宜得多。

    参数：
      who   — 昵称 / 备注名 / 群名 / wxid，支持片段模糊匹配
      limit — 最多取多少条语音（从最近的往前取）
      days  — 只看这段时间内的；0 表示不限
    """
    _ensure_data()
    s = store()
    us = s._find_username(who)
    if len(us) > 1:
        shows = [f"  - {s.display(u)}（{u}）" for u in us[:8]]
        return f"「{who}」匹配到 {len(us)} 个会话，请说得更具体：\n" + "\n".join(shows)
    if not us:
        return f"没找到「{who}」的会话。可以先用 list_recent_chats 看看都有谁。"

    meta, items = s.voice_transcripts(who, limit=limit, days=days or None)
    if not items:
        return f"{meta['name']} 在这段时间内没有语音消息。"

    head = (f"=== {meta['name']}（{meta['username']}）"
            f"语音 {meta['voices']} 条，其中有转写 {meta['with_transcript']} 条 ===")
    lines = [head]
    for it in items:
        sec = f"{it['seconds']:g}秒" if it["seconds"] else "时长未知"
        body = it["text"] or "（微信没做转写）"
        lines.append(f"{it['epoch']}  {it['time']}  {it['sender']}（{sec}）：{body}")
    if meta["with_transcript"] < meta["voices"]:
        lines.append(f"注：{meta['voices'] - meta['with_transcript']} 条微信本身没转写，"
                     "只能靠听——音频不在本地，取不到。")
    return "\n".join(lines)


@mcp.tool()
def search_chats(keyword: str, limit: int = 30, days: float = 0, chat: str = "") -> str:
    """在所有微信聊天里搜关键词（压缩存储的消息也会解开再搜）。

    参数：
      keyword — 要搜的文字，原样子串匹配（不支持正则）
      limit   — 返回条数上限
      days    — 只搜这段时间的；0 表示不限
      chat    — 限定在某个人/群，留空表示全库
    """
    _ensure_data()
    if not keyword.strip():
        return "关键词不能为空。"
    t0 = time.time()
    hits = store().search(keyword, limit=limit, days=days or None,
                          chat=chat or None)
    cost = time.time() - t0
    if not hits:
        return f"没搜到「{keyword}」（耗时 {cost:.1f}s）。"
    lines = [f"「{keyword}」命中 {len(hits)} 条（耗时 {cost:.1f}s）："]
    lines += [_fmt_msg(m, with_chat=True) for m in hits]
    return "\n".join(lines)


@mcp.tool()
def refresh_data() -> str:
    """重新从微信进程取密钥并解密消息库——看完新消息后需要刷新才看得到。

    要求微信正在运行（密钥只在内存里）。
    """
    global _store
    if _store is not None:              # 先放掉文件句柄，否则写不进去
        _store.close()
        _store = None
    t0 = time.time()
    try:
        ok, bad, note = wxcore.refresh()
    except RuntimeError as e:            # 明文目录是外部的 / 密钥扫不到
        return str(e)
    try:
        INDEX_CACHE.unlink(missing_ok=True)
    except OSError:
        pass
    msg = [f"解密完成：{len(ok)} 个库，耗时 {time.time()-t0:.1f}s{note}"]
    if bad:
        msg.append("失败：" + "；".join(bad))
    return "\n".join(msg)


@mcp.tool()
def clear_cache(keep_images: bool = False) -> str:
    """删掉这台电脑上由本工具生成的明文缓存（解密后的聊天库、索引、图片）。

    源头的微信数据库不动。删完下次 read_chat 前要再 refresh_data 一次（约十几秒）。

    参数：
      keep_images — True 时保留解出来的图片，只删聊天库和索引
    """
    global _store
    if _store is not None:
        _store.close()
        _store = None
    try:
        freed = wxcore.purge()
    except RuntimeError as e:
        return str(e)
    parts = [f"已删除明文聊天库，释放 {freed:.1f} MB"]
    for p in ([INDEX_CACHE] if INDEX_CACHE.exists() else []):
        try:
            p.unlink()
            parts.append("已删除会话索引")
        except OSError:
            pass
    if keep_images:
        parts.append("图片已保留")
    else:
        import shutil
        import wxquery
        if wxquery.IMG_CACHE.exists():
            shutil.rmtree(wxquery.IMG_CACHE, ignore_errors=True)
            parts.append("已删除解出来的图片")
    return "；".join(parts) + "。"


if __name__ == "__main__":
    mcp.run()
