# -*- coding: utf-8 -*-
"""自检：真的用 MCP 客户端连上服务，把几个工具各调一遍。

用法：
    python selftest.py            # 用最近一个会话做测试
    python selftest.py 某人        # 指定会话
"""
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))


async def main():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable,
                                   args=[os.path.join(HERE, "server.py")], cwd=HERE)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("工具清单:")
            for t in tools.tools:
                print(f"  - {t.name}: {(t.description or '').splitlines()[0]}")

            async def call(name, args):
                print(f"\n>>> {name}({args})")
                res = await s.call_tool(name, args)
                for c in res.content:
                    print(getattr(c, "text", c)[:900])
                return res

            await call("list_recent_chats", {"limit": 5})

            # 拿最近一个会话当样本，不写死任何人名
            who = sys.argv[1] if len(sys.argv) > 1 else None
            if not who:
                res = await call("list_recent_chats", {"limit": 1})
                first = (getattr(res.content[0], "text", "") or "").splitlines()
                if len(first) > 1:
                    who = first[1].split("  ")[-1].strip()
            if who:
                await call("read_chat", {"who": who, "limit": 12})
                await call("search_chats", {"keyword": "的", "limit": 3, "chat": who})
            else:
                print("\n跳过 read_chat：没有任何会话。")

            # 连刷两次：第一次会占住文件句柄，第二次才暴露锁死问题
            await call("refresh_data", {})
            await call("refresh_data", {})
            await call("list_recent_chats", {"limit": 3})


if __name__ == "__main__":
    asyncio.run(main())
