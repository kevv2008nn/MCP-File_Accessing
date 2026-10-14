"""Start the platform:  python run.py   then open http://127.0.0.1:8000"""
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

import uvicorn  # noqa: E402  (must come after load_dotenv so host.app sees the env)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"\n  MCP Agent Platform -> http://127.0.0.1:{port}\n")
    # host is fixed to 127.0.0.1 ON PURPOSE: never expose an agent that can touch your files to the network.
    # No reload=True: on Windows it switches to an event loop that cannot launch the MCP subprocesses.
    uvicorn.run("host.app:app", host="127.0.0.1", port=port, log_level="info")
