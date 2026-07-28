"""Helper resolusi channel Telegram, dipakai bareng oleh recorder Fase 1
(tools/collect_signals.py) dan pipeline penuh (src/main.py)."""

from telethon.tl.types import PeerChannel


def as_channel_ref(channel):
    """Terima username ('@nama'), ID polos, atau ID bertanda '-100...'
    (format yang biasa muncul dari bot semacam @getidsbot atau Telegram
    Web) dan kembalikan bentuk yang bisa diresolve Telethon. Channel
    private HANYA bisa diresolve kalau akun ini sudah pernah 'melihat'
    entity-nya — pemanggil harus lebih dulu memanggil client.get_dialogs()
    supaya cache access_hash terisi."""
    if isinstance(channel, str) and not channel.lstrip("-").isdigit():
        return channel  # username, mis. "@nama_channel"

    raw_id = int(channel)
    if raw_id < 0:
        s = str(raw_id)
        if s.startswith("-100"):
            return PeerChannel(int(s[4:]))
        return raw_id
    return PeerChannel(raw_id)


async def resolve_channel_entity(client, channel):
    """Sinkron dialog dulu (supaya channel private ke-cache), lalu resolve.
    Raises ValueError/TypeError kalau tidak ketemu — biarkan pemanggil
    yang menangani pesan errornya sendiri (beda konteks tools vs service)."""
    await client.get_dialogs()
    return await client.get_entity(as_channel_ref(channel))
