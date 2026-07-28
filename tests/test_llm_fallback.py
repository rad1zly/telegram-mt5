import json
import sys

sys.path.insert(0, ".")

from src.parser.llm_fallback import parse_followup_with_llm, parse_signal_with_llm  # noqa: E402


class FakeFunction:
    def __init__(self, arguments: dict):
        self.arguments = json.dumps(arguments)


class FakeToolCall:
    def __init__(self, arguments: dict):
        self.function = FakeFunction(arguments)


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


def _response_with_args(args: dict) -> FakeResponse:
    return FakeResponse([FakeChoice(FakeMessage(tool_calls=[FakeToolCall(args)]))])


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


def test_followup_extracted_with_kind():
    client = FakeClient(response=_response_with_args({"kind": "move_sl_be"}))
    followup = parse_followup_with_llm("amankan posisi ke breakeven ya", message_id=6, reply_to_msg_id=1, client=client)
    assert followup is not None
    assert followup.kind == "move_sl_be"
    assert followup.reply_to_msg_id == 1


def test_followup_rejected_when_no_tool_call():
    client = FakeClient(response=_response_no_tool_call())
    followup = parse_followup_with_llm("semangat pagi semuanya", message_id=7, reply_to_msg_id=None, client=client)
    assert followup is None
