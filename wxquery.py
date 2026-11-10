# -*- coding: utf-8 -*-
"""在解密后的微信库上做查询：会话索引 / 关键词搜索 / 看某人的消息。

所有函数只返回数据，不 print。
"""
import hashlib
import html
import json
import re
import sqlite3
import time
from pathlib import Path

import zstandard as zstd

import wxcore
import wxmedia

from wxcore import DEC_DIR

MY_WXID = wxcore.account_wxid()                        # 账号目录名去掉尾部 hash
INDEX_CACHE = wxcore.APP_HOME / "index.json"           # 会话索引缓存
IMG_CACHE = wxcore.APP_HOME / "images"                 # 解出来的图片放这

TYPE = {1: "文本", 2: "文本", 3: "图片", 34: "语音", 42: "名片", 43: "视频",
        47: "表情", 48: "位置", 49: "卡片", 50: "通话", 10000: "系统",
        10002: "系统", 10003: "系统", 10004: "系统"}
TLD = re.compile(r"<title>(.*?)</title>", re.S)
DES = re.compile(r"<des>(.*?)</des>", re.S)
# 群消息正文形如 "wxid_xxx:\n真正内容"，前缀是发送者 wxid
SENDER_PREFIX = re.compile(r"^(wxid_[0-9A-Za-z]+|[0-9]+@chatroom|filehelper):\n")
ZSTD = zstd.ZstdDecompressor()


# ------------------------------------------------------------------ 小工具
def md5name(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def unzstd(v):
    if isinstance(v, (bytes, bytearray)) and v:
        try:
            return ZSTD.decompress(v, max_output_size=1 << 24)
        except Exception:
            try:
                return ZSTD.decompressobj().decompress(v)
            except Exception:
                return v
    return v


def to_text(v, ct):
    if isinstance(v, (bytes, bytearray)):
        v = unzstd(v) if ct == 4 else v
        for enc in ("utf-8", "gbk"):
            try:
                return v.decode(enc)
            except Exception:
                pass
        return v.hex()[:80]
    return v if v is not None else ""


def ts(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t or 0))


def _plain(s):
    """系统提示里常夹着 CDATA 和微信自造的内联标签，剥成纯文字。"""
    s = (s or "").strip()
    if s.startswith("<![CDATA[") and s.endswith("]]>"):
        s = s[9:-3]
    s = re.sub(r"<[^>\n]{1,400}>", "", s)       # <_wc_custom_link_ href="...">
    return html.unescape(s).strip()


# ------------------------------------------------------------------ Store
class Store:
    def __init__(self, dec_dir=DEC_DIR):
        self.dir = Path(dec_dir)
        self._conn = {}
        self._contacts = None
        self._user2table = None
        self._index = None

    # ---------- 连接
    def conn(self, path):
        p = str(path)
        if p not in self._conn:
            self._conn[p] = sqlite3.connect(f"file:{p}?mode=ro", uri=True,
                                            check_same_thread=False)
        return self._conn[p]

    def close(self):
        """刷新前必须调用：Windows 上文件被连接占着就写不进去。"""
        for c in self._conn.values():
            try:
                c.close()
            except Exception:
                pass
        self._conn.clear()

    def message_dbs(self):
        return sorted((self.dir / "message").glob("message_*.db"))

    # ---------- 通讯录
    def contacts(self):
        """username -> 显示名（备注 > 昵称 > 微信号）"""
        if self._contacts is not None:
            return self._contacts
        out = {}
        p = self.dir / "contact" / "contact.db"
        if p.exists():
            con = self.conn(p)
            for u, remark, nick, alias in con.execute(
                    "SELECT username, remark, nick_name, alias FROM contact"):
                if not u:
                    continue
                out[u] = (remark or nick or alias or u).strip()
            for (u,) in con.execute("SELECT username FROM chat_room"):
                if u and u not in out:
                    out[u] = u
        self._contacts = out
        return out

    def display(self, username):
        if username == MY_WXID:
            return "我"
        c = self.contacts()
        return c.get(username) or username

    # ---------- 会话索引
    def index(self):
        """[{db, table, username, count, last}]，按最后时间倒序"""
        if self._index is not None:
            return self._index
        db_files = self.message_dbs()
        stamp = {p.name: [p.stat().st_mtime, p.stat().st_size] for p in db_files}
        if INDEX_CACHE.exists():
            try:
                cache = json.loads(INDEX_CACHE.read_text(encoding="utf-8"))
                if cache.get("stamp") == stamp:
                    self._index = cache["items"]
                    return self._index
            except Exception:
                pass

        # md5 -> username 反查表
        u2t = {}
        for u in list(self.contacts()) + [MY_WXID]:
            u2t[md5name(u)] = u
        self._user2table = u2t

        items = []
        for p in db_files:
            md5s = None
            try:
                con = self.conn(p)
                md5s = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
                for t in md5s:
                    n, mx = con.execute(
                        f'SELECT COUNT(*), MAX(create_time) FROM "{t}"').fetchone()
                    if n:
                        items.append({"db": p.name, "table": t,
                                      "username": u2t.get(t[4:], ""),
                                      "count": n, "last": mx or 0})
            except sqlite3.Error:
                continue
        items.sort(key=lambda x: x["last"], reverse=True)
        self._index = items
        try:
            INDEX_CACHE.write_text(json.dumps({"stamp": stamp, "items": items},
                                              ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return items

    def _table_for(self, username):
        m = md5name(username)
        for it in self.index():
            if it["table"] == "Msg_" + m:
                return it
        return None

    def _find_username(self, who):
        """按昵称/备注/wxid 找会话：精确 > 前缀 > 包含"""
        c = self.contacts()
        cands = [u for u in c if who in (c[u], u)]
        if not cands:
            cands = [u for u in c if who in c[u] or who in u]
        # 只保留真有消息的
        have = {it["username"] for it in self.index() if it["username"]}
        cands = [u for u in cands if u in have]
        if not cands:
            return []
        # 精确匹配排前
        cands.sort(key=lambda u: (c[u] != who and u != who, -len(c.get(u, u))))
        return cands

    # ---------- 读消息
    def _rows(self, it, limit, since=None, before=None, offset=0):
        p = self.dir / "message" / it["db"]
        con = self.conn(p)
        where = []
        args = []
        if since:
            where.append("create_time >= ?")
            args.append(since)
        if before:
            where.append("create_time < ?")
            args.append(before)
        sql = f'SELECT rowid, local_type, create_time, real_sender_id, message_content, ' \
              f'compress_content, WCDB_CT_message_content, packed_info_data ' \
              f'FROM "{it["table"]}"'
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY create_time DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        return list(con.execute(sql, args))

    def _sender(self, it, rid):
        p = self.dir / "message" / it["db"]
        try:
            r = self.conn(p).execute(
                "SELECT user_name FROM Name2Id WHERE rowid=?", (rid,)).fetchone()
            return r[0] if r else str(rid)
        except sqlite3.Error:
            return str(rid)

    def _render(self, it, row):
        row = list(row) + [b""] * (8 - len(row))
        _, raw_lt, ct_, rid, mc, cc, ctype, packed = row[:8]
        lt = raw_lt & 0xFFFFFFFF        # 高 32 位存来源编号，低 32 位才是消息类型
        body = to_text(mc if mc not in (None, "", b"") else cc, ctype)
        m = SENDER_PREFIX.match(body)
        if m:                            # 群消息正文带 "wxid_xxx:\n" 前缀
            body = body[m.end():]
            prefix = m.group(1)
        else:
            prefix = None
        if body.startswith("<![CDATA[") and body.rstrip().endswith("]]>"):
            body = body.strip()[9:-3]        # 有些系统消息整条被 CDATA 包着
        label = TYPE.get(lt, str(lt))
        text = body.strip()
        extra = {}

        vm = re.search(r"通话时长 ([\d:]+)", body) or \
            re.search(r"Duration: ([\d:]+)", body)
        if "<sysmsg" in body:                    # 撤回/拍一拍/转账提醒之类的系统提示
            c = re.search(r"<content>(.*?)</content>", body, re.S)
            text = _plain(c.group(1)) if c else _plain(body)
            label = "系统"
        elif vm:                                 # 语音/视频通话气泡
            text, label = f"通话 {vm.group(1)}", "通话"
        elif lt == 34:                           # 语音：取微信自己的转写
            sec = wxmedia.voice_seconds(body)
            tr = wxmedia.voice_transcript(packed)
            label = "语音"
            extra["seconds"] = sec
            text = tr if tr else "没有转写"      # 秒数由调用方加在括号里
        elif lt == 3:                            # 图片
            text, label, extra = self._image(body, it["username"]), "图片", {}
        elif lt == 43:                           # 视频
            text, label = "视频", "视频"
        elif lt == 47:
            text, label = "[表情]", "表情"
        elif lt == 49:                           # 卡片：文件/集录/链接/小程序
            a = wxmedia.parse_appmsg(body)
            label = {"file": "文件", "record": "集录", "link": "链接",
                     "miniprogram": "小程序", "quote": "引用",
                     "transfer": "转账"}.get(a["kind"], "卡片")
            text = self._appmsg(a, ct_)
            extra = {"sub": a.get("sub")}
        elif lt in (10000, 10002, 10003, 10004) and "<" in body:
            label = "系统"                        # 系统提示，尽量剥掉标签
            c = re.search(r"<(?:content|title)>(.*?)</(?:content|title)>", body, re.S)
            text = _plain(c.group(1)) if c else _plain(body)
        who = self._sender(it, rid) or prefix or ""
        out = {
            "time": ts(ct_),
            "sender": self.display(who) if who else "未知",
            "type": label,
            "text": text[:600],
        }
        out.update(extra)
        return out

    def _image(self, body, chat=""):
        """图片：按明文大小找到 .dat，有密钥就解出来存成 jpg。

        先试原图（`length`），再试缩略图（`cdnthumblength`）。原图若是微信
        自己的 `wxgf` 长图格式就跳过、退到缩略图——缩略图一定是普通 JPEG。
        """
        idx = self._imgidx()
        sizes = []
        for pat in (r'(?:^|\s)length="(\d+)"', r'cdnthumblength="(\d+)"'):
            m = re.search(pat, body)
            if m:
                sizes.append(int(m.group(1)))

        k = wxmedia.keys()
        wxgf = None
        for size in sizes:
            for p in idx.find(chat, size):
                out = None
                if k["aes"]:
                    try:
                        out = wxmedia.decode_dat(Path(p).read_bytes(),
                                                 k["aes"], k["xor"])
                    except Exception:
                        out = None
                if not out:
                    continue
                if out.startswith(b"wxgf"):        # 微信自有格式，解不出画面
                    wxgf = wxgf or (p, out)
                    continue
                # 文件名带上会话前缀：不同会话可能有同名的 .dat，否则会互相覆盖
                dst = IMG_CACHE / (md5name(chat)[:8] + "_" + Path(p).stem + ".jpg")
                if not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(out)
                return f"[图片] {dst}"

        h = re.search(r'\bmd5="([0-9a-f]{32})"', body)
        if h:
            for p in wxmedia.thumb_jpg(f"{h.group(1)}_t.dat"):
                return f"[图片] {p}"

        if wxgf:
            p, out = wxgf
            dst = IMG_CACHE / (md5name(chat)[:8] + "_" + Path(p).stem + ".wxgf")
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(out)
            return f"[图片]（微信自有的 wxgf 长图格式，看得见的是它没缩略图）{dst}"
        if not k["aes"]:
            return "[图片]（还没取图片密钥，见 README）"
        return "[图片]（本机没有这张的原文件）"

    def _imgidx(self):
        if not hasattr(self, "_img_index"):
            self._img_index = wxmedia.ImageIndex()
        return self._img_index

    def _appmsg(self, a, msg_ts):
        if a["kind"] == "file":
            size = a.get("size") or 0
            p = self._files().find(a["title"], size, msg_ts)
            head = f"[文件] {a['title']}"
            if size:
                head += f"（{size/1048576:.1f} MB）"
            return f"{head}\n  本地：{p}" if p else f"{head}\n  本地没有（没下载到这台电脑）"
        if a["kind"] == "record":
            lines = a.get("lines") or []
            if not lines:                       # 没有 info 块就用明细拼
                lines = [f"{it['who']}: {it['title']}".strip(": ")
                         for it in (a.get("items") or []) if it.get("title")]
            head = f"[集录] {a.get('title') or '聊天记录'}，共 {len(lines)} 条"
            return head + ("\n" + "\n".join("  " + ln for ln in lines[:40]) if lines else "")
        if a["kind"] == "link":
            return f"[链接] {a.get('title')}\n  {a.get('url')}"
        if a["kind"] == "miniprogram":
            return f"[小程序] {a.get('title')}"
        if a["kind"] == "quote":
            who = self.display(a.get("quoted_who") or "") if a.get("quoted_who") else "对方"
            q = (a.get("quoted") or "").strip()[:300]
            return f"[引用] {a.get('title')}\n  原话（{who}）：{q}" if q else f"[引用] {a.get('title')}"
        cn = {"transfer": "转账", "video": "视频", "sticker": "表情"}.get(a["kind"], a["kind"])
        head = f"[{cn}] {a.get('title')}"
        des = (a.get("des") or "").strip()
        return f"{head}\n  {des[:200]}" if des else head

    def _files(self):
        if not hasattr(self, "_fileidx"):
            self._fileidx = wxmedia.FileIndex()
        return self._fileidx

    # ---------- 对外三个能力
    def recent_chats(self, limit=20, days=None):
        since = time.time() - days * 86400 if days else None
        out = []
        for it in self.index():
            if since and it["last"] < since:
                continue
            out.append({
                "name": self.display(it["username"]) if it["username"] else f"[{it['table'][4:16]}]",
                "username": it["username"],
                "count": it["count"],
                "last": ts(it["last"]),
            })
            if len(out) >= limit:
                break
        return out

    def chat_messages(self, who, limit=30, days=None, offset=0):
        us = self._find_username(who)
        if not us:
            return None, []
        u = us[0]
        it = self._table_for(u)
        since = time.time() - days * 86400 if days else None
        rows = self._rows(it, limit, since, offset=offset)
        return {"username": u, "name": self.display(u), "count": it["count"]}, \
            [self._render(it, r) for r in reversed(rows)]

    def voice_transcripts(self, who, limit=500, days=None):
        """只取语音消息的转写，供上层工具给别的库「打补丁」。

        返回 (meta, items)：meta 里带总条数和有转写的条数，
        items 每条是 {epoch, time, sender, seconds, text}，按时间正序。
        为什么要单独一个方法：别人的微信索引把语音统一存成「[语音]」，
        而转写只在 packed_info_data 里——只取语音比重读整个会话便宜得多。
        """
        us = self._find_username(who)
        if not us:
            return None, []
        u = us[0]
        it = self._table_for(u)
        if not it:
            return None, []
        where = ["(local_type & 4294967295) = 34"]     # 高 32 位是来源编号
        args = []
        if days:
            where.append("create_time >= ?")
            args.append(time.time() - days * 86400)
        sql = ('SELECT create_time, real_sender_id, message_content, compress_content, '
               'WCDB_CT_message_content, packed_info_data FROM "%s" WHERE %s '
               'ORDER BY create_time DESC LIMIT ?') % (it["table"], " AND ".join(where))
        args.append(limit)
        rows = list(self.conn(self.dir / "message" / it["db"]).execute(sql, args))

        items = []
        for ct_, rid, mc, cc, ctype, packed in rows:
            body = to_text(mc if mc not in (None, "", b"") else cc, ctype)
            items.append({
                "epoch": int(ct_ or 0),
                "time": ts(ct_),
                "sender": self.display(self._sender(it, rid)),
                "seconds": wxmedia.voice_seconds(body),
                "text": wxmedia.voice_transcript(packed),
            })
        items.reverse()
        meta = {
            "username": u,
            "name": self.display(u),
            "voices": len(items),
            "with_transcript": sum(1 for i in items if i["text"]),
        }
        return meta, items

    def search(self, keyword, limit=50, days=None, chat=None):
        """全库找关键词。压缩行解压后再比。

        先把所有命中按时间排好、截出前 limit 条，**最后才渲染**——
        否则一个聊天很多的群会把配额吃光，而且渲染图片很慢。
        """
        since = time.time() - days * 86400 if days else None
        targets = self.index()
        if chat:
            us = self._find_username(chat)
            if not us:
                return []
            targets = [it for it in targets if it["username"] == us[0]]

        cands = []                              # [(会话, 原始行)]
        for it in targets:
            if since and it["last"] < since:
                continue
            p = self.dir / "message" / it["db"]
            con = self.conn(p)
            sql = (f'SELECT rowid, local_type, create_time, real_sender_id, message_content, '
                   f'compress_content, WCDB_CT_message_content, packed_info_data '
                   f'FROM "{it["table"]}"')
            args = []
            if since:
                sql += " WHERE create_time >= ?"
                args.append(since)
            sql += " ORDER BY create_time DESC"
            try:
                rows = con.execute(sql, args)
            except sqlite3.Error:
                continue
            got = 0
            for row in rows:
                mc, cc, ctype = row[4], row[5], row[6]
                val = mc if mc not in (None, "", b"") else cc
                if ctype == 4:
                    val = to_text(val, 4)
                elif isinstance(val, (bytes, bytearray)):
                    val = to_text(val, 0)
                if keyword not in (val or ""):
                    continue
                cands.append((it, row))
                got += 1
                if got >= limit:                # 单个会话最多占满一整页
                    break

        cands.sort(key=lambda x: x[1][2], reverse=True)
        hits = []
        for it, row in cands[:limit]:
            d = self._render(it, row)
            d["chat"] = self.display(it["username"]) if it["username"] else it["table"]
            hits.append(d)
        return hits
