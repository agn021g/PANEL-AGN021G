# relay_vless.py
# بخش VLESS Relay — جدا شده از main.py (منطق اصلی دست‌نخورده)
# بهینه‌سازی سرعت: بافر بزرگ‌تر، حساب‌کتاب ترافیک دسته‌ای، قفل کمتر، TCP_NODELAY + بافر سوکت بزرگ

import asyncio
import secrets
from datetime import datetime

from fastapi import WebSocket, WebSocketDisconnect

import logging

logger = logging.getLogger("AGN021G.relay")

def _M():
    """Lazy import main to avoid circular import at startup."""
    import main as m
    return m

try:
    from speed_limit import throttle
except Exception:
    async def throttle(uuid, n):  # type: ignore
        return None

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
    m = _M(); sub_used_bytes, sub_limit_bytes, SUBS, save_state = m.sub_used_bytes, m.sub_limit_bytes, m.SUBS, m.save_state

    async with _M().LINKS_LOCK:
        link = _M().LINKS.get(uid)
        if link is None:
            return False
        if not link.get("active", True):
            return False
        try:
            if _M().is_link_expired(link):
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
        _M().stats["total_bytes"] += n
        try:
            _M().hourly_traffic[_M().now_ir().strftime("%H:00")] += n
        except Exception:
            pass
        _traffic_dirty = True
        _traffic_since_save += n
        should_save = _traffic_since_save >= _TRAFFIC_SAVE_EVERY
        if should_save:
            _traffic_since_save = 0
    if should_save:
        try:
            await _M().save_state()
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
            _M().stats["total_requests"] += 1
            _M().connections[conn_id]["bytes"] += len(data)
            writer.write(data)
            # drain sooner so pages/streams don't stall mid-load
            if writer.transport.get_write_buffer_size() > (256 * 1024):
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
    """Forward remote TCP bytes to client. VLESS header must already have been sent once."""
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
            try:
                _M().connections[conn_id]["bytes"] += len(data)
            except Exception:
                pass
            await ws.send_bytes(data)
    except Exception:
        pass
    finally:
        await gate.flush()



async def open_dual_stack(address: str, port: int, timeout: float = 10.0):
    """Connect outbound: IPv4 first by default, then IPv6. TCP_NODELAY + large buffers."""
    import socket

    # Default IPv4 priority — IPv6 only if prefer_ipv6=True in settings
    prefer_v6 = False
    try:
        prefer_v6 = bool(_M().NETWORK_CFG.get("prefer_ipv6", False))
    except Exception:
        prefer_v6 = False

    loop = asyncio.get_running_loop()
    # Fast path: literal IP — skip DNS
    try:
        import ipaddress as _ip
        _ip.ip_address(address.strip("[]"))
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(address.strip("[]"), port),
            timeout=min(4.0, float(timeout)),
        )
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCK_BUF)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCK_BUF)
            except Exception:
                pass
        return reader, writer
    except ValueError:
        pass  # not an IP literal
    except Exception as e:
        logger.debug("IP literal connect failed %s:%s — %s", address, port, e)
        # fall through to getaddrinfo path

    infos4, infos6 = [], []
    try:
        infos4 = await loop.getaddrinfo(address, port, type=socket.SOCK_STREAM, family=socket.AF_INET)
    except Exception:
        pass
    try:
        infos6 = await loop.getaddrinfo(address, port, type=socket.SOCK_STREAM, family=socket.AF_INET6)
    except Exception:
        pass
    # dedupe while preserving order
    seen = set()
    ordered = []
    sequence = (infos6 + infos4) if prefer_v6 else (infos4 + infos6)
    if not sequence:
        # last resort: any family
        sequence = await loop.getaddrinfo(address, port, type=socket.SOCK_STREAM)
    for info in sequence:
        sockaddr = info[4]
        key = (sockaddr[0], sockaddr[1] if len(sockaddr) > 1 else port)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(info)
    if not ordered:
        raise OSError(f"resolve failed: {address}")

    per_try = min(4.0, max(2.0, float(timeout) / max(1, len(ordered))))
    last_err = None
    for family, type_, proto, canon, sockaddr in ordered:
        host_ip = sockaddr[0]
        # skip pure IPv6 when we want IPv4 priority and we still have v4 left? already ordered
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host_ip, sockaddr[1]),
                timeout=per_try,
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
    link = _M().LINKS.get(uuid)
    if link is not None:
        return uuid, link
    compact = (uuid or "").replace("-", "")
    if compact and compact != uuid:
        for k, v in _M().LINKS.items():
            if (k or "").replace("-", "") == compact:
                return k, v
    return uuid, None



async def _udp_tunnel(ws: WebSocket, address: str, port: int, initial: bytes, conn_id: str, uid: str, ver: bytes):
    """Basic VLESS UDP (DNS etc.). Always reply VLESS OK first."""
    import socket
    try:
        await ws.send_bytes((ver if ver else bytes([0])) + bytes([0]))
    except Exception:
        try:
            await ws.send_bytes(bytes([0, 0]))
        except Exception:
            return

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(address, port, type=socket.SOCK_DGRAM, family=socket.AF_INET)
    except Exception:
        try:
            infos = await loop.getaddrinfo(address, port, type=socket.SOCK_DGRAM)
        except Exception as exc:
            logger.warning("UDP resolve failed %s:%s %s", address, port, exc)
            return
    if not infos:
        return
    dest = infos[0][4]
    sock = socket.socket(infos[0][0], socket.SOCK_DGRAM)
    sock.setblocking(False)

    def strip_len_prefix(data: bytes) -> bytes:
        if len(data) >= 2:
            ln = int.from_bytes(data[:2], "big")
            if ln == len(data) - 2 and 0 < ln <= 65535:
                return data[2:]
        return data

    async def send_udp(data: bytes):
        data = strip_len_prefix(data)
        if not data:
            return
        await loop.sock_sendto(sock, data, dest)
        try:
            _M().connections[conn_id]["bytes"] += len(data)
        except Exception:
            pass

    if initial:
        try:
            await send_udp(initial)
        except Exception as exc:
            logger.warning("UDP send failed: %s", exc)

    async def ws_to_udp():
        gate = _QuotaBatch(uid)
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                data = msg.get("bytes") or b""
                if not data:
                    continue
                if not await gate.add(len(data)):
                    break
                await throttle(uid, len(data))
                await send_udp(data)
        except Exception:
            pass
        finally:
            await gate.flush()

    async def udp_to_ws():
        gate = _QuotaBatch(uid)
        try:
            while True:
                try:
                    data, _ = await asyncio.wait_for(loop.sock_recvfrom(sock, 65535), timeout=45.0)
                except asyncio.TimeoutError:
                    continue
                if not data:
                    break
                if not await gate.add(len(data)):
                    break
                await throttle(uid, len(data))
                try:
                    _M().connections[conn_id]["bytes"] += len(data)
                except Exception:
                    pass
                await ws.send_bytes(len(data).to_bytes(2, "big") + data)
        except Exception:
            pass
        finally:
            await gate.flush()

    try:
        done, pending = await asyncio.wait(
            {asyncio.create_task(ws_to_udp()), asyncio.create_task(udp_to_ws())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
    finally:
        try:
            sock.close()
        except Exception:
            pass


async def websocket_tunnel(ws: WebSocket, uuid: str):
    await ws.accept()

    async with _M().LINKS_LOCK:
        uuid, link = _resolve_link(uuid)

    if link is None:
        logger.warning(f"🚫 WS unknown uuid={uuid[:12]}… links={len(_M().LINKS)}")
        await ws.close(code=1008, reason="unknown uuid")
        return

    if not _M().is_link_allowed(link):
        logger.warning(f"🚫 WS rejected uuid={uuid[:8]}… (not allowed active={link.get('active')} expired?)")
        await ws.close(code=1008, reason="not authorized")
        return

    # per-link concurrent connection limit
    try:
        cl = int(link.get("connection_limit") or 0)
        if cl > 0:
            cur = sum(1 for c in _M().connections.values() if c.get("uuid") == uuid)
            if cur >= cl:
                logger.warning(f"🚫 WS conn-limit uuid={uuid[:8]} cur={cur}/{cl}")
                await ws.close(code=1008, reason="connection limit")
                return
    except Exception:
        pass

    ip = _ws_client_ip(ws)

    if not _M().is_ip_allowed(link, uuid, ip):
        logger.warning(f"🚫 WS rejected uuid={uuid[:8]}… ip={ip} (ip limit reached)")
        _M().log_activity(
            "connection",
            f"اتصال {ip} به کانفیگ «{link.get('label', '?')}» رد شد (محدودیت تعداد آی‌پی)",
            "warn",
        )
        await ws.close(code=1008, reason="ip limit reached")
        return

    conn_id = secrets.token_urlsafe(6)
    _M().connections[conn_id] = {
        "uuid": uuid,
        "ip": ip,
        "transport": "vless-ws",
        "connected_at": datetime.now().isoformat(),
        "bytes": 0,
    }
    logger.info(f"✅ WS [{conn_id}] uuid={uuid[:8]}… ip={ip} total={len(_M().connections)}")
    _M().log_activity("connection", f"اتصال جدید از {ip} (کانفیگ {link.get('label', '?')})", "info")
    writer = None

    try:
        first_msg = await asyncio.wait_for(ws.receive(), timeout=15.0)
        if first_msg["type"] == "websocket.disconnect":
            return
        first_chunk = first_msg.get("bytes") or (first_msg.get("text") or "").encode()
        if not first_chunk:
            return

        command, address, port, payload = await parse_vless_header(first_chunk)
        ver = first_chunk[0:1] if first_chunk else bytes([0])

        if not await check_and_use(uuid, len(first_chunk)):
            await ws.close(code=1008, reason="quota/disabled")
            return

        _M().stats["total_requests"] += 1
        _M().connections[conn_id]["bytes"] += len(first_chunk)
        logger.info(f"➡️  [{conn_id}] cmd={command} → {address}:{port}")

        # command: 1 = TCP, 2 = UDP (DNS and similar)
        if command == 2:
            await _udp_tunnel(ws, address, port, payload, conn_id, uuid, ver)
            asyncio.create_task(_M().save_state())
            return

        reader, writer = await asyncio.wait_for(
            open_dual_stack(address, port, timeout=8.0),
            timeout=12.0,
        )

        # CRITICAL: reply VLESS success immediately (version + addon_len=0)
        try:
            await ws.send_bytes(ver + bytes([0]))
        except Exception:
            await ws.send_bytes(bytes([0, 0]))

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

        asyncio.create_task(_M().save_state())

    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        _M().stats["total_errors"] += 1
        _M().error_logs.append({"error": "connection timeout", "time": datetime.now().isoformat()})
    except Exception as exc:
        _M().stats["total_errors"] += 1
        _M().error_logs.append({"error": str(exc), "time": datetime.now().isoformat()})
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
