# relay_vless.py
# رله VLESS — منطق مطابق PXPanel اصلی (پاسخ VLESS روی اولین پکت دیتا)
# سازگار با AGN021G از طریق lazy import

import asyncio
import secrets
from datetime import datetime

from fastapi import WebSocket, WebSocketDisconnect

import logging

logger = logging.getLogger("AGN021G.relay")


def _M():
    import main as m
    return m


try:
    from speed_limit import throttle
except Exception:
    async def throttle(uuid, n):  # type: ignore
        return None


RELAY_BUF = 256 * 1024  # مثل PXPanel اصلی


def _ws_client_ip(ws: WebSocket) -> str:
    fwd = ws.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    real_ip = ws.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return ws.client.host if ws.client else "نامشخص"


async def parse_vless_header(chunk: bytes):
    if len(chunk) < 24:
        raise ValueError("chunk too small")
    pos = 1
    pos += 16
    addon_len = chunk[pos]
    pos += 1 + addon_len
    command = chunk[pos]
    pos += 1
    port = int.from_bytes(chunk[pos : pos + 2], "big")
    pos += 2
    addr_type = chunk[pos]
    pos += 1
    if addr_type == 1:
        address = ".".join(str(b) for b in chunk[pos : pos + 4])
        pos += 4
    elif addr_type == 2:
        dlen = chunk[pos]
        pos += 1
        address = chunk[pos : pos + dlen].decode("utf-8", errors="ignore")
        pos += dlen
    elif addr_type == 3:
        ab = chunk[pos : pos + 16]
        pos += 16
        address = ":".join(f"{ab[i]:02x}{ab[i + 1]:02x}" for i in range(0, 16, 2))
    else:
        raise ValueError(f"unknown addr type: {addr_type}")
    return command, address, port, chunk[pos:]


async def check_and_use(uid: str, n: int) -> bool:
    """مثل PXPanel: افزایش مصرف + چک فعال بودن."""
    m = _M()
    async with m.LINKS_LOCK:
        link = m.LINKS.get(uid)
        if link is None:
            return False
        if not m.is_link_allowed(link):
            return False
        link["used_bytes"] = int(link.get("used_bytes") or 0) + n
        m.stats["total_bytes"] = int(m.stats.get("total_bytes") or 0) + n
        try:
            m.hourly_traffic[m.now_ir().strftime("%H:00")] += n
        except Exception:
            pass
    return True


async def relay_ws_to_tcp(ws: WebSocket, writer: asyncio.StreamWriter, conn_id: str, uid: str):
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes") or (msg.get("text") or "").encode()
            if not data:
                continue
            if not await check_and_use(uid, len(data)):
                await ws.close(code=1008, reason="quota/disabled/unknown")
                break
            await throttle(uid, len(data))
            _M().stats["total_requests"] += 1
            try:
                _M().connections[conn_id]["bytes"] += len(data)
            except Exception:
                pass
            writer.write(data)
            if writer.transport.get_write_buffer_size() > RELAY_BUF:
                await writer.drain()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        try:
            writer.write_eof()
        except Exception:
            pass


async def relay_tcp_to_ws(ws: WebSocket, reader: asyncio.StreamReader, conn_id: str, uid: str):
    # منطق اصلی PXPanel: پاسخ VLESS (\x00\x00) فقط روی اولین پکت از مقصد
    first = True
    try:
        while True:
            data = await reader.read(RELAY_BUF)
            if not data:
                break
            if not await check_and_use(uid, len(data)):
                await ws.close(code=1008, reason="quota/disabled/unknown")
                break
            await throttle(uid, len(data))
            try:
                _M().connections[conn_id]["bytes"] += len(data)
            except Exception:
                pass
            payload = (b"\x00\x00" + data) if first else data
            first = False
            await ws.send_bytes(payload)
    except Exception:
        pass


def _resolve_link(uuid: str):
    link = _M().LINKS.get(uuid)
    if link is not None:
        return uuid, link
    compact = (uuid or "").replace("-", "")
    if compact and compact != uuid:
        for k, v in _M().LINKS.items():
            if (k or "").replace("-", "") == compact:
                return k, v
    return uuid, None


async def open_dual_stack(address: str, port: int, timeout: float = 10.0):
    """اتصال خروجی ساده مثل PXPanel + TCP_NODELAY."""
    import socket

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(address, port),
        timeout=timeout,
    )
    sock = writer.transport.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
    return reader, writer


async def websocket_tunnel(ws: WebSocket, uuid: str):
    """تونل VLESS/WS — دقیقاً مطابق جریان PXPanel."""
    await ws.accept()

    async with _M().LINKS_LOCK:
        uuid, link = _resolve_link(uuid)

    if link is None or not _M().is_link_allowed(link):
        logger.warning(f"🚫 WS rejected uuid={(uuid or '')[:12]}… (not allowed)")
        await ws.close(code=1008, reason="not authorized")
        return

    ip = _ws_client_ip(ws)

    if not _M().is_ip_allowed(link, uuid, ip):
        logger.warning(f"🚫 WS rejected uuid={uuid[:8]}… ip={ip} (ip limit)")
        try:
            _M().log_activity(
                "connection",
                f"اتصال {ip} به کانفیگ «{link.get('label', '?')}» رد شد (محدودیت آی‌پی)",
                "warn",
            )
        except Exception:
            pass
        await ws.close(code=1008, reason="ip limit reached")
        return

    # محدودیت تعداد اتصال همزمان (اگر ست شده)
    try:
        cl = int(link.get("connection_limit") or 0)
        if cl > 0:
            cur = sum(1 for c in _M().connections.values() if c.get("uuid") == uuid)
            if cur >= cl:
                await ws.close(code=1008, reason="connection limit")
                return
    except Exception:
        pass

    conn_id = secrets.token_urlsafe(6)
    _M().connections[conn_id] = {
        "uuid": uuid,
        "ip": ip,
        "transport": "vless-ws",
        "connected_at": datetime.now().isoformat(),
        "bytes": 0,
    }
    logger.info(f"✅ WS [{conn_id}] uuid={uuid[:8]}… ip={ip} total={len(_M().connections)}")
    try:
        _M().log_activity("connection", f"اتصال جدید از {ip} (کانفیگ {link.get('label', '?')})", "info")
    except Exception:
        pass

    writer = None
    try:
        first_msg = await asyncio.wait_for(ws.receive(), timeout=15.0)
        if first_msg["type"] == "websocket.disconnect":
            return
        first_chunk = first_msg.get("bytes") or (first_msg.get("text") or "").encode()
        if not first_chunk:
            return

        command, address, port, payload = await parse_vless_header(first_chunk)

        if not await check_and_use(uuid, len(first_chunk)):
            await ws.close(code=1008, reason="quota/disabled")
            return

        _M().stats["total_requests"] += 1
        _M().connections[conn_id]["bytes"] += len(first_chunk)
        logger.info(f"➡️  [{conn_id}] → {address}:{port}")

        # مثل PXPanel: open_connection ساده
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port),
            timeout=10.0,
        )
        sock = writer.transport.get_extra_info("socket")
        if sock:
            import socket as _socket
            try:
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
            except Exception:
                pass

        if payload:
            writer.write(payload)
            await writer.drain()

        done, pending = await asyncio.wait(
            {
                asyncio.create_task(relay_ws_to_tcp(ws, writer, conn_id, uuid)),
                asyncio.create_task(relay_tcp_to_ws(ws, reader, conn_id, uuid)),
            },
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

        try:
            asyncio.create_task(_M().save_state())
        except Exception:
            pass

    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        _M().stats["total_errors"] += 1
        try:
            _M().error_logs.append({"error": "connection timeout", "time": datetime.now().isoformat()})
        except Exception:
            pass
    except Exception as exc:
        _M().stats["total_errors"] += 1
        try:
            _M().error_logs.append({"error": str(exc), "time": datetime.now().isoformat()})
        except Exception:
            pass
        logger.error(f"WS error [{conn_id}]: {exc}")
    finally:
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        _M().connections.pop(conn_id, None)
        logger.info(f"🔌 WS closed [{conn_id}] total={len(_M().connections)}")
