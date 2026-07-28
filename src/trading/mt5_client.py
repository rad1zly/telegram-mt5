"""Wrapper tipis di atas package MetaTrader5 — HANYA jalan di Windows
dengan `pip install MetaTrader5` dan terminal MT5 sudah terbuka & login.

MetaTrader5 diimpor lazy (di dalam fungsi, bukan di top-level module) supaya
file ini tetap bisa diimpor dan bagian pure-logic-nya (decide_order_kind)
tetap bisa ditest di mesin non-Windows.

Semua fungsi di sini blocking/sinkron (sifat asli library MT5). Kalau
dipanggil dari asyncio event loop nanti (pipeline penuh, Fase 4), bungkus
dengan loop.run_in_executor.
"""

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

DEVIATION_POINTS = 20
MAGIC_NUMBER = 990011  # penanda order dari bot ini, bukan order manual


def _mt5():
    import MetaTrader5 as mt5

    return mt5


def connect(login: Optional[int] = None, password: Optional[str] = None, server: Optional[str] = None) -> bool:
    """Tanpa argumen: attach ke terminal MT5 yang sudah terbuka & login
    manual. Dengan login/password/server: login programatik (dipakai nanti
    untuk service 24/7 yang harus bisa reconnect sendiri tanpa interaksi)."""
    mt5 = _mt5()
    if login and password and server:
        ok = mt5.initialize(login=login, password=password, server=server)
    else:
        ok = mt5.initialize()
    if not ok:
        log.error("mt5.initialize gagal: %s", mt5.last_error())
        return False
    return True


def shutdown() -> None:
    _mt5().shutdown()


def is_demo_account() -> Optional[bool]:
    """None kalau tidak bisa ambil account_info (belum connect)."""
    mt5 = _mt5()
    info = mt5.account_info()
    if info is None:
        return None
    return info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO


def get_symbol_info(symbol: str):
    mt5 = _mt5()
    info = mt5.symbol_info(symbol)
    if info is None:
        return None
    if not info.visible:
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)
    return info


def get_all_symbol_names() -> list[str]:
    return [s.name for s in _mt5().symbols_get()]


def decide_order_kind(direction: str, entry: float, current_price: float, tolerance: float) -> str:
    """Pure logic, tidak butuh MT5 — testable di mesin manapun.

    direction: 'BUY' atau 'SELL'. Return salah satu:
    'MARKET', 'BUY_LIMIT', 'SELL_LIMIT', 'BUY_STOP', 'SELL_STOP'.

    Kalau entry masih dalam toleransi dari harga sekarang -> market order.
    Kalau tidak: BUY dengan entry di atas harga sekarang -> breakout (STOP);
    BUY dengan entry di bawah harga sekarang -> pullback (LIMIT). Simetris
    untuk SELL. Ini menghindari perlu menebak STOP vs LIMIT dari kata
    'below'/'above' di teks channel — cukup arah + level, order type
    ditentukan dari harga live saat eksekusi.
    """
    diff = entry - current_price
    if abs(diff) <= tolerance:
        return "MARKET"
    if direction == "BUY":
        return "BUY_STOP" if entry > current_price else "BUY_LIMIT"
    return "SELL_STOP" if entry < current_price else "SELL_LIMIT"


def _order_type_constant(mt5, kind: str, direction: str):
    if kind == "MARKET":
        return mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    return {
        "BUY_LIMIT": mt5.ORDER_TYPE_BUY_LIMIT,
        "SELL_LIMIT": mt5.ORDER_TYPE_SELL_LIMIT,
        "BUY_STOP": mt5.ORDER_TYPE_BUY_STOP,
        "SELL_STOP": mt5.ORDER_TYPE_SELL_STOP,
    }[kind]


def _pick_filling_mode(mt5, info):
    # info.filling_mode adalah bitmask: 1=FOK, 2=IOC. Broker beda-beda
    # dukungannya; salah pilih bikin order_send gagal dengan retcode
    # "Unsupported filling mode" walau semua field lain benar.
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


@dataclass
class OrderResult:
    success: bool
    ticket: Optional[int] = None
    price: Optional[float] = None
    kind: Optional[str] = None
    error: Optional[str] = None
    raw_retcode: Optional[int] = None


def send_order(
    symbol: str,
    direction: str,
    volume: float,
    entry: float,
    sl: float,
    tp: float,
    comment: str = "telegram-mt5",
) -> OrderResult:
    mt5 = _mt5()

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return OrderResult(success=False, error=f"Tidak bisa ambil tick untuk {symbol}")

    info = get_symbol_info(symbol)
    if info is None:
        return OrderResult(success=False, error=f"symbol_info kosong untuk {symbol}")

    current_price = tick.ask if direction == "BUY" else tick.bid
    tolerance = info.point * 50  # toleransi kasar ~5 pip; dibuat konfigurabel di Fase 4
    kind = decide_order_kind(direction, entry, current_price, tolerance)
    order_type = _order_type_constant(mt5, kind, direction)

    if kind == "MARKET":
        price = current_price
        action = mt5.TRADE_ACTION_DEAL
    else:
        price = entry
        action = mt5.TRADE_ACTION_PENDING

    request = {
        "action": action,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": DEVIATION_POINTS,
        "magic": MAGIC_NUMBER,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _pick_filling_mode(mt5, info),
    }

    result = mt5.order_send(request)
    if result is None:
        return OrderResult(success=False, error=f"order_send mengembalikan None: {mt5.last_error()}")
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return OrderResult(
            success=False,
            error=f"retcode={result.retcode} ({result.comment})",
            raw_retcode=result.retcode,
        )

    return OrderResult(success=True, ticket=result.order, price=result.price, kind=kind)


def modify_sl_tp(ticket: int, symbol: str, sl: Optional[float] = None, tp: Optional[float] = None) -> OrderResult:
    mt5 = _mt5()
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol": symbol,
    }
    if sl is not None:
        request["sl"] = sl
    if tp is not None:
        request["tp"] = tp

    result = mt5.order_send(request)
    if result is None:
        return OrderResult(success=False, error=f"order_send mengembalikan None: {mt5.last_error()}")
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return OrderResult(success=False, error=f"retcode={result.retcode} ({result.comment})", raw_retcode=result.retcode)
    return OrderResult(success=True, ticket=ticket)


def partial_close(ticket: int, symbol: str, volume: float) -> OrderResult:
    mt5 = _mt5()
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return OrderResult(success=False, error=f"Posisi #{ticket} tidak ditemukan")
    position = positions[0]

    close_direction_is_buy = position.type == mt5.ORDER_TYPE_SELL  # tutup SELL = order BUY, dan sebaliknya
    tick = mt5.symbol_info_tick(symbol)
    price = tick.ask if close_direction_is_buy else tick.bid
    info = get_symbol_info(symbol)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": ticket,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY if close_direction_is_buy else mt5.ORDER_TYPE_SELL,
        "price": price,
        "deviation": DEVIATION_POINTS,
        "magic": MAGIC_NUMBER,
        "comment": "partial-close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _pick_filling_mode(mt5, info),
    }

    result = mt5.order_send(request)
    if result is None:
        return OrderResult(success=False, error=f"order_send mengembalikan None: {mt5.last_error()}")
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return OrderResult(success=False, error=f"retcode={result.retcode} ({result.comment})", raw_retcode=result.retcode)
    return OrderResult(success=True, ticket=ticket, price=result.price)
