import sys

sys.path.insert(0, ".")

from src.trading.risk import calculate_lot, calculate_partial_close_volume  # noqa: E402


def test_calculate_lot_basic():
    result = calculate_lot(
        entry=4344.5, sl=4348.0,
        tick_size=0.01, tick_value=1.0,
        volume_step=0.01, volume_min=0.01, volume_max=100.0,
        risk_usd=50.0, max_lot_cap=5.0,
    )
    # sl_distance=3.5 -> loss_per_lot=(3.5/0.01)*1.0=350 -> lot_raw=50/350=0.1428..
    # -> di-floor ke kelipatan 0.01 -> 0.14
    assert result.ok
    assert result.lot == 0.14
    assert not result.capped


def test_calculate_lot_capped_when_sl_very_close():
    result = calculate_lot(
        entry=100.0, sl=100.01,
        tick_size=0.01, tick_value=1.0,
        volume_step=0.01, volume_min=0.01, volume_max=100.0,
        risk_usd=50.0, max_lot_cap=5.0,
    )
    # sl_distance=0.01 -> loss_per_lot=1.0 -> lot_raw=50 -> dipotong ke max_lot_cap=5.0
    assert result.ok
    assert result.lot == 5.0
    assert result.capped


def test_calculate_lot_rejected_when_below_volume_min():
    result = calculate_lot(
        entry=100.0, sl=110.0,
        tick_size=0.01, tick_value=1.0,
        volume_step=0.01, volume_min=0.01, volume_max=100.0,
        risk_usd=0.05, max_lot_cap=5.0,
    )
    assert not result.ok
    assert result.error is not None


def test_calculate_lot_rejects_zero_sl_distance():
    result = calculate_lot(
        entry=100.0, sl=100.0,
        tick_size=0.01, tick_value=1.0,
        volume_step=0.01, volume_min=0.01, volume_max=100.0,
        risk_usd=50.0, max_lot_cap=5.0,
    )
    assert not result.ok


def test_calculate_lot_rejects_invalid_broker_tick_info():
    result = calculate_lot(
        entry=100.0, sl=105.0,
        tick_size=0.0, tick_value=1.0,
        volume_step=0.01, volume_min=0.01, volume_max=100.0,
        risk_usd=50.0, max_lot_cap=5.0,
    )
    assert not result.ok


def test_partial_close_normal_case():
    result = calculate_partial_close_volume(
        position_lot=0.2, percent=50, volume_step=0.01, volume_min=0.01,
    )
    assert result.action == "partial"
    assert result.volume == 0.1


def test_partial_close_rounds_down_to_step_not_up():
    # 0.07 lot * 50% = 0.035 -> harus dibulatkan KE BAWAH ke 0.03, bukan 0.04
    result = calculate_partial_close_volume(
        position_lot=0.07, percent=50, volume_step=0.01, volume_min=0.01,
    )
    assert result.action == "partial"
    assert result.volume == 0.03


def test_partial_close_falls_back_to_full_when_step_too_coarse():
    # index dengan volume_step=1.0: 50% dari lot 1.0 = 0.5, tidak valid
    # (di bawah step) -> harus tutup penuh, bukan kirim 0.5 yang ditolak broker
    result = calculate_partial_close_volume(
        position_lot=1.0, percent=50, volume_step=1.0, volume_min=1.0,
    )
    assert result.action == "full"


def test_partial_close_falls_back_to_full_when_remainder_too_small():
    # broker dengan volume_min (0.05) LEBIH BESAR dari volume_step (0.01):
    # close_volume=0.09 valid (>= min), tapi sisanya 0.03 di bawah volume_min
    # -> harus tutup penuh, bukan tinggalkan sisa yang tidak valid.
    result = calculate_partial_close_volume(
        position_lot=0.12, percent=80, volume_step=0.01, volume_min=0.05,
    )
    assert result.action == "full"


def test_partial_close_works_fine_with_coarse_step_when_lot_large_enough():
    result = calculate_partial_close_volume(
        position_lot=2.0, percent=50, volume_step=1.0, volume_min=1.0,
    )
    assert result.action == "partial"
    assert result.volume == 1.0


def test_partial_close_rejects_invalid_position_lot():
    result = calculate_partial_close_volume(
        position_lot=0.0, percent=50, volume_step=0.01, volume_min=0.01,
    )
    assert result.action == "reject"
    assert not result.ok
