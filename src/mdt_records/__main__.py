"""本地运行入口：python3 -m src.mdt_records [db_path] [port]"""

from __future__ import annotations

import sys

from .server import create_server


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "mdt_records.db"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    server = create_server(db_path, host="127.0.0.1", port=port)
    actual = server.server_address[1]
    print(f"MDT 决策留痕服务已启动: http://127.0.0.1:{actual} (db={db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.service.store.close()
        server.server_close()


if __name__ == "__main__":
    main()
