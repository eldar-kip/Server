import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.scripts.seed_db import seed_database

if __name__ == "__main__":
    asyncio.run(seed_database())
