import asyncio

from services.pos.csv_replay import _main

if __name__ == "__main__":
    asyncio.run(_main())
