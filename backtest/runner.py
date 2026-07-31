"""Runner: replay seluruh korpus sinyal historis terhadap data harga M5,
hasilkan daftar SimulatedTrade + statistik yang dilewati (skipped) —
memakai parser, symbol resolver, dan risk sizing YANG SAMA PERSIS dengan
pipeline live (src/parser, src/trading), supaya hasilnya representatif.

Fallback LLM (MiniMax) SENGAJA TIDAK dipakai di sini — replay 6716 pesan
lewat API akan lambat & berbayar. Backtest ini hanya mencakup sinyal yang
berhasil dikenali regex (patterns.py/followup.py), bukan 100% korpus.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from backtest.engine import SimulatedTrade, SymbolSpec, pnl_usd, resolve_entry_fill, resolve_trade_up_to
from backtest.price_data import PriceSeries
from src.parser.followup import classify_followup_kinds, parse_followup_regex
from src.parser.patterns import parse_entry_signal
from src.parser.schema import FollowUp, Signal, apply_price_offset
from src.trading.risk import calculate_lot, calculate_partial_close_volume
from src.trading.symbols import SymbolResolver


@dataclass
class BacktestConfig:
    risk_usd: float
    max_lot_cap: float
    max_price_deviation_pips: float
    price_deviation_overrides: dict = field(default_factory=dict)
    min_sl_distance_overrides: dict = field(default_factory=dict)
    price_offset_overrides: dict = field(default_factory=dict)  # {canonical: offset} -- lihat
                                                                  # src/parser/schema.py:apply_price_offset
    partial_close_percent: float = 50.0
    move_sl_to_be_enabled: bool = True
    partial_close_enabled: bool = True
    close_all_enabled: bool = False
    sl_plus_buffer_overrides: dict = field(default_factory=dict)
    tp_index: int = -1  # -1 = TP terakhir/terjauh (perilaku executor.py live saat ini).
                         # 0 = TP pertama/terdekat -- dipakai untuk bandingkan strategi.
                         # DIABAIKAN kalau tp_r_multiple diisi (lihat bawah).
    tp_r_multiple: Optional[float] = None  # kalau diisi (mis. 4.0), TP keras = entry +/- N*R,
                                            # BUKAN dari signal.tp sama sekali -- R = jarak entry ke SL
                                            # (beda per trade, self-scaling, tidak bergantung angka TP channel).
    tp_fixed_distance_overrides: dict = field(default_factory=dict)  # {canonical: jarak harga} -- TP keras
                                            # = entry +/- jarak TETAP per simbol (BUKAN relatif ke SL trade
                                            # spt tp_r_multiple, BUKAN dari signal.tp/tp_index). Diturunkan dari
                                            # persentil ke-40 distribusi "gerakan terbaik sebelum SL kena" secara
                                            # historis per simbol -- terbukti (lewat sweep) hasil PALING baik dari
                                            # semua strategi TP yang sudah dicoba. Prioritas: tp_r_multiple >
                                            # tp_fixed_distance_overrides (kalau simbolnya ada) > tp_index (TP
                                            # channel apa adanya).
    auto_be_r_multiple: Optional[float] = None  # kalau diisi (mis. 1.0), SL otomatis pindah ke
                                                  # breakeven begitu harga +N*R -- mekanis, TIDAK
                                                  # bergantung follow-up message channel sama sekali.


def load_signal_rows(path: str) -> list:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows.sort(key=lambda r: r["date_utc"])
    return rows


def _resolve_trade_via_reply_chain(
    reply_to_msg_id: Optional[int],
    rows_by_id: dict,
    trade_by_message_id: dict,
    max_depth: int = 20,
) -> Optional[SimulatedTrade]:
    """Telusuri reply_to_msg_id ke atas sampai ketemu message_id yang sudah
    terhubung ke sebuah trade (entry ATAU follow-up sebelumnya yang sudah
    di-resolve). Ini pengait EKSAK (data reply asli dari Telegram, bukan
    tebakan simbol+waktu-terdekat) -- ditemukan 50%+ pesan di korpus channel
    ini PUNYA reply_to_msg_id, sebelumnya sama sekali tidak dipakai. Kalau
    chain putus/tidak ada/tidak nyambung ke trade manapun, return None
    (caller fallback ke pencocokan simbol+waktu-terdekat yang lama)."""
    current_id = reply_to_msg_id
    depth = 0
    while current_id is not None and depth < max_depth:
        trade = trade_by_message_id.get(current_id)
        if trade is not None:
            return trade
        parent_row = rows_by_id.get(current_id)
        if parent_row is None:
            return None
        current_id = parent_row.get("reply_to_msg_id")
        depth += 1
    return None


def run(
    signal_rows: list,
    resolver: SymbolResolver,
    broker_symbols: list,
    price_series: dict,
    symbol_specs: dict,
    config: BacktestConfig,
    classify_fn=None,
):
    """classify_fn (opsional): pengganti parse_entry_signal/parse_followup_regex
    -- dipanggil sbg classify_fn(text, message_id, reply_to_msg_id) ->
    Signal | FollowUp | None. Dipakai utk mode 'llm' (baca dari cache hasil
    classify_message_with_llm, lihat backtest/llm_source.py) supaya bisa
    bandingkan hasil sumber sinyal LLM vs regex TANPA duplikasi logika
    eksekusi/reply-chain di bawah ini. Default None -> regex seperti biasa."""
    trades: list = []
    open_trades_by_symbol: dict = {}
    trade_by_message_id: dict = {}
    rows_by_id = {row["message_id"]: row for row in signal_rows}
    skipped = {"symbol_not_covered": 0, "never_filled": 0, "no_sl": 0, "lot_rejected": 0}

    for row in signal_rows:
        text = row.get("text") or ""
        if not text.strip():
            continue
        date_str = row.get("date_utc")
        if not date_str:
            continue
        event_time = datetime.fromisoformat(date_str)

        if classify_fn is not None:
            classified = classify_fn(text, row["message_id"], row.get("reply_to_msg_id"))
            signal = classified if isinstance(classified, Signal) else None
            preclassified_followup = classified if isinstance(classified, FollowUp) else None
            if signal is None and preclassified_followup is None:
                continue
        else:
            signal = parse_entry_signal(text, message_id=row["message_id"])
            preclassified_followup = None

        if signal is not None:
            canonical = resolver.canonical_of(signal.symbol)
            if canonical is None or canonical not in price_series:
                skipped["symbol_not_covered"] += 1
                continue

            series = price_series[canonical]
            spec = symbol_specs[canonical]

            offset = config.price_offset_overrides.get(canonical, 0.0)
            signal = apply_price_offset(signal, offset)

            if signal.sl is None:
                skipped["no_sl"] += 1
                continue

            deviation_pips = config.price_deviation_overrides.get(canonical, config.max_price_deviation_pips)
            fill = resolve_entry_fill(
                direction=signal.action, entry=signal.entry, entry_range=signal.entry_range,
                series=series, signal_time=event_time, spec=spec, max_deviation_pips=deviation_pips,
            )
            if fill is None:
                skipped["never_filled"] += 1
                continue
            fill_idx, fill_price, kind = fill

            min_sl_distance = config.min_sl_distance_overrides.get(canonical, 0.0)
            lot_result = calculate_lot(
                entry=fill_price, sl=signal.sl,
                tick_size=spec.trade_tick_size, tick_value=spec.trade_tick_value,
                volume_step=spec.volume_step, volume_min=spec.volume_min, volume_max=spec.volume_max,
                risk_usd=config.risk_usd, max_lot_cap=config.max_lot_cap,
                min_sl_distance=min_sl_distance,
            )
            if not lot_result.ok:
                skipped["lot_rejected"] += 1
                continue

            r_value = abs(fill_price - signal.sl)
            fixed_distance = config.tp_fixed_distance_overrides.get(canonical)
            if config.tp_r_multiple is not None:
                tp_price = (
                    fill_price + config.tp_r_multiple * r_value
                    if signal.action == "BUY"
                    else fill_price - config.tp_r_multiple * r_value
                )
            elif fixed_distance is not None:
                tp_price = fill_price + fixed_distance if signal.action == "BUY" else fill_price - fixed_distance
            else:
                tp_price = signal.tp[config.tp_index] if signal.tp else fill_price

            trade = SimulatedTrade(
                signal_message_id=row["message_id"], canonical_symbol=canonical,
                direction=signal.action, lot=lot_result.lot,
                entry_price=fill_price, entry_time=series.candles[fill_idx].time,
                sl=signal.sl, tp=tp_price, kind=kind, r_value=r_value,
            )
            trade._last_resolved_index = fill_idx - 1
            trades.append(trade)
            open_trades_by_symbol.setdefault(canonical, []).append(trade)
            trade_by_message_id[row["message_id"]] = trade
            continue

        if classify_fn is not None:
            followup = preclassified_followup  # dijamin bukan None (lihat guard di atas)
        else:
            followup = parse_followup_regex(text, message_id=row["message_id"], reply_to_msg_id=row.get("reply_to_msg_id"))
        target_trade = None
        kinds: list = []

        if followup is not None:
            # Header 'Live Update' + simbol ketemu di teks sendiri (regex),
            # ATAU hasil classify_fn (LLM) apa pun bentuk teksnya.
            if not followup.kinds:
                continue
            kinds = followup.kinds
            # Prioritas 1: ikuti reply_to_msg_id ke atas -- pengait EKSAK dari
            # Telegram sendiri, lebih presisi dari tebakan simbol+waktu-terdekat.
            target_trade = _resolve_trade_via_reply_chain(row.get("reply_to_msg_id"), rows_by_id, trade_by_message_id)
            if target_trade is not None:
                canonical = target_trade.canonical_symbol
                series = price_series[canonical]
                spec = symbol_specs[canonical]
                resolve_trade_up_to(target_trade, series, event_time, auto_be_r_multiple=config.auto_be_r_multiple)
                if not target_trade.is_open:
                    target_trade = None
            # Prioritas 2 (fallback): pencocokan simbol+waktu-terdekat yang lama.
            if target_trade is None:
                if followup.symbol is None:
                    continue
                canonical = resolver.canonical_of(followup.symbol)
                if canonical is None or canonical not in price_series:
                    continue
                series = price_series[canonical]
                spec = symbol_specs[canonical]
                candidates = sorted(open_trades_by_symbol.get(canonical, []), key=lambda t: t.entry_time, reverse=True)
                for t in candidates:
                    resolve_trade_up_to(t, series, event_time, auto_be_r_multiple=config.auto_be_r_multiple)
                    if t.is_open:
                        target_trade = t
                        break
                if target_trade is None:
                    continue
        else:
            # TIDAK ADA header 'Live Update' sama sekali (mis. msg 2098 di
            # korpus asli: "So Now you can SELL it from here..." -- simbol
            # cuma disebut di pesan SEBELUMNYA dalam thread). Satu-satunya
            # cara mengenali ini sebagai follow-up: reply_to_msg_id-nya
            # menunjuk (langsung/berantai) ke trade yang sudah kita kenal.
            target_trade = _resolve_trade_via_reply_chain(row.get("reply_to_msg_id"), rows_by_id, trade_by_message_id)
            if target_trade is None:
                continue
            canonical = target_trade.canonical_symbol
            series = price_series[canonical]
            spec = symbol_specs[canonical]
            resolve_trade_up_to(target_trade, series, event_time, auto_be_r_multiple=config.auto_be_r_multiple)
            if not target_trade.is_open:
                continue
            kinds = classify_followup_kinds(text)
            if not kinds:
                continue

        trade_by_message_id[row["message_id"]] = target_trade

        if "move_sl_be" in kinds and not target_trade.be_moved and config.move_sl_to_be_enabled:
            target_trade.sl = target_trade.entry_price
            target_trade.be_moved = True

        if "partial_close_tp1" in kinds and not target_trade.tp1_hit and config.partial_close_enabled:
            pc = calculate_partial_close_volume(
                position_lot=target_trade.remaining_lot, percent=config.partial_close_percent,
                volume_step=spec.volume_step, volume_min=spec.volume_min,
            )
            if pc.ok:
                idx = series.index_at_or_after(event_time)
                approx_price = series.candles[idx].open if idx is not None else target_trade.entry_price
                if pc.action == "partial":
                    target_trade.realized_pnl_usd += pnl_usd(target_trade, approx_price, pc.volume, spec)
                    target_trade.remaining_lot -= pc.volume

                    # SL+ otomatis: begitu TP1/partial-close berhasil, SL dipindah ke
                    # breakeven+buffer -- SAMA PERSIS dgn logika main.py live, tidak
                    # bergantung apakah followup.kinds juga mengandung move_sl_be.
                    buffer = config.sl_plus_buffer_overrides.get(canonical, 0.0)
                    if buffer > 0:
                        target_trade.sl = (
                            target_trade.entry_price + buffer
                            if target_trade.direction == "BUY"
                            else target_trade.entry_price - buffer
                        )
                        target_trade.be_moved = True
                else:
                    target_trade.realized_pnl_usd += pnl_usd(target_trade, approx_price, target_trade.remaining_lot, spec)
                    target_trade.remaining_lot = 0.0
                    target_trade.exit_price = approx_price
                    target_trade.exit_time = event_time
                    target_trade.exit_reason = "partial_close_full"
                target_trade.tp1_hit = True

        if "close_all" in kinds and target_trade.is_open and config.close_all_enabled:
            idx = series.index_at_or_after(event_time)
            approx_price = series.candles[idx].open if idx is not None else target_trade.entry_price
            target_trade.realized_pnl_usd += pnl_usd(target_trade, approx_price, target_trade.remaining_lot, spec)
            target_trade.remaining_lot = 0.0
            target_trade.exit_price = approx_price
            target_trade.exit_time = event_time
            target_trade.exit_reason = "close_all"

    for canonical, series in price_series.items():
        if not series.candles:
            continue
        last_time = series.candles[-1].time
        for t in open_trades_by_symbol.get(canonical, []):
            resolve_trade_up_to(t, series, last_time, auto_be_r_multiple=config.auto_be_r_multiple)

    return trades, skipped


@dataclass
class BacktestReport:
    total_trades: int
    closed_trades: int
    still_open_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl_usd: float
    max_drawdown_usd: float
    max_consecutive_losses: int
    profit_factor: Optional[float]
    max_balance_drawdown_pct: float
    max_equity_drawdown_pct: float
    max_balance_drawdown_at: Optional[datetime]
    max_equity_drawdown_at: Optional[datetime]
    max_balance_drawdown_peak_usd: float
    max_equity_drawdown_peak_usd: float
    account_blown: bool
    account_blown_at: Optional[datetime]
    per_symbol: dict
    skipped: dict


def _max_drawdown_usd(chronological_pnls: list) -> float:
    """Penurunan TERBESAR dari puncak running-equity ke titik terendah
    sesudahnya (dalam $), dihitung dari kurva P/L kumulatif dalam urutan
    KRONOLOGIS exit (bukan urutan list trades apa adanya, karena trades
    tidak selalu tersimpan berurutan waktu exit-nya)."""
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for pnl in chronological_pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _balance_curve_points(closed_trades: list, initial_deposit: float) -> list:
    """[(exit_time, balance)] terurut kronologis -- BALANCE (beda dari
    equity) cuma berubah saat trade benar-benar CLOSE/realized, persis
    definisi 'Balance' di terminal broker."""
    points = []
    running = initial_deposit
    for _, exit_time, pnl in sorted(closed_trades, key=lambda c: c[1]):
        running += pnl
        points.append((exit_time, running))
    return points


def _equity_curve_points(closed_trades: list, initial_deposit: float) -> list:
    """[(t, equity)] di setiap titik entry/exit TRADE MANAPUN -- equity =
    balance + floating P/L trade yang masih terbuka saat itu (equity bisa
    lebih rendah dari balance saat posisi lain sedang floating rugi,
    meski belum ada yang di-close).

    CATATAN PENTING: floating P/L trade yang masih terbuka DIAPROKSIMASI
    linear dari 0 (saat entry) ke pnl final (saat exit) berdasarkan
    fraksi waktu berlalu -- build_report cuma menerima titik entry & exit
    tiap trade (bukan seluruh jalur harga di antaranya), jadi ini ESTIMASI,
    bukan mark-to-market presisi tick. Balance TIDAK kena masalah ini
    karena cuma bergantung pada P/L final yang sudah pasti/realized."""
    if not closed_trades:
        return []
    event_times = sorted({t for c in closed_trades for t in (c[0], c[1])})
    by_exit = sorted(closed_trades, key=lambda c: c[1])

    points = []
    for t in event_times:
        balance = initial_deposit + sum(pnl for _, exit_time, pnl in by_exit if exit_time <= t)
        floating = 0.0
        for entry_time, exit_time, pnl in closed_trades:
            if entry_time <= t < exit_time:
                duration = (exit_time - entry_time).total_seconds()
                frac = (t - entry_time).total_seconds() / duration if duration > 0 else 0.0
                floating += pnl * min(max(frac, 0.0), 1.0)
        points.append((t, balance + floating))
    return points


def _first_account_blown_at(equity_points: list) -> Optional[datetime]:
    """Titik PERTAMA equity tembus <= 0 -- ini batas bawah/worst-case: MC
    beneran di broker (stop-out) hampir pasti kejadian LEBIH AWAL dari
    ini (broker mantau margin LEVEL, bukan nunggu equity persis nol),
    jadi kalau ini kepicu, semua P/L SESUDAH titik ini di laporan tidak
    realistis -- akun sudah tidak akan bisa lanjut trading sejauh itu."""
    for t, equity in equity_points:
        if equity <= 0:
            return t
    return None


def _max_drawdown_pct(points: list, initial_deposit: float):
    """Persentase penurunan TERBESAR dari puncak berjalan (standar
    'maximal drawdown %' broker -- relatif ke puncak SEBELUM titik itu,
    bukan ke initial_deposit tetap). Puncak diawali dari initial_deposit
    itu sendiri, supaya loss di trade pertama pun terukur dengan benar.

    PENTING: drawdown % yang BESAR bisa terjadi di AWAL kurun waktu justru
    KARENA puncaknya masih kecil (belum jauh dari initial_deposit) --
    penurunan dolar yang sama akan tampak jauh lebih kecil persentasenya
    kalau terjadi belakangan, setelah puncak sudah naik banyak. Ini bukan
    bug, tapi properti matematis dari % relatif-ke-puncak -- makanya
    fungsi ini juga mengembalikan KAPAN dan di level berapa puncaknya,
    supaya bisa diverifikasi apakah masuk akal.

    Return: (max_dd_pct, waktu_titik_terendah, nilai_puncak_saat_itu)."""
    peak = initial_deposit
    max_dd_pct = 0.0
    at_time = None
    peak_at_dd = initial_deposit
    for t, value in points:
        peak = max(peak, value)
        if peak > 0:
            dd_pct = (peak - value) / peak * 100
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                at_time = t
                peak_at_dd = peak
    return max_dd_pct, at_time, peak_at_dd


def _max_consecutive_losses(chronological_pnls: list) -> int:
    longest = 0
    current = 0
    for pnl in chronological_pnls:
        if pnl <= 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _profit_factor(chronological_pnls: list) -> Optional[float]:
    """gross profit / gross loss -- None kalau tidak ada trade rugi sama
    sekali (rasio tidak terdefinisi, bukan berarti tak terhingga)."""
    gross_profit = sum(p for p in chronological_pnls if p > 0)
    gross_loss = -sum(p for p in chronological_pnls if p <= 0)
    if gross_loss <= 0:
        return None
    return gross_profit / gross_loss


def build_report(trades: list, symbol_specs: dict, skipped: dict, initial_deposit: float = 1000.0) -> BacktestReport:
    wins = 0
    losses = 0
    total_pnl = 0.0
    closed_count = 0
    open_count = 0
    per_symbol: dict = {}
    closed = []  # (entry_time, exit_time, pnl) -- buat max DD/consecutive-loss/profit factor/balance/equity

    for t in trades:
        spec = symbol_specs[t.canonical_symbol]
        stats = per_symbol.setdefault(t.canonical_symbol, {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        stats["trades"] += 1

        pnl = t.realized_pnl_usd
        if t.exit_reason in ("tp", "sl", "partial_close_full", "close_all"):
            if t.remaining_lot > 0:
                pnl += pnl_usd(t, t.exit_price, t.remaining_lot, spec)
            closed_count += 1
            if pnl > 0:
                wins += 1
                stats["wins"] += 1
            else:
                losses += 1
                stats["losses"] += 1
            closed.append((t.entry_time, t.exit_time, pnl))
        else:
            open_count += 1

        total_pnl += pnl
        stats["pnl"] += pnl

    win_rate = (wins / closed_count * 100) if closed_count else 0.0

    ordered_pnls = [pnl for _, _, pnl in sorted(closed, key=lambda c: c[1])]
    balance_points = _balance_curve_points(closed, initial_deposit)
    equity_points = _equity_curve_points(closed, initial_deposit)
    blown_at = _first_account_blown_at(equity_points)
    balance_dd_pct, balance_dd_at, balance_dd_peak = _max_drawdown_pct(balance_points, initial_deposit)
    equity_dd_pct, equity_dd_at, equity_dd_peak = _max_drawdown_pct(equity_points, initial_deposit)

    return BacktestReport(
        total_trades=len(trades),
        closed_trades=closed_count,
        still_open_trades=open_count,
        wins=wins, losses=losses, win_rate=win_rate,
        total_pnl_usd=total_pnl,
        max_drawdown_usd=_max_drawdown_usd(ordered_pnls),
        max_consecutive_losses=_max_consecutive_losses(ordered_pnls),
        profit_factor=_profit_factor(ordered_pnls),
        max_balance_drawdown_pct=balance_dd_pct,
        max_equity_drawdown_pct=equity_dd_pct,
        max_balance_drawdown_at=balance_dd_at,
        max_equity_drawdown_at=equity_dd_at,
        max_balance_drawdown_peak_usd=balance_dd_peak,
        max_equity_drawdown_peak_usd=equity_dd_peak,
        account_blown=blown_at is not None,
        account_blown_at=blown_at,
        per_symbol=per_symbol,
        skipped=skipped,
    )
