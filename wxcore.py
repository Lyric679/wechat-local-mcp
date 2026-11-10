# -*- coding: utf-8 -*-
"""微信本地库访问内核：找目录 -> 从内存取密钥 -> SQLCipher 逐页解密。

设计原则
--------
1. 取密钥只读微信进程的内存（PROCESS_VM_READ），不写、不注入、不改对方的态
2. 解密的唯一判据是第 1 页的 HMAC —— 验不过的密钥一律丢掉，不靠猜
3. 所有对外函数只返回纯数据，不 print（MCP 走 stdout，任何 print 都会毁掉协议）
4. 只支持 Windows：要读另一个进程的内存

目录探测顺序（找 wxchat_files 根）
  环境变量 WECHAT_FILE_ROOT  >  ~/Documents/xwechat_files  >  ~/xwechat_files
  >  各盘符的 X:/xwechat_files
"""
import ctypes
import hashlib
import hmac as hmac_mod
import logging
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

from Crypto.Cipher import AES

IS_WINDOWS = sys.platform == "win32"

log = logging.getLogger("wxcore")

# ---------------------------------------------------------------- 运行期目录
def app_home():
    """解密产物、图片、索引、密钥放哪（默认不在源码目录里）。"""
    env = os.environ.get("WECHAT_MCP_HOME")
    if env:
        return Path(env)
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "wechat-mcp"
    return Path.home() / ".cache" / "wechat-mcp"


APP_HOME = app_home()
DEC_DIR = Path(os.environ.get("WECHAT_DEC_DIR") or (APP_HOME / "decrypted"))


# ---------------------------------------------------------------- 找微信目录
def find_wechat_root():
    """返回 xwechat_files 目录（里面有若干 <wxid>_<4hex> 账号目录）。"""
    cands = []
    env = os.environ.get("WECHAT_FILE_ROOT")
    if env:
        cands.append(Path(env))
    cands += [Path.home() / "Documents" / "xwechat_files",
              Path.home() / "xwechat_files"]
    if IS_WINDOWS:
        drives = (os.listdrives() if hasattr(os, "listdrives")
                  else [f"{c}:/" for c in "CDEFGH"])
        cands += [Path(d) / "xwechat_files" for d in drives]

    for p in cands:
        try:
            if not p.is_dir():
                continue
            if any((d / "db_storage").is_dir() for d in p.iterdir() if d.is_dir()):
                return p
        except OSError:
            continue
    return None


def find_account(root, wanted=None):
    """在根目录里挑一个账号目录（含 db_storage 的）。

    wanted 可以是完整目录名（wxid_xxx_7f3a）或裸 wxid；匹配不到时：
    只有一个账号就用它，多个账号用 db_storage 改动最新的那个。
    """
    if not root:
        return None
    try:
        dirs = [d for d in Path(root).iterdir()
                if d.is_dir() and (d / "db_storage").is_dir()]
    except OSError:
        return None
    if not dirs:
        return None
    wanted = wanted or os.environ.get("WECHAT_ACCOUNT")
    if wanted:
        for d in dirs:
            if d.name == wanted or d.name.rsplit("_", 1)[0] == wanted:
                return d
    if len(dirs) == 1:
        return dirs[0]
    try:
        return max(dirs, key=lambda d: (d / "db_storage").stat().st_mtime)
    except OSError:
        return dirs[0]


def account_wxid(account=None):
    """账号目录名 -> 裸 wxid（去掉尾部的 _<4位十六进制>）。"""
    d = account or ACCOUNT_DIR
    if not d:
        return ""
    name = Path(d).name
    m = re.fullmatch(r"(.+)_[0-9a-f]{4}", name)
    return m.group(1) if m else name


WECHAT_ROOT = find_wechat_root()
ACCOUNT_DIR = find_account(WECHAT_ROOT)
DB_STORAGE = (ACCOUNT_DIR / "db_storage") if ACCOUNT_DIR else None

PAGE, RESERVE, USABLE = 4096, 80, 4096 - 80
NEEDLE = b"com.Tencent.WCDB.Config.Cipher"
# 4.1.11+ 的固定 32 字节掩码；在 4.2.0.9 上实测未变
MASK = bytes.fromhex("d2c7442458020000004889442450488b"
                     "450048844c2448488944254048584c24")
BLOB_MAX = 1024
LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")

# ---------------------------------------------------------------- Win32
k32 = None
if IS_WINDOWS:
    import ctypes.wintypes as wt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    PROCESS_VM_READ, PROCESS_QUERY_INFORMATION = 0x0010, 0x0400
    MEM_COMMIT = 0x1000
    READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}
    MAX_ADDR = 0x0000_7FFF_FFFF_FFFF

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_uint64),
                    ("AllocationBase", ctypes.c_uint64),
                    ("AllocationProtect", wt.DWORD), ("_p1", wt.DWORD),
                    ("RegionSize", ctypes.c_uint64), ("State", wt.DWORD),
                    ("Protect", wt.DWORD), ("Type", wt.DWORD), ("_p2", wt.DWORD)]

    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_uint64,
                                   ctypes.POINTER(MBI), ctypes.c_size_t]
    k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_uint64, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    k32.CloseHandle.argtypes = [wt.HANDLE]


def _u64(b, off):
    return struct.unpack_from("<Q", b, off)[0]


def _read(h, addr, size):
    if size <= 0 or addr >= MAX_ADDR:
        return b""
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h, addr, buf, size, ctypes.byref(n)):
        return b""
    return buf.raw[:n.value]


def _regions(h):
    mbi = MBI()
    addr = 0
    while addr < MAX_ADDR:
        if k32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi),
                              ctypes.sizeof(mbi)) == 0:
            break
        if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE
                and 0 < mbi.RegionSize <= 1 << 30):
            yield mbi.BaseAddress, mbi.RegionSize
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt


def _chunks(h, regs, overlap=0, chunk=4 << 20):
    for base, size in regs:
        off = 0
        while off < size:
            n = min(chunk, size - off)
            data = _read(h, base + off, n)
            if data:
                yield base + off, data
            off += max(1, n - overlap)


def _weixin_pids():
    """微信（4.x 进程名 Weixin.exe；3.x 是 WeChat.exe）的进程号。"""
    if not IS_WINDOWS:
        return []
    pids = []
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for image in ("Weixin.exe", "WeChat.exe"):
        try:
            r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}",
                                "/FO", "CSV", "/NH"],
                               capture_output=True, timeout=15, creationflags=flags)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in r.stdout.decode("gbk", "replace").strip().splitlines():
            parts = line.strip().strip('"').split('","')
            if len(parts) >= 2 and parts[1].isdigit():
                pids.append(int(parts[1]))
    return pids


def _verify_key(enc_key, page1):
    salt = page1[:16]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)
    hh = hmac_mod.new(mac_key, page1[16:PAGE - 80 + 16], hashlib.sha512)
    hh.update(struct.pack("<I", 1))
    return hh.digest() == page1[PAGE - 64:PAGE]


# ---------------------------------------------------------------- 取密钥
def load_dbs(dbdir=None):
    """扫描库目录，返回 {相对路径: 第 1 页原始 4096 字节}"""
    dbdir = Path(dbdir or DB_STORAGE or "")
    out = {}
    if not dbdir.is_dir():
        return out
    for p in dbdir.rglob("*.db"):
        try:
            page1 = p.read_bytes()[:PAGE]
        except OSError:
            continue
        if len(page1) == PAGE:
            out[str(p.relative_to(dbdir))] = page1
    return out


def scan_keys(dbdir=None):
    """从微信进程内存里取密钥并逐个 HMAC 验真。

    返回 (found, total, opened)：
      found  — {相对路径: 密钥hex}
      total  — 扫到的库文件数
      opened — 成功打开并扫描过的进程数（0 说明没权限或微信没运行）
    """
    if not IS_WINDOWS:
        raise RuntimeError("取密钥要读另一个进程的内存，只能在 Windows 上跑。")
    dbs = load_dbs(dbdir)
    salts = {}
    for name, page1 in dbs.items():
        salts.setdefault(page1[:16], []).append((name, page1))

    found, opened = {}, 0
    for pid in _weixin_pids():
        h = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
        if not h:
            continue
        opened += 1
        try:
            regs = list(_regions(h))
            addrs = set()
            for base, data in _chunks(h, regs, overlap=len(NEEDLE) - 1):
                pos = data.find(NEEDLE)
                while pos >= 0:
                    addrs.add(base + pos)
                    pos = data.find(NEEDLE, pos + 1)
            if not addrs:
                continue
            pairs = [struct.pack("<QQ", a, len(NEEDLE)) for a in addrs]
            for base, data in _chunks(h, regs, overlap=0x80):
                for pat in pairs:
                    pos = data.find(pat)
                    while pos >= 0:
                        node = _read(h, base + pos - 0x10, 0x50)
                        if (len(node) >= 0x40 and _u64(node, 0x10) in addrs
                                and _u64(node, 0x18) == len(NEEDLE)):
                            cfg = _u64(node, 0x28)
                            if 0x10000 <= cfg < MAX_ADDR:
                                obj = _read(h, cfg + 0x88, 0x28)
                                if len(obj) >= 0x18:
                                    dptr, dlen = _u64(obj, 0x8), _u64(obj, 0x10)
                                    if 0 < dlen <= BLOB_MAX and 0x10000 <= dptr < MAX_ADDR:
                                        blob = _read(h, dptr, int(dlen))
                                        if len(blob) == dlen:
                                            dec = bytes(b ^ MASK[i % len(MASK)]
                                                        for i, b in enumerate(blob))
                                            for m in LITERAL_RE.finditer(dec):
                                                run = m.group(1).decode("ascii").lower()
                                                if len(run) < 96:
                                                    continue
                                                key = bytes.fromhex(run[:64])
                                                salt = bytes.fromhex(run[64:96])
                                                for name, page1 in salts.get(salt, []):
                                                    if name in found:
                                                        continue
                                                    if _verify_key(key, page1):
                                                        found[name] = key.hex()
                        pos = data.find(pat, pos + 1)
        finally:
            k32.CloseHandle(h)
    return found, len(dbs), opened


# ---------------------------------------------------------------- 解密
def decrypt_file(src, key_hex, dst, verify=True):
    """SQLCipher 4 逐页解密 -> 标准 SQLite。"""
    key = bytes.fromhex(key_hex)
    data = Path(src).read_bytes()
    pages = (len(data) + PAGE - 1) // PAGE
    out = bytearray()
    for i in range(pages):
        page = data[i * PAGE:(i + 1) * PAGE]
        if len(page) < PAGE:
            page += b"\x00" * (PAGE - len(page))
        iv = page[USABLE:USABLE + 16]
        if i == 0:
            pt = AES.new(key, AES.MODE_CBC, iv).decrypt(page[16:USABLE])
            out += b"SQLite format 3\x00" + pt
        else:
            pt = AES.new(key, AES.MODE_CBC, iv).decrypt(page[:USABLE])
            out += pt
        out += b"\x00" * RESERVE
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(out)

    if verify:                      # 解完再让 SQLite 自己读一遍，坏的当场发现
        con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        try:
            res = con.execute("PRAGMA integrity_check").fetchone()
        finally:
            con.close()
        if not res or res[0] != "ok":
            raise ValueError(f"解密产物校验不通过：{res[0] if res else '空'}")
    return pages


# 只解聊天正文、通讯录、会话列表；媒体库/公众号/全文索引都不解
WANTED = re.compile(r"^(?:message\\message_\d+\.db|contact\\contact\.db|session\\session\.db)$")


def refresh(dbdir=None, dec_dir=None, purge=True):
    """重取密钥 + 解密需要的库。返回 (成功列表, 失败列表, 说明)。

    purge=True 时先清空输出目录——否则上游删掉的库，明文会一直留在盘上。
    """
    dbdir = Path(dbdir or DB_STORAGE or "")
    dec_dir = Path(dec_dir or DEC_DIR)
    if not dbdir.is_dir():
        raise RuntimeError(
            "找不到微信数据库目录。请设置环境变量 WECHAT_FILE_ROOT 指向 "
            "xwechat_files 所在目录（默认会自动在 文档 / 各盘根目录下找）。")

    keys, total, opened = scan_keys(dbdir)
    if not keys:
        if opened == 0:
            raise RuntimeError(
                "打不开微信进程。请确认：① 微信正在运行；"
                "② 本程序与微信是同一个用户、且微信没以管理员身份运行。")
        raise RuntimeError(
            f"扫了 {opened} 个微信进程，{total} 个库一个密钥都没验过。"
            "多半是微信升级后内存掩码变了。")

    targets = [n for n in keys if WANTED.match(n)]
    if purge and dec_dir.exists():
        shutil.rmtree(dec_dir, ignore_errors=True)

    ok, bad = [], []
    for name in sorted(targets):
        try:
            decrypt_file(dbdir / name, keys[name], dec_dir / name)
            ok.append(name)
        except Exception as e:                      # 单个库失败不影响其它
            bad.append(f"{name}: {e}")
    log.info("refresh: %d/%d 个库取到密钥，解密 %d 个", len(keys), total, len(ok))
    note = "" if len(keys) >= total else f"（{total - len(keys)} 个库没取到密钥）"
    return ok, bad, note


def purge(dec_dir=None):
    """删掉所有解密产物。返回释放的 MB。"""
    dec_dir = Path(dec_dir or DEC_DIR)
    if not dec_dir.exists():
        return 0.0
    size = sum(p.stat().st_size for p in dec_dir.rglob("*") if p.is_file())
    shutil.rmtree(dec_dir, ignore_errors=True)
    return size / 1048576
