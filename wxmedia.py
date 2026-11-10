# -*- coding: utf-8 -*-
"""微信消息里的富媒体解析：语音转写、文件定位、转发集录、图片 .dat 解密。

只依赖标准库 + pycryptodome；不联网、不 print。
"""
import glob
import hashlib
import html
import re
import shutil
import struct
from pathlib import Path

from Crypto.Cipher import AES

import wxcore

# 微信账号目录（里面有 msg\attach、msg\file）——由 wxcore 自动探测
WX_ROOT = wxcore.ACCOUNT_DIR
DAT_MAGIC = b"\x07\x08V2\x08\x07"

# 图片密钥存在运行期目录里，不放在源码目录（免得被 git 收走）
IMG_KEY_FILE = wxcore.APP_HOME / "imgkey.txt"
_LEGACY_KEY = Path(__file__).with_name("_imgkey.txt")
if _LEGACY_KEY.exists() and not IMG_KEY_FILE.exists():
    try:                                   # 老版本留下的密钥，搬过来
        IMG_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(_LEGACY_KEY), str(IMG_KEY_FILE))
    except OSError:
        pass

# ---------------------------------------------------------------- XML 解析
TAG = lambda name: re.compile(rf"<{name}[^>]*>(.*?)</{name}>", re.S)
TITLE = TAG("title")
DES = TAG("des")
URL = TAG("url")
SUBTYPE = TAG("type")
TOTALLEN = TAG("totallen")
FILEEXT = TAG("fileext")
XMLMD5 = TAG("md5")
VOICELEN = re.compile(r'voicelength="(\d+)"')
RECORDITEM = re.compile(r"<recorditem><!\[CDATA\[(.*?)\]\]></recorditem>", re.S)
RECORD_INFO = re.compile(r"<info>(.*?)</info>", re.S)
DATAITEM = re.compile(r"<dataitem[^>]*>(.*?)</dataitem>", re.S)
DATATITLE = TAG("datatitle")
SOURCENAME = TAG("sourcename")
SOURCETIME = TAG("sourcetime")


def _txt(pattern, body, default=""):
    m = pattern.search(body)
    if not m:
        return default
    s = m.group(1).strip()
    if s.startswith("<![CDATA[") and s.endswith("]]>"):   # 有些字段整段是 CDATA
        s = s[9:-3]
    return html.unescape(s)


def parse_appmsg(body):
    """把 type=49 的 XML 拆成结构化字典。"""
    if "<appmsg" not in body:
        return {"kind": "other"}
    sub = _txt(SUBTYPE, body)
    out = {
        "kind": {"6": "file", "19": "record", "5": "link", "33": "miniprogram",
                 "36": "miniprogram", "57": "quote", "2000": "transfer",
                 "4": "link", "51": "video", "8": "sticker"}.get(sub, f"app{sub}"),
        "sub": sub,
        "title": _txt(TITLE, body),
        "des": _txt(DES, body),
        "url": _txt(URL, body),
    }
    if sub == "6":
        out["size"] = int(_txt(TOTALLEN, body, "0") or 0)
        out["ext"] = _txt(FILEEXT, body)
        out["md5"] = _txt(XMLMD5, body)
    if sub == "57":
        m = re.search(r"<refermsg>(.*?)</refermsg>", body, re.S)
        if m:
            blk = m.group(1)
            q = html.unescape(_txt(TAG("content"), blk))
            # 引用的是一条引用时，content 里又是一层 appmsg XML，往里挖到标题为止
            for _ in range(3):
                if "<appmsg" not in q:
                    break
                inner = TITLE.search(q)
                if not inner:
                    break
                q = html.unescape(inner.group(1).strip())
            out["quoted"] = q
            out["quoted_who"] = _txt(TAG("fromusr"), blk)
            out["quoted_type"] = _txt(TAG("type"), blk)
    if sub == "19":
        m = RECORDITEM.search(body) or re.search(
            r"<recorditem[^>]*>(.*?)</recorditem>", body, re.S)
        if m:
            inner = m.group(1).strip()
            if inner.startswith("<![CDATA["):
                inner = inner[9:-3]
            else:
                inner = html.unescape(inner)      # 有时不是 CDATA 而是转义过的
            info = RECORD_INFO.search(inner)
            out["lines"] = [ln.strip() for ln in info.group(1).splitlines()
                            if ln.strip()] if info else []
            items = []
            for it in DATAITEM.finditer(inner):
                blk = it.group(1)
                items.append({
                    "who": _txt(SOURCENAME, blk),
                    "time": _txt(SOURCETIME, blk),
                    "title": _txt(DATATITLE, blk),
                })
            out["items"] = items
    return out


# ---------------------------------------------------------------- 语音转写
def _varint(b, i):
    r = s = 0
    while i < len(b):
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if not c & 0x80:
            return r, i
        s += 7
    raise ValueError("bad varint")


def _fields(buf):
    i = 0
    while i < len(buf):
        tag, i = _varint(buf, i)
        f, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _varint(buf, i)
            yield f, v
        elif wt == 2:
            ln, i = _varint(buf, i)
            yield f, buf[i:i + ln]
            i += ln
        elif wt == 1:
            yield f, buf[i:i + 8]
            i += 8
        elif wt == 5:
            yield f, buf[i:i + 4]
            i += 4
        else:
            return


def voice_transcript(packed):
    """packed_info_data 里存着微信自己的语音转文字（字段 5 -> 2）。"""
    if not packed:
        return ""
    try:
        for f, v in _fields(packed):
            if f == 5 and isinstance(v, bytes):
                for f2, v2 in _fields(v):
                    if f2 == 2 and isinstance(v2, bytes):
                        return v2.decode("utf-8", "replace")
    except Exception:
        pass
    return ""


def voice_seconds(body):
    m = VOICELEN.search(body)
    return round(int(m.group(1)) / 1000, 1) if m else 0.0


# ---------------------------------------------------------------- 文件定位
class FileIndex:
    """把 msg/file/** 建成 文件名 -> [路径] 的索引，按需刷新。"""

    def __init__(self, root=None):
        root = root or WX_ROOT
        self.root = Path(root) / "msg" / "file" if root else None
        self._by_name = None

    def build(self):
        idx = {}
        if self.root and self.root.exists():
            for p in self.root.glob("*/*"):
                if p.is_file():
                    idx.setdefault(p.name, []).append(p)
        self._by_name = idx
        return idx

    def find(self, name, size=0, near_ts=0):
        """同名多份时，用大小和修改时间挑最接近的那份。"""
        if not name:
            return None
        idx = self._by_name if self._by_name is not None else self.build()
        cands = idx.get(name)
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]

        def score(p):
            try:
                st = p.stat()
            except OSError:
                return (2, 0)
            return (0 if (not size or st.st_size == size) else 1,
                    abs(st.st_mtime - near_ts) if near_ts else 0)
        return sorted(cands, key=score)[0]


# ---------------------------------------------------------------- 图片 .dat
IMG_MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"RIFF", b"GIF8", b"wxgf")


def keys():
    """读 _imgkey.txt，返回 {'aes': bytes|None, 'xor': int|None}。

    AES 键是 16 个 ASCII 字符（不是 16 字节二进制），文件里原样存着。
    XOR 键是单字节，存成 0xNN；写 auto 表示每张图自己推。
    """
    out = {"aes": None, "xor": None}
    try:
        for line in IMG_KEY_FILE.read_text(encoding="ascii").splitlines():
            k, _, v = line.partition("=")
            if k == "aes" and len(v) >= 16:
                out["aes"] = v[:16].encode("ascii")
            elif k == "xor" and v.startswith("0x"):
                out["xor"] = int(v, 16)
    except (OSError, ValueError):
        pass
    return out


def dat_key():
    return keys()["aes"]


def save_keys(aes_str, xor_k=None):
    xor = f"0x{xor_k:02x}" if xor_k is not None else "auto"
    IMG_KEY_FILE.write_text(f"aes={aes_str}\nxor={xor}", encoding="ascii")


def decode_dat(raw, aes_key, xor_key=None):
    """V2: [6B magic][4B AES长][4B XOR长][1B pad] + AES段 + 明文段 + XOR段

    aes_key 是 16 字节（由 16 个 ASCII 字符 encode 而来）。
    """
    if not aes_key or len(raw) < 32 or raw[:6] != DAT_MAGIC:
        return None
    aes_sz, xor_sz = struct.unpack_from("<II", raw, 6)
    off = 15
    aligned = aes_sz + (16 - aes_sz % 16) % 16
    seg = raw[off:off + aligned]
    if len(seg) < aligned:
        return None
    dec = AES.new(aes_key, AES.MODE_ECB).decrypt(seg)
    pad = dec[-1]
    if 1 <= pad <= 16 and dec[-pad:] == bytes([pad]) * pad:
        dec = dec[:-pad]
    off += aligned
    mid_len = len(raw) - off - xor_sz
    if mid_len < 0:
        return None
    mid = raw[off:off + mid_len]
    tail_raw = raw[off + mid_len:]
    # XOR 键：配置里存的 > 已知常见的 0xF3 > 按 JPEG 结尾反推
    cands = [xor_key, 0xF3, tail_raw[-1] ^ 0xD9 if tail_raw else None]
    for k in dict.fromkeys(c for c in cands if c is not None):
        out = dec + mid + bytes(x ^ k for x in tail_raw)
        if any(out.startswith(m) for m in IMG_MAGIC):
            return out
    return None


def plain_size(path):
    """算一个 .dat 解出来会有多少字节——不看密钥，只看头部三段长度。

    明文长 = AES段 + 明文段 + XOR段 - PKCS7 填充（aes_size 是 16 的倍数时总补 16 字节）。
    """
    try:
        st = Path(path).stat()
        with open(path, "rb") as fp:
            head = fp.read(15)
    except OSError:
        return None
    if head[:6] != DAT_MAGIC:
        return None
    aes_sz, xor_sz = struct.unpack_from("<II", head, 6)
    aligned = aes_sz + (16 - aes_sz % 16) % 16
    mid = st.st_size - 15 - aligned - xor_sz
    return aes_sz + mid + xor_sz - 16 if mid >= 0 else None


class ImageIndex:
    """按「明文大小」把消息和 .dat 对上。

    消息 XML 里的 length 就是解出来多少字节，而 .dat 的大小能从头部算出来，
    两边一比就配上了（实测 23/25 命中）。磁盘文件名不是消息里的 md5，别按 md5 找。
    """

    def __init__(self, root=None):
        root = root or WX_ROOT
        self.root = Path(root) / "msg" / "attach" if root else None
        self._cache = {}

    def _build(self, chat):
        idx = {}
        if not self.root:
            return idx
        d = self.root / hashlib.md5(chat.encode("utf-8")).hexdigest()
        for p in d.glob("*/Img/*.dat"):
            n = plain_size(p)
            if n is not None:
                idx.setdefault(n, []).append(p)
        return idx

    def find(self, chat, size):
        if not chat or not size or not self.root:
            return []
        if chat not in self._cache:
            self._cache[chat] = self._build(chat)
        hits = self._cache[chat].get(size) or []
        # 缩略图排后面：完整图优先，没有完整的再退而求其次
        return sorted(hits, key=lambda p: p.name.endswith("_t.dat"))


def thumb_jpg(name):
    """cache 里未加密的缩略图。"""
    stem = Path(name).stem
    if stem.endswith("_t"):
        stem = stem[:-2]
    if not WX_ROOT:
        return []
    return list((Path(WX_ROOT) / "cache").glob(f"*/Message/*/Thumb/{stem}*"))
