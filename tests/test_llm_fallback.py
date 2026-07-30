import json
import sys

sys.path.insert(0, ".")

from src.parser.llm_fallback import (  # noqa: E402
    classify_message_with_llm,
    parse_followup_with_llm,
    parse_signal_with_llm,
)
from src.parser.schema import FollowUp, Signal  # noqa: E402


class FakeFunction:
    def __init__(self, arguments: dict, name: str = "extract_signal"):
        self.arguments = json.dumps(arguments)
        self.name = name


class FakeToolCall:
    def __init__(self, arguments: dict, name: str = "extract_signal"):
        self.function = FakeFunction(arguments, name=name)


class FakeMessage:
    def __init__(self, tool_calls=None):
        self.tool_calls = tool_calls


class FakeChoice:
    def __init__(self, message):
        self.message = message


class FakeResponse:
    def __init__(self, choices):
        self.choices = choices


class FakeCompletions:
    def __init__(self, response=None, exception=None):
        self._response = response
        self._exception = exception
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self._exception:
            raise self._exception
        return self._response


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    def __init__(self, response=None, exception=None):
        self.chat = FakeChat(FakeCompletions(response, exception))


def _response_with_args(args: dict, name: str = "extract_signal") -> FakeResponse:
    return FakeResponse([FakeChoice(FakeMessage(tool_calls=[FakeToolCall(args, name=name)]))])


def _response_no_tool_call() -> FakeResponse:
    return FakeResponse([FakeChoice(FakeMessage(tool_calls=None))])


def test_signal_extracted_when_model_calls_tool_with_complete_args():
    args = {"action": "BUY", "symbol": "GOLD", "entry": 2350.0, "sl": 2340.0, "tp": [2360.0, 2370.0]}
    client = FakeClient(response=_response_with_args(args))

    signal = parse_signal_with_llm("BUY GOLD di 2350, SL 2340, TP 2360/2370", message_id=1, client=client)

    assert signal is not None
    assert signal.action == "BUY"
    assert signal.symbol == "GOLD"
    assert signal.entry == 2350.0
    assert signal.sl == 2340.0
    assert signal.tp == [2360.0, 2370.0]
    # tools dikirim ke API, bukan diasumsikan
    assert client.chat.completions.last_kwargs["tools"][0]["function"]["name"] == "extract_signal"


def test_signal_rejected_when_model_declines_to_call_tool():
    client = FakeClient(response=_response_no_tool_call())
    signal = parse_signal_with_llm("update market hari ini santai", message_id=2, client=client)
    assert signal is None


def test_signal_rejected_when_required_field_missing():
    # model memanggil tool tapi lupa sl -> harus ditolak, bukan pakai None diam-diam
    args = {"action": "BUY", "symbol": "GOLD", "entry": 2350.0}
    client = FakeClient(response=_response_with_args(args))
    signal = parse_signal_with_llm("BUY GOLD di 2350", message_id=3, client=client)
    assert signal is None


def test_signal_returns_none_on_api_exception_not_crash():
    client = FakeClient(exception=RuntimeError("network down"))
    signal = parse_signal_with_llm("BUY GOLD di 2350 SL 2340", message_id=4, client=client)
    assert signal is None


def test_signal_entry_range_parsed_when_both_bounds_present():
    args = {
        "action": "SELL",
        "symbol": "EURUSD",
        "entry_range_low": 1.0810,
        "entry_range_high": 1.0820,
        "sl": 1.0850,
        "tp": [1.0780],
    }
    client = FakeClient(response=_response_with_args(args))
    signal = parse_signal_with_llm("SELL EURUSD 1.0810-1.0820 SL 1.0850 TP 1.0780", message_id=5, client=client)
    assert signal is not None
    assert signal.entry_range == (1.0810, 1.0820)
    assert signal.entry is None


def test_followup_extracted_with_single_kind():
    client = FakeClient(response=_response_with_args({"kinds": ["move_sl_be"], "symbol": "GOLD"}))
    followup = parse_followup_with_llm("amankan posisi ke breakeven ya", message_id=6, reply_to_msg_id=1, client=client)
    assert followup is not None
    assert followup.kinds == ["move_sl_be"]
    assert followup.reply_to_msg_id == 1
    assert followup.symbol == "GOLD"


def test_followup_extracted_with_multiple_kinds():
    # kasus nyata: "close partially AND move SL to entry" dalam satu pesan
    client = FakeClient(response=_response_with_args({"kinds": ["partial_close_tp1", "move_sl_be"]}))
    followup = parse_followup_with_llm(
        "You may close partially to secure gains and move the stop-loss to the entry.",
        message_id=7,
        reply_to_msg_id=None,
        client=client,
    )
    assert followup is not None
    assert set(followup.kinds) == {"partial_close_tp1", "move_sl_be"}


def test_followup_recognized_but_no_actionable_kind_is_info_only():
    # model memanggil tool (yakin ini follow-up) tapi kinds kosong -> info-only,
    # BEDA dari model tidak memanggil tool sama sekali (return None)
    client = FakeClient(response=_response_with_args({"kinds": []}))
    followup = parse_followup_with_llm(
        "close fully position or Close partially and place your sl around 4349",
        message_id=8,
        reply_to_msg_id=None,
        client=client,
    )
    assert followup is not None
    assert followup.kinds == []


def test_followup_rejected_when_no_tool_call():
    client = FakeClient(response=_response_no_tool_call())
    followup = parse_followup_with_llm("semangat pagi semuanya", message_id=7, reply_to_msg_id=None, client=client)
    assert followup is None


class TestClassifyMessageWithLLM:
    """classify_message_with_llm: SATU panggilan LLM per pesan, KEDUA tool
    ditawarkan sekaligus, model sendiri yang pilih (mode trial 'llm_first')."""

    def test_returns_signal_when_model_calls_extract_signal(self):
        args = {"action": "BUY", "symbol": "GOLD", "entry": 2350.0, "sl": 2340.0, "tp": [2360.0]}
        client = FakeClient(response=_response_with_args(args, name="extract_signal"))

        result = classify_message_with_llm("BUY GOLD 2350 SL 2340 TP 2360", message_id=1, reply_to_msg_id=None, client=client)

        assert isinstance(result, Signal)
        assert result.action == "BUY"
        assert result.symbol == "GOLD"
        # kedua tool DIKIRIM sekaligus, bukan cuma satu
        tool_names = {t["function"]["name"] for t in client.chat.completions.last_kwargs["tools"]}
        assert tool_names == {"extract_signal", "extract_followup"}

    def test_returns_followup_when_model_calls_extract_followup(self):
        args = {"kinds": ["partial_close_tp1"], "symbol": "US30"}
        client = FakeClient(response=_response_with_args(args, name="extract_followup"))

        result = classify_message_with_llm("close partially please", message_id=2, reply_to_msg_id=5, client=client)

        assert isinstance(result, FollowUp)
        assert result.kinds == ["partial_close_tp1"]
        assert result.symbol == "US30"
        assert result.reply_to_msg_id == 5

    def test_returns_none_when_model_calls_no_tool(self):
        client = FakeClient(response=_response_no_tool_call())
        result = classify_message_with_llm("weekly results recap, +80% win rate", message_id=3, reply_to_msg_id=None, client=client)
        assert result is None

    def test_returns_none_when_signal_missing_required_field(self):
        args = {"action": "BUY", "symbol": "GOLD"}  # sl kosong
        client = FakeClient(response=_response_with_args(args, name="extract_signal"))
        result = classify_message_with_llm("BUY GOLD 2350", message_id=4, reply_to_msg_id=None, client=client)
        assert result is None

    def test_returns_none_on_api_exception_not_crash(self):
        client = FakeClient(exception=RuntimeError("network down"))
        result = classify_message_with_llm("BUY GOLD 2350 SL 2340", message_id=5, reply_to_msg_id=None, client=client)
        assert result is None
