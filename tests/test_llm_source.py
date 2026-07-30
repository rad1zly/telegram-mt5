import json
import sys

sys.path.insert(0, ".")

from backtest.llm_source import load_llm_cache, make_llm_classify_fn  # noqa: E402
from src.parser.schema import FollowUp, Signal  # noqa: E402


def test_load_llm_cache_reads_jsonl(tmp_path):
    path = tmp_path / "cache.jsonl"
    path.write_text(
        json.dumps({"message_id": 1, "type": "signal", "action": "BUY", "symbol": "GOLD", "sl": 4340.0, "tp": [4360.0]}) + "\n"
        + json.dumps({"message_id": 2, "type": "none"}) + "\n"
    )
    cache = load_llm_cache(str(path))
    assert set(cache.keys()) == {1, 2}
    assert cache[1]["symbol"] == "GOLD"


def test_load_llm_cache_last_duplicate_wins(tmp_path):
    path = tmp_path / "cache.jsonl"
    path.write_text(
        json.dumps({"message_id": 1, "type": "none"}) + "\n"
        + json.dumps({"message_id": 1, "type": "signal", "action": "SELL", "symbol": "GOLD", "sl": 4350.0, "tp": []}) + "\n"
    )
    cache = load_llm_cache(str(path))
    assert cache[1]["type"] == "signal"
    assert cache[1]["action"] == "SELL"


def test_classify_fn_reconstructs_signal_with_entry_range():
    cache = {
        5: {"message_id": 5, "type": "signal", "action": "BUY", "symbol": "EURUSD",
            "entry": None, "entry_range": [1.0810, 1.0820], "sl": 1.0850, "tp": [1.0780]},
    }
    fn = make_llm_classify_fn(cache)
    result = fn("apa pun teksnya", message_id=5, reply_to_msg_id=None)
    assert isinstance(result, Signal)
    assert result.entry is None
    assert result.entry_range == (1.0810, 1.0820)
    assert result.sl == 1.0850


def test_classify_fn_reconstructs_followup():
    cache = {7: {"message_id": 7, "type": "followup", "kinds": ["partial_close_tp1", "move_sl_be"], "symbol": "US30"}}
    fn = make_llm_classify_fn(cache)
    result = fn("teks apa pun", message_id=7, reply_to_msg_id=3)
    assert isinstance(result, FollowUp)
    assert set(result.kinds) == {"partial_close_tp1", "move_sl_be"}
    assert result.reply_to_msg_id == 3


def test_classify_fn_returns_none_for_type_none():
    cache = {9: {"message_id": 9, "type": "none"}}
    fn = make_llm_classify_fn(cache)
    assert fn("weekly recap", message_id=9, reply_to_msg_id=None) is None


def test_classify_fn_returns_none_when_message_not_in_cache():
    fn = make_llm_classify_fn({})
    assert fn("teks", message_id=999, reply_to_msg_id=None) is None
