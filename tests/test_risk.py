import sys

sys.path.insert(0, ".")

from src.trading.risk import calculate_lot  # noqa: E402


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
