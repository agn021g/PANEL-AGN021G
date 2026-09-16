# relay_vless.py
# بخش VLESS Relay — جدا شده از main.py (منطق اصلی دست‌نخورده)
# بهینه‌سازی سرعت: بافر بزرگ‌تر، حساب‌کتاب ترافیک دسته‌ای، قفل کمتر، TCP_NODELAY + بافر سوکت بزرگ

import asyncio
import secrets
from datetime import datetime

from fastapi import WebSocket, WebSocketDisconnect

from main import (
    LINKS,
    LINKS_LOCK,
    stats,
    hourly_traffic,
    connections,
    error_logs,
    logger,
    is_link_allowed,
    is_link_expired,
    is_ip_allowed,
    save_state,
    log_activity,
    now_ir,
)
from speed_limit import throttle

# ══════════════════════════════════════════════════════════════════════════════
# VLESS Relay — بهینه‌شده برای حداکثر throughput
# ══════════════════════════════════════════════════════════════════════════════

RELAY_BUF = 1024 * 1024          # 1 MB read/write chunks
SOCK_BUF = 4 * 1024 * 1024       # 4 MB kernel socket buffers
_TRAFFIC_SAVE_EVERY = 8 * 1024 * 1024  # persist every ~8 MB (کمتر I/O روی دیسک)
QUOTA_BATCH = 256 * 1024         # حساب‌کتاب هر ~256KB یک‌بار (به جای هر پکت)


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
    port = int.from_bytes(chunk[pos:pos + 2], "big")
    pos += 2
    addr_type = chunk[pos]
    pos += 1
    if addr_type == 1:
        address = ".".join(str(b) for b in chunk[pos:pos + 4])
        pos += 4
    elif addr_type == 2:
        dlen = chunk[pos]
        pos += 1
        address = chunk[pos:pos + dlen].decode("utf-8", errors="ignore")
        pos += dlen
    elif addr_type == 3:
        ab = chunk[pos:pos + 16]
        pos += 16
        address = ":".join(f"{ab[i]:02x}{ab[i + 1]:02x}" for i in range(0, 16, 2))
    else:
        raise ValueError(f"unknown addr type: {addr_type}")
    return command, address, port, chunk[pos:]


_traffic_dirty = False
_traffic_since_save = 0


async def check_and_use(uid: str, n: int) -> bool:
    """Account traffic and enforce per-link + group quota. Returns False → must disconnect."""
    global _traffic_dirty, _traffic_since_save
    from main import sub_used_bytes, sub_limit_bytes, SUBS, save_state

    async with LINKS_LOCK:
        link = LINKS.get(uid)
        if link is None:
            return False
        if not link.get("active", True):
            return False
        try:
            if is_link_expired(link):
                return False
        except Exception:
            pass
        # per-link
        limit = int(link.get("limit_bytes") or 0)
        used = int(link.get("used_bytes") or 0)
        if limit > 0 and used + n > limit:
            return False
        # group
        sub_id = link.get("sub_id")
        if sub_id:
            g_limit = sub_limit_bytes(sub_id)
            if g_limit > 0:
                g_used = sub_used_bytes(sub_id)
                if g_used + n > g_limit:
                    return False
        link["used_bytes"] = used + n
        stats["total_bytes"] += n
        try:
            hourly_traffic[now_ir().strftime("%H:00")] += n
        except Exception:
            pass
        _traffic_dirty = True
        _traffic_since_save += n
        should_save = _traffic_since_save >= _TRAFFIC_SAVE_EVERY
        if should_save:
            _traffic_since_save = 0
    if should_save:
        try:
            await save_state()
        except Exception:
            pass
    return True


class _QuotaBatch:
    """Batch quota accounting so we don't take LINKS_LOCK on every small packet."""

    __slots__ = ("uuid", "pending", "ok")

    def __init__(self, uuid: str):
        self.uuid = uuid
        self.pending = 0
        self.ok = True

    async def add(self, n: int) -> bool:
        if not self.ok:
            return False
        self.pending += n
        if self.pending >= QUOTA_BATCH:
            flush, self.pending = self.pending, 0
            self.ok = await check_and_use(self.uuid, flush)
            return self.ok
        return True

    async def flush(self) -> bool:
        if self.pending and self.ok:
            flush, self.pending = self.pending, 0
            self.ok = await check_and_use(self.uuid, flush)
        return self.ok


async def relay_ws_to_tcp(ws: WebSocket, writer: asyncio.StreamWriter, conn_id: str, uid: str):
    gate = _QuotaBatch(uid)
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes") or (msg.get("text") or "").encode()
            if not data:
                continue
            if not await gate.add(len(data)):
                await ws.close(code=1008, reason="quota/disabled/unknown")
                break
            await throttle(uid, len(data))
            stats["total_requests"] += 1
            connections[conn_id]["bytes"] += len(data)
            writer.write(data)
            if writer.transport.get_write_buffer_size() > RELAY_BUF:
                await writer.drain()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        await gate.flush()
        try:
            writer.write_eof()
        except Exception:
            pass


async def relay_tcp_to_ws(ws: WebSocket, reader: asyncio.StreamReader, conn_id: str, uid: str):
    first = True
    gate = _QuotaBatch(uid)
    try:
        while True:
            data = await reader.read(RELAY_BUF)
            if not data:
                break
            if not await gate.add(len(data)):
                await ws.close(code=1008, reason="quota/disabled/unknown")
                break
            await throttle(uid, len(data))
            connections[conn_id]["bytes"] += len(data)
            payload = (b"\x00\x00" + data) if first else data
            first = False
            await ws.send_bytes(payload)
    except Exception:
        pass
    finally:
        await gate.flush()


async def open_dual_stack(address: str, port: int, timeout: float = 12.0):
    """Connect with IPv4/IPv6 + TCP_NODELAY/keepalive + large buffers for max throughput."""
    import socket

    prefer_v6 = True
    try:
        from main import NETWORK_CFG
        prefer_v6 = bool(NETWORK_CFG.get("prefer_ipv6", True))
    except Exception:
        pass
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(address, port, type=socket.SOCK_STREAM)
    if not infos:
        raise OSError(f"resolve failed: {address}")

    def key(info):
        family = info[0]
        is_v6 = 1 if family == socket.AF_INET6 else 0
        return (-is_v6) if prefer_v6 else (is_v6)

    infos = sorted(infos, key=key)
    last_err = None
    for family, type_, proto, canon, sockaddr in infos:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(sockaddr[0], sockaddr[1]),
                timeout=timeout,
            )
            sock = writer.get_extra_info("socket")
            if sock is not None:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except Exception:
                    pass
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                except Exception:
                    pass
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_BUF)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCK_BUF)
                except Exception:
                    pass
            return reader, writer
        except Exception as e:
            last_err = e
            continue
    raise last_err or OSError("connect failed")


def _resolve_link(uuid: str):
    """Find link by uuid (exact or without dashes)."""
    link = LINKS.get(uuid)
    if link is not None:
        return uuid, link
    compact = (uuid or "").replace("-", "")
    if compact and compact != uuid:
        for k, v in LINKS.items():
            if (k or "").replace("-", "") == compact:
                return k, v
    return uuid, None


async def websocket_tunnel(ws: WebSocket, uuid: str):
    await ws.accept()

    async with LINKS_LOCK:
        uuid, link = _resolve_link(uuid)

    if link is None:
        logger.warning(f"🚫 WS unknown uuid={uuid[:12]}… links={len(LINKS)}")
        await ws.close(code=1008, reason="unknown uuid")
        return

    if not is_link_allowed(link):
        logger.warning(f"🚫 WS rejected uuid={uuid[:8]}… (not allowed active={link.get('active')} expired?)")
        await ws.close(code=1008, reason="not authorized")
        return

    # per-link concurrent connection limit
    try:
        cl = int(link.get("connection_limit") or 0)
        if cl > 0:
            cur = sum(1 for c in connections.values() if c.get("uuid") == uuid)
            if cur >= cl:
                logger.warning(f"🚫 WS conn-limit uuid={uuid[:8]} cur={cur}/{cl}")
                await ws.close(code=1008, reason="connection limit")
                return
    except Exception:
        pass

    ip = _ws_client_ip(ws)

    if not is_ip_allowed(link, uuid, ip):
        logger.warning(f"🚫 WS rejected uuid={uuid[:8]}… ip={ip} (ip limit reached)")
        log_activity(
            "connection",
            f"اتصال {ip} به کانفیگ «{link.get('label', '?')}» رد شد (محدودیت تعداد آی‌پی)",
            "warn",
        )
        await ws.close(code=1008, reason="ip limit reached")
        return

    conn_id = secrets.token_urlsafe(6)
    connections[conn_id] = {
        "uuid": uuid,
        "ip": ip,
        "transport": "vless-ws",
        "connected_at": datetime.now().isoformat(),
        "bytes": 0,
    }
    logger.info(f"✅ WS [{conn_id}] uuid={uuid[:8]}… ip={ip} total={len(connections)}")
    log_activity("connection", f"اتصال جدید از {ip} (کانفیگ {link.get('label', '?')})", "info")
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

        stats["total_requests"] += 1
        connections[conn_id]["bytes"] += len(first_chunk)
        logger.info(f"➡️  [{conn_id}] → {address}:{port}")

        reader, writer = await asyncio.wait_for(
            open_dual_stack(address, port),
            timeout=10.0,
        )

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

        asyncio.create_task(save_state())

    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        stats["total_errors"] += 1
        error_logs.append({"error": "connection timeout", "time": datetime.now().isoformat()})
    except Exception as exc:
        stats["total_errors"] += 1
        error_logs.append({"error": str(exc), "time": datetime.now().isoformat()})
        logger.error(f"WS error [{conn_id}]: {exc}")
    finally:
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        connections.pop(conn_id, None)
        logger.info(f"🔌 WS closed [{conn_id}] total={len(connections)}")
