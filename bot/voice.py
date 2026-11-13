from asyncio import timeout
from io import BytesIO

from aiogram import Bot


MAX_VOICE_SECONDS = 60
MAX_VOICE_BYTES = 2 * 1024 * 1024


class VoiceTooLarge(Exception):
    """The download exceeded the voice message size limit."""


class VoiceBuffer(BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_VOICE_BYTES:
            raise VoiceTooLarge
        return super().write(data)


async def download_voice(bot: Bot, file_id: str) -> bytes:
    with VoiceBuffer() as destination:
        async with timeout(15):
            await bot.download(file_id, destination=destination, seek=False)
        return destination.getvalue()
