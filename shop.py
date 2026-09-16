# shop.py — ماژول فروش AGN021G (محصول / کیف پول / کارت‌به‌کارت)
import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from main import DATA_DIR, logger

SHOP_FILE = Path(DATA_DIR) / "shop_state.json"

_DEFAULT = {
    "card_number": "",
    "card_holder": "",
    "products": [],
    "wallets": {},       # str(uid) -> {"balance": int, "name": str}
    "orders": {},        # order_id -> {...}
    "pending": {},       # order_id waiting admin approve
}


def _load() -> dict:
    try:
        if SHOP_FILE.exists():
            data = json.loads(SHOP_FILE.read_text(encoding="utf-8"))
            for k, v in _DEFAULT.items():
                data.setdefault(k, v if not isinstance(v, dict) else dict(v))
            return data
    except Exception as e:
        logger.warning(f"shop load: {e}")
    return json.loads(json.dumps(_DEFAULT))


def _save(data: dict):
    try:
        SHOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        SHOP_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"shop save: {e}")


def get_card() -> tuple[str, str]:
    d = _load()
    return str(d.get("card_number") or ""), str(d.get("card_holder") or "")


def set_card(number: str, holder: str = ""):
    d = _load()
    d["card_number"] = (number or "").strip()
    d["card_holder"] = (holder or "").strip()
    _save(d)


def list_products(active_only: bool = True) -> list:
    d = _load()
    items = list(d.get("products") or [])
    if active_only:
        items = [p for p in items if p.get("active", True)]
    return sorted(items, key=lambda x: int(x.get("price") or 0))


def get_product(pid: str) -> dict | None:
    for p in _load().get("products") or []:
        if str(p.get("id")) == str(pid):
            return p
    return None


def add_product(name: str, price: int, volume_gb: float, days: int, protocol: str = "vless-ws") -> dict:
    d = _load()
    pid = secrets.token_hex(4)
    prod = {
        "id": pid,
        "name": (name or "محصول")[:60],
        "price": max(0, int(price)),
        "volume_gb": max(0.0, float(volume_gb)),
        "days": max(0, int(days)),
        "protocol": protocol or "vless-ws",
        "active": True,
        "created_at": datetime.now().isoformat(),
    }
    d.setdefault("products", []).append(prod)
    _save(d)
    return prod


def update_product(pid: str, **kwargs) -> bool:
    d = _load()
    for p in d.get("products") or []:
        if str(p.get("id")) == str(pid):
            for k, v in kwargs.items():
                if k in ("name", "price", "volume_gb", "days", "protocol", "active") and v is not None:
                    p[k] = v
            _save(d)
            return True
    return False


def delete_product(pid: str) -> bool:
    d = _load()
    before = len(d.get("products") or [])
    d["products"] = [p for p in (d.get("products") or []) if str(p.get("id")) != str(pid)]
    if len(d["products"]) < before:
        _save(d)
        return True
    return False


def get_wallet(uid: int | str) -> dict:
    d = _load()
    key = str(uid)
    w = d.setdefault("wallets", {}).setdefault(key, {"balance": 0, "name": ""})
    return w


def set_wallet_name(uid: int | str, name: str):
    d = _load()
    w = d.setdefault("wallets", {}).setdefault(str(uid), {"balance": 0, "name": ""})
    w["name"] = (name or "")[:80]
    _save(d)


def add_balance(uid: int | str, amount: int) -> int:
    d = _load()
    w = d.setdefault("wallets", {}).setdefault(str(uid), {"balance": 0, "name": ""})
    w["balance"] = max(0, int(w.get("balance") or 0) + int(amount))
    _save(d)
    return int(w["balance"])


def deduct_balance(uid: int | str, amount: int) -> bool:
    d = _load()
    w = d.setdefault("wallets", {}).setdefault(str(uid), {"balance": 0, "name": ""})
    bal = int(w.get("balance") or 0)
    if bal < amount:
        return False
    w["balance"] = bal - amount
    _save(d)
    return True


def create_order(uid: int, product_id: str, username: str = "", full_name: str = "") -> dict | None:
    prod = get_product(product_id)
    if not prod or not prod.get("active", True):
        return None
    oid = secrets.token_hex(5)
    order = {
        "id": oid,
        "user_id": int(uid),
        "username": (username or "")[:64],
        "full_name": (full_name or "")[:80],
        "product_id": prod["id"],
        "product_name": prod["name"],
        "price": int(prod["price"]),
        "volume_gb": float(prod.get("volume_gb") or 0),
        "days": int(prod.get("days") or 0),
        "protocol": prod.get("protocol") or "vless-ws",
        "status": "awaiting_payment",  # awaiting_payment | pending_review | paid | rejected | cancelled
        "receipt": "",
        "config_uuid": "",
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    d = _load()
    d.setdefault("orders", {})[oid] = order
    _save(d)
    return order


def get_order(oid: str) -> dict | None:
    return (_load().get("orders") or {}).get(str(oid))


def set_order_receipt(oid: str, receipt: str) -> dict | None:
    d = _load()
    o = (d.get("orders") or {}).get(str(oid))
    if not o:
        return None
    o["receipt"] = (receipt or "")[:500]
    o["status"] = "pending_review"
    o["updated_at"] = datetime.now().isoformat()
    d.setdefault("pending", {})[oid] = True
    _save(d)
    return o


def list_pending_orders() -> list:
    d = _load()
    out = []
    for oid in list((d.get("pending") or {}).keys()):
        o = (d.get("orders") or {}).get(oid)
        if o and o.get("status") == "pending_review":
            out.append(o)
    return sorted(out, key=lambda x: x.get("created_at") or "")


def list_user_orders(uid: int) -> list:
    d = _load()
    out = [o for o in (d.get("orders") or {}).values() if int(o.get("user_id") or 0) == int(uid)]
    return sorted(out, key=lambda x: x.get("created_at") or "", reverse=True)


def approve_order(oid: str, config_uuid: str = "") -> dict | None:
    d = _load()
    o = (d.get("orders") or {}).get(str(oid))
    if not o:
        return None
    o["status"] = "paid"
    o["config_uuid"] = config_uuid or o.get("config_uuid") or ""
    o["updated_at"] = datetime.now().isoformat()
    (d.get("pending") or {}).pop(str(oid), None)
    _save(d)
    return o


def reject_order(oid: str, reason: str = "") -> dict | None:
    d = _load()
    o = (d.get("orders") or {}).get(str(oid))
    if not o:
        return None
    o["status"] = "rejected"
    o["receipt"] = (o.get("receipt") or "") + (f"\n[رد: {reason}]" if reason else "")
    o["updated_at"] = datetime.now().isoformat()
    (d.get("pending") or {}).pop(str(oid), None)
    _save(d)
    return o


def fmt_price(n: int) -> str:
    try:
        return f"{int(n):,}".replace(",", "٬") + " تومان"
    except Exception:
        return f"{n} تومان"
