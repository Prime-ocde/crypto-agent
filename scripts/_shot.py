"""用 CDP 截一张稳定的页面图（开发自检用）。

`msedge --screenshot --virtual-time-budget` 会快进定时器，Streamlit 的 websocket
渲染还没完成就截图，结果时好时坏。这里改成真实等待 N 秒再抓屏。

用法：python scripts/_shot.py <url> <out.png> [wait_seconds]
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333


async def capture(url: str, out: Path, wait_s: float = 12.0, js: str = "", out2: Path | None = None, wait2: float = 10.0, post_js_wait: float = 3.5) -> None:
    import websockets

    profile = Path(tempfile.mkdtemp(prefix="edge-cdp-"))
    proc = subprocess.Popen(
        [
            EDGE,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={PORT}",
            "--window-size=1500,1350",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(50):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=1) as resp:
                    pages = [t for t in json.load(resp) if t.get("type") == "page"]
                if pages:
                    target = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2)
        if not target:
            raise RuntimeError("CDP 未就绪")

        async with websockets.connect(target, max_size=80 * 1024 * 1024, ping_interval=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
            ua = os.getenv("SHOT_UA")
            if ua:
                await ws.send(json.dumps({"id": 9, "method": "Network.enable"}))
                await ws.send(
                    json.dumps(
                        {
                            "id": 10,
                            "method": "Network.setUserAgentOverride",
                            "params": {"userAgent": ua},
                        }
                    )
                )
            await ws.send(json.dumps({"id": 2, "method": "Page.navigate", "params": {"url": url}}))
            await asyncio.sleep(wait_s)

            async def call(call_id: int, method: str, params: dict | None = None) -> dict:
                payload: dict = {"id": call_id, "method": method}
                if params is not None:
                    payload["params"] = params
                await ws.send(json.dumps(payload))
                while True:
                    message = json.loads(await ws.recv())
                    if message.get("id") == call_id:
                        return message

            click_text = os.getenv("SHOT_CLICK_TEXT")
            if click_text:
                expr = (
                    "(() => { const el=[...document.querySelectorAll('button,a,[role=button],[role=tab]')]"
                    f".find(x=>(x.innerText||'').includes({json.dumps(click_text)}));"
                    " if(!el) return null; const r=el.getBoundingClientRect();"
                    " return [r.left + r.width/2, r.top + r.height/2]; })()"
                )
                res = await call(20, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
                coords = (res.get("result") or {}).get("result", {}).get("value")
                if coords:
                    x, y = float(coords[0]), float(coords[1])
                    for kind in ("mousePressed", "mouseReleased"):
                        await call(
                            21,
                            "Input.dispatchMouseEvent",
                            {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1},
                        )
                    print(f"(已点击「{click_text}」@{x:.0f},{y:.0f})", flush=True)
                else:
                    print(f"(页面里找不到「{click_text}」)", flush=True)
                await asyncio.sleep(post_js_wait)

            if js:
                await ws.send(
                    json.dumps(
                        {
                            "id": 4,
                            "method": "Runtime.evaluate",
                            "params": {"expression": js, "returnByValue": True},
                        }
                    )
                )
                await asyncio.sleep(post_js_wait)
            await ws.send(json.dumps({"id": 3, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == 3:
                    out.write_bytes(base64.b64decode(message["result"]["data"]))
                    break
            if out2 is None:
                return
            await asyncio.sleep(wait2)
            await ws.send(json.dumps({"id": 5, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
            while True:
                message = json.loads(await ws.recv())
                if message.get("id") == 5:
                    out2.write_bytes(base64.b64decode(message["result"]["data"]))
                    return
    finally:
        proc.terminate()


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8502"
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "shot.png")
    wait = float(sys.argv[3]) if len(sys.argv) > 3 else 12.0
    js = sys.argv[4] if len(sys.argv) > 4 else ""
    out2 = Path(sys.argv[5]) if len(sys.argv) > 5 else None
    post_wait = float(sys.argv[6]) if len(sys.argv) > 6 else 3.5
    asyncio.run(capture(url, out, wait, js, out2, 10.0, post_wait))
    print(f"OK {out} ({out.stat().st_size} bytes)")
    if out2:
        print(f"OK {out2} ({out2.stat().st_size} bytes)")
