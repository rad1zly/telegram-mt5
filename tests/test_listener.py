import sys

sys.path.insert(0, ".")

from telethon.tl.types import PeerChannel  # noqa: E402

from src.tg.listener import as_channel_ref  # noqa: E402


def test_username_passthrough():
    assert as_channel_ref("@nama_channel") == "@nama_channel"


def test_marked_negative_id_as_int():
    ref = as_channel_ref(-1001234567890)
    assert isinstance(ref, PeerChannel)
    assert ref.channel_id == 1234567890


def test_marked_negative_id_as_string():
    ref = as_channel_ref("-1001234567890")
    assert isinstance(ref, PeerChannel)
    assert ref.channel_id == 1234567890


def test_bare_positive_id():
    ref = as_channel_ref(1234567890)
    assert isinstance(ref, PeerChannel)
    assert ref.channel_id == 1234567890
