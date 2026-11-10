# -*- coding: utf-8 -*-
"""从微信进程内存里取 V2 图片密钥（.dat 的 AES key + XOR key）。

为什么要单独跑：微信 4.x 的 V2 格式图片没有固定密钥，AES key 是 16 个 ASCII
字符，只在微信解码图片那一下才进内存。所以顺序是——
    1. 在微信里点开两三张图片（点成大图看一眼就够）
    2. 立刻运行： python find_image_key.py
取到之后会写进 _imgkey.txt，wxmedia 读它来解图片。

用法：
    python find_image_key.py            # 找一次
    python find_image_key.py --watch    # 一直盯着，你什么时候点图就什么时候抓
    python find_image_key.py --check    # 只用已存的 key 验一下还能不能用
"""
import ctypes
import glob
import io
import os
import re
import struct
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from Crypto.Cipher import AES

from wxcore import MBI, _chunks, _weixin_pids, k32, MEM_COMMIT
import wxmedia

MAGIC = b"\x07\x08V2\x08\x07"
HERE = wxmedia.IMG_KEY_FILE
RE32 = re.compile(rb"(?<![A-Za-z0-9])[A-Za-z0-9]{32}(?![A-Za-z0-9])")
RE16 = re.compile(rb"(?<![A-Za-z0-9])[A-Za-z0-9]{16}(?![A-Za-z0-9])")

PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100

# 解出来开头该是什么：JPEG / PNG / WEBP / GIF / 微信自研格式
HEADS = (b"\xFF\xD8\xFF", b"\x89PNG", b"RIFF", b"GIF8", b"wxgf")


# ------------------------------------------------------------------ 样本
def pick_sample():
    """拿一张最近的缩略图 .dat 当样本，返回 (密文 16 字节, 路径)。"""
    if not wxmedia.WX_ROOT:
        return None, None
    pat = str(Path(wxmedia.WX_ROOT) / "msg" / "attach" / "*" / "*" / "Img" / "*_t.dat")
    files = sorted(glob.glob(pat), key=os.path.getmtime, reverse=True)
    for f in files[:200]:
        try:
            with open(f, "rb") as fp:
                head = fp.read(31)
        except OSError:
            continue
        if head[:6] == MAGIC and len(head) >= 31:
            return head[15:31], f
    return None, None


def xor_key(attach_root=None):
    """缩略图尾字节众数推 XOR 键（JPEG 结尾 FF D9）。"""
    if not attach_root and not wxmedia.WX_ROOT:
        return None
    root = Path(attach_root or (Path(wxmedia.WX_ROOT) / "msg" / "attach"))
    files = sorted(glob.glob(str(root / "*" / "*" / "Img" / "*_t.dat")),
                   key=os.path.getmtime, reverse=True)[:32]
    votes = {}
    for f in files:
        try:
            size = os.path.getsize(f)
            with open(f, "rb") as fp:
                if fp.read(6) != MAGIC:
                    continue
                fp.seek(size - 2)
                a, b = fp.read(2)
        except OSError:
            continue
        votes[(a, b)] = votes.get((a, b), 0) + 1
    if not votes:
        return None
    a, b = max(votes, key=votes.get)
    k = a ^ 0xFF
    return k if k == (b ^ 0xD9) else None


# ------------------------------------------------------------------ 试钥匙
def try_key(key, ct):
    try:
        dec = AES.new(key, AES.MODE_ECB).decrypt(ct)
    except Exception:
        return None
    for h in HEADS:
        if dec.startswith(h):
            return h
    return None


RW_PROTECT = (0x04, 0x08, 0x40, 0x80)      # READWRITE / WRITECOPY / EXECUTE_*


def scan(pid, ct, verbose=True):
    """扫一个进程，返回 (key 字符串, 命中的格式) 或 (None, None)。

    先扫可写区域——key 是普通字符串，几乎总在堆上。
    """
    h = k32.OpenProcess(0x10 | 0x400, False, pid)      # VM_READ | QUERY_INFORMATION
    if not h:
        if verbose:
            print(f"  打不开进程 {pid}（可能需要管理员权限）")
        return None, None
    try:
        mbi = MBI()
        addr, rw, other = 0, [], []
        while addr < 1 << 47:
            if k32.VirtualQueryEx(h, addr, ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
                break
            if (mbi.State == MEM_COMMIT and mbi.Protect != PAGE_NOACCESS
                    and not mbi.Protect & PAGE_GUARD
                    and 0 < mbi.RegionSize <= 1 << 30):
                reg = (mbi.BaseAddress, mbi.RegionSize)
                (rw if mbi.Protect in RW_PROTECT else other).append(reg)
            nxt = mbi.BaseAddress + mbi.RegionSize
            if nxt <= addr:
                break
            addr = nxt

        for tag, regs in (("可写", rw), ("其余", other)):
            t0, n32, n16 = time.time(), 0, 0
            for i, (_base, data) in enumerate(_chunks(h, regs, chunk=4 << 20)):
                for m in RE32.finditer(data):
                    n32 += 1
                    s = m.group()
                    fmt = try_key(s[:16], ct)
                    if fmt:
                        return s.decode("ascii")[:16], fmt
                    fmt = try_key(s, ct)                # 也可能直接是 AES-256
                    if fmt:
                        return s.decode("ascii"), fmt
                for m in RE16.finditer(data):
                    n16 += 1
                    if try_key(m.group(), ct):
                        return m.group().decode("ascii"), "16"
            if verbose:
                print(f"    {tag}区扫完：32字符候选 {n32}，16字符候选 {n16}，"
                      f"{time.time()-t0:.0f}s")
        return None, None
    finally:
        k32.CloseHandle(h)


# ------------------------------------------------------------------ 存取
def save(aes_str, xor_k):
    HERE.write_text(f"aes={aes_str}\nxor=" + (f"0x{xor_k:02x}" if xor_k is not None else "auto"),
                    encoding="ascii")


def check():
    """用存下来的 key 试解最近的缩略图，返回是否可用。"""
    ct, f = pick_sample()
    if not ct:
        print("找不到 V2 格式的 .dat，没法验证。")
        return False
    k = wxmedia.keys()
    if not k["aes"]:
        print("还没有存 key。")
        return False
    fmt = try_key(k["aes"], ct)
    print(f"样本 {Path(f).name}：{'可用 ' + str(fmt) if fmt else '不可用（微信重启过的话要重取）'}")
    return bool(fmt)


# ------------------------------------------------------------------ 自检
def selftest():
    """造一个 V2 .dat 再解回来——验格式推断对不对，不需要真密钥。"""
    from PIL import Image

    buf = io.BytesIO()
    img = Image.new("RGB", (320, 240))
    img.putdata([((x * 7) % 256, (y * 5) % 256, (x * y) % 256)
                 for x in range(320) for y in range(240)])
    img.save(buf, "JPEG", quality=90)      # 要足够大，得盖过 1024 字节的 AES 段
    jpg = buf.getvalue()
    assert len(jpg) > 1500, f"测试图太小只有 {len(jpg)} 字节"

    key_str = "AbCdEfGh12345678"           # 16 个 ASCII 字符，形状和微信的一致
    aes = key_str.encode("ascii")
    aes_sz = 1024                          # 头部这个数就是密文长度（和真样本一致）
    plain_len = aes_sz - 16                # PKCS7 一定补满一块，所以明文是 1008
    ct = AES.new(aes, AES.MODE_ECB).encrypt(jpg[:plain_len] + bytes([16]) * 16)
    mid, tail = jpg[plain_len:plain_len + 16], jpg[plain_len + 16:]
    raw = (MAGIC + struct.pack("<II", aes_sz, len(tail)) + b"\x01"
           + ct + mid + bytes(b ^ 0xF3 for b in tail))

    out = wxmedia.decode_dat(raw, aes, 0xF3)
    cases = [
        ("原样还原", out == jpg),
        ("认得出是 JPEG", out is not None and out.startswith(b"\xff\xd8\xff")),
        ("没给 XOR 键也能解", (wxmedia.decode_dat(raw, aes, None) or b"") == jpg),
        ("换把错钥匙要失败", wxmedia.decode_dat(raw, b"FFFFFFFFFFFFFFFF", 0xF3) is None),
    ]
    ok = True
    for name, passed in cases:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
        ok &= passed
    if ok:
        Image.open(io.BytesIO(out)).load()      # 再让图像库真解一遍
        print("  PASS  图像库能完整解码")
    print("自检", "通过" if ok else "失败")
    return ok


# ------------------------------------------------------------------ 主流程
def once(quiet=False):
    ct, f = pick_sample()
    if not ct:
        print("没找到 V2 格式的 .dat，说明本机没有图片缓存。")
        return False
    if not quiet:
        print(f"样本：{Path(f).name}")
    xk = xor_key()
    if not quiet:
        print(f"XOR 键：{'0x%02x' % xk if xk is not None else '推不出来（不影响取 AES 键）'}")

    pids = _weixin_pids()
    if not pids:
        print("微信没在运行。")
        return False
    if not quiet:
        print(f"微信进程：{pids}")
    for pid in pids:
        if not quiet:
            print(f"扫 PID {pid} …")
        key, fmt = scan(pid, ct, verbose=not quiet)
        if key:
            save(key, xk)
            print(f"\n取到了：AES key = {key}（样本开头是 {fmt}）")
            print(f"已写入 {HERE.name}，现在 read_chat 就能解图片了。")
            return True
    return False


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        sys.exit(0 if selftest() else 1)
    if "--check" in args:
        sys.exit(0 if check() else 1)
    if "--watch" in args:
        print("盯着微信内存……现在去微信里点开两三张图片（点成大图）。Ctrl+C 结束。")
        while True:
            if once(quiet=True):
                return
            time.sleep(2)
    if once():
        return
    print("\n没取到。按这个顺序再试一次：")
    print("  1. 确认微信开着")
    print("  2. 在微信里点开两三张图片，看到大图后关掉")
    print("  3. 5 秒内重新运行： python find_image_key.py")
    print("  或者干脆 --watch，一边点图一边等它抓。")
    sys.exit(1)


if __name__ == "__main__":
    main()
