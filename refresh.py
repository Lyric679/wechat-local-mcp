# -*- coding: utf-8 -*-
"""重新取密钥并解密消息库。

用法（微信必须在运行）：
    python refresh.py
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import wxcore
from wxquery import INDEX_CACHE


def main():
    if "--purge" in sys.argv[1:]:
        freed = wxcore.purge()
        print(f"已删除解密产物，释放 {freed:.1f} MB")
        return 0
    print(f"微信根 : {wxcore.WECHAT_ROOT}")
    print(f"账号   : {wxcore.ACCOUNT_DIR}（{wxcore.account_wxid()}）")
    print(f"库目录 : {wxcore.DB_STORAGE}")
    print(f"输出   : {wxcore.DEC_DIR}")
    t0 = time.time()
    try:
        ok, bad, note = wxcore.refresh()
    except RuntimeError as e:
        print(f"\n[失败] {e}")
        return 1
    print(f"\n解密 {len(ok)} 个库，用时 {time.time()-t0:.1f}s{note}")
    for n in ok:
        p = wxcore.DEC_DIR / n
        print(f"  [OK] {n:<32} {p.stat().st_size/1048576:6.2f} MB")
    for n in bad:
        print(f"  [!!] {n}")
    if INDEX_CACHE.exists() and INDEX_CACHE.unlink() is None:
        print("已清掉旧索引缓存，下次查询自动重建。")
    return 0 if ok and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
