"""服务启动入口：

    python -m mdt_records [--db data/mdt.db] [--host 127.0.0.1] [--port 8080] [--seed]

使用 ``--seed`` 会写入开发用固定令牌（见 seed.py），仅用于院内开发/测试环境。
"""

from __future__ import annotations

import argparse

from .app import create_server
from .seed import seed


def main() -> None:
    parser = argparse.ArgumentParser(description="多学科决策记录 HTTP 服务")
    parser.add_argument("--db", default="data/mdt.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--seed", action="store_true", help="写入开发用种子用户与令牌")
    args = parser.parse_args()

    server, service, store = create_server(args.db, args.host, args.port)
    if args.seed:
        seed(store)
    print(f"MDT 决策记录服务已启动：http://{args.host}:{args.port}（数据库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        store.close()


if __name__ == "__main__":
    main()
