import sys

sys.path.insert(0, ".")

from src.trading.symbols import SymbolResolver, normalize  # noqa: E402

ALIASES = {
    "XAUUSD": ["GOLD", "XAUUSD", "XAU/USD", "GOLDUSD"],
    "NAS100": ["NAS100", "USTEC", "US100", "NASDAQ"],
    "EURUSD": ["EURUSD", "EUR/USD"],
}


def test_normalize_strips_punctuation_but_not_letters():
    assert normalize("XAU/USD") == "XAUUSD"
    assert normalize("xauusd") == "XAUUSD"
    assert normalize("XAUUSDm") == "XAUUSDM"  # suffix letter preserved on purpose


def test_exact_single_match_no_override_needed():
    resolver = SymbolResolver(ALIASES)
    result = resolver.resolve("GOLD", ["XAUUSD", "EURUSD", "NAS100"])
    assert result.ok
    assert result.matched == "XAUUSD"
    assert result.canonical == "XAUUSD"


def test_punctuation_suffix_is_auto_matched():
    # kasus persis skenario nyata: channel bilang "GOLD", broker menamainya "XAUUSD+".
    # "+" cuma tanda baca (dibuang saat normalize), jadi ini aman di-auto-match.
    resolver = SymbolResolver(ALIASES)
    result = resolver.resolve("GOLD", ["XAUUSD+", "EURUSD"])
    assert result.ok
    assert result.matched == "XAUUSD+"


def test_letter_suffix_variant_is_not_auto_matched():
    # broker menamainya "XAUUSDm" (huruf, bukan tanda baca) -> normalized core
    # beda dari "XAUUSD" -> harus ditolak, butuh broker_overrides eksplisit
    resolver = SymbolResolver(ALIASES)
    result = resolver.resolve("GOLD", ["XAUUSDm", "EURUSD"])
    assert not result.ok
    assert result.error is not None


def test_no_ambiguity_when_only_one_symbol_matches_normalized_core():
    resolver = SymbolResolver(ALIASES)
    result = resolver.resolve("GOLD", ["XAUUSD", "XAUUSDT", "EURUSD"])
    # normalize("XAUUSDT") = "XAUUSDT" != "XAUUSD", jadi cuma 1 exact match.
    # Ambiguitas nyata (2 simbol dengan normalized core sama) diuji di bawah lewat override.
    assert result.ok
    assert result.matched == "XAUUSD"


def test_true_ambiguity_between_two_identical_normalized_symbols_is_impossible_by_construction():
    # sqlite/broker tidak mungkin punya 2 simbol persis sama namanya; ambiguitas nyata
    # muncul kalau alias table punya 2 canonical berbeda yang normalize ke kunci sama.
    # Uji cara override menyelesaikan itu secara eksplisit:
    resolver = SymbolResolver(ALIASES, broker_overrides={"XAUUSD": "XAUUSDm"})
    result = resolver.resolve("GOLD", ["XAUUSD", "XAUUSDm"])
    assert result.matched == "XAUUSDm"


def test_override_wins_even_when_exact_match_exists():
    resolver = SymbolResolver(ALIASES, broker_overrides={"NAS100": "USTECm"})
    result = resolver.resolve("USTEC", ["NAS100", "USTECm"])
    assert result.matched == "USTECm"


def test_override_pointing_to_missing_broker_symbol_errors():
    resolver = SymbolResolver(ALIASES, broker_overrides={"XAUUSD": "XAUUSD.raw"})
    result = resolver.resolve("GOLD", ["XAUUSD", "EURUSD"])
    assert not result.ok
    assert "XAUUSD.raw" in result.error


def test_unknown_token_not_in_alias_table_rejected():
    resolver = SymbolResolver(ALIASES)
    result = resolver.resolve("UNKNOWNPAIR123", ["XAUUSD", "EURUSD"])
    assert not result.ok
    assert result.canonical is None


def test_token_in_alias_table_but_absent_from_broker_rejected():
    resolver = SymbolResolver(ALIASES)
    result = resolver.resolve("GOLD", ["EURUSD"])  # broker tidak punya XAUUSD sama sekali
    assert not result.ok
    assert result.canonical == "XAUUSD"


def test_suggest_returns_prefix_matches_for_human_confirmation():
    resolver = SymbolResolver(ALIASES)
    suggestions = resolver.suggest("GOLD", ["XAUUSD+", "XAUUSDm", "XAUUSD.raw", "EURUSD"])
    assert set(suggestions) == {"XAUUSD+", "XAUUSDm", "XAUUSD.raw"}
