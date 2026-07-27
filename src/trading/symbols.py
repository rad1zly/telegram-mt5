import re
from dataclasses import dataclass, field
from typing import Optional


def normalize(name: str) -> str:
    """Uppercase + buang karakter non-alfanumerik. 'XAU/USD' dan 'xauusd'
    jadi sama ('XAUUSD'), tapi 'XAUUSD' vs 'XAUUSDm' TETAP berbeda —
    suffix huruf sengaja tidak dibuang karena sering berarti varian
    akun/spread yang berbeda di broker, bukan sekadar gaya penulisan."""
    return re.sub(r"[^A-Z0-9]", "", name.upper())


@dataclass
class ResolveResult:
    matched: Optional[str]
    canonical: Optional[str]
    ambiguous: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.matched is not None


class SymbolResolver:
    """Menyamakan nama simbol dari channel Telegram ('GOLD', 'NAS100', dst)
    dengan nama simbol persis di broker MT5 ('XAUUSD+', 'NAS100m', dst).

    Prinsip: kalau tidak yakin, TOLAK — jangan menebak simbol untuk order
    sungguhan. Ambiguitas (lebih dari satu simbol broker cocok) hanya bisa
    diselesaikan lewat broker_overrides eksplisit di config, diisi via
    tools/map_symbols.py setelah dikonfirmasi manusia.
    """

    def __init__(self, aliases: dict[str, list[str]], broker_overrides: Optional[dict[str, str]] = None):
        self._alias_to_canonical: dict[str, str] = {}
        for canonical, spellings in aliases.items():
            self._alias_to_canonical[normalize(canonical)] = canonical
            for spelling in spellings:
                self._alias_to_canonical[normalize(spelling)] = canonical
        self._broker_overrides = {normalize(k): v for k, v in (broker_overrides or {}).items()}

    def canonical_of(self, channel_token: str) -> Optional[str]:
        return self._alias_to_canonical.get(normalize(channel_token))

    def resolve(self, channel_token: str, broker_symbols: list[str]) -> ResolveResult:
        canonical = self.canonical_of(channel_token)
        norm_key = normalize(canonical or channel_token)

        # 1. Override eksplisit menang selalu — ini yang menyelesaikan ambiguitas
        if norm_key in self._broker_overrides:
            override = self._broker_overrides[norm_key]
            if override in broker_symbols:
                return ResolveResult(matched=override, canonical=canonical)
            return ResolveResult(
                matched=None,
                canonical=canonical,
                error=f"broker_overrides menunjuk '{override}' untuk "
                      f"{canonical or channel_token}, tapi simbol itu tidak ada di broker sekarang",
            )

        # 2. Tanpa override: cocokkan exact pada bentuk yang dinormalisasi
        broker_norm: dict[str, list[str]] = {}
        for sym in broker_symbols:
            broker_norm.setdefault(normalize(sym), []).append(sym)

        candidates = broker_norm.get(norm_key, [])

        if len(candidates) == 1:
            return ResolveResult(matched=candidates[0], canonical=canonical)

        if len(candidates) > 1:
            return ResolveResult(
                matched=None,
                canonical=canonical,
                ambiguous=candidates,
                error=f"{len(candidates)} simbol broker cocok untuk '{channel_token}': "
                      f"{candidates} — tambahkan broker_overrides di config untuk memilih salah satu",
            )

        return ResolveResult(
            matched=None,
            canonical=canonical,
            error=(
                f"Tidak ada simbol broker yang cocok untuk '{channel_token}'"
                + (f" (dikenali sebagai {canonical})" if canonical else " (tidak ada di alias table — tambahkan dulu)")
            ),
        )

    def suggest(self, channel_token: str, broker_symbols: list[str]) -> list[str]:
        """Dipakai tools/map_symbols.py: cari simbol broker yang normalized
        form-nya DIAWALI oleh core kanonik, sebagai kandidat untuk
        dikonfirmasi manusia — bukan untuk auto-resolve saat trading."""
        canonical = self.canonical_of(channel_token) or channel_token
        core = normalize(canonical)
        return [s for s in broker_symbols if normalize(s).startswith(core)]
