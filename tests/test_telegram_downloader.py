import sys
from unittest.mock import MagicMock

# Mock dependencies before importing telegram_downloader
mock_telethon = MagicMock()
sys.modules['telethon'] = mock_telethon
sys.modules['telethon.errors'] = MagicMock()
sys.modules['tqdm'] = MagicMock()
sys.modules['tqdm.asyncio'] = MagicMock()

import unittest
from telegram_downloader import get_media_type

class TestGetMediaType(unittest.IsolatedAsyncioTestCase):
    async def test_photo(self):
        message = MagicMock()
        message.photo = True
        message.video = False
        message.voice = False
        message.audio = False
        message.document = False
        message.gif = False
        self.assertEqual(await get_media_type(message), 'photos')

    async def test_video(self):
        message = MagicMock()
        message.photo = False
        message.video = MagicMock()
        message.video.attributes = []
        message.voice = False
        message.audio = False
        message.document = False
        message.gif = False
        self.assertEqual(await get_media_type(message), 'videos')

    async def test_round_video(self):
        message = MagicMock()
        message.photo = False

        attr = MagicMock()
        attr.round_message = True

        # We need to make sure type(message.video.attributes[0]) check passes
        # The code uses type(message.video.attributes[0])
        message.video = MagicMock()
        message.video.attributes = [attr]

        message.voice = False
        message.audio = False
        message.document = False
        message.gif = False
        self.assertEqual(await get_media_type(message), 'round_video')

    async def test_voice(self):
        message = MagicMock()
        message.photo = False
        message.video = False
        message.voice = True
        message.audio = False
        message.document = False
        message.gif = False
        self.assertEqual(await get_media_type(message), 'voice')

    async def test_audio(self):
        message = MagicMock()
        message.photo = False
        message.video = False
        message.voice = False
        message.audio = True
        message.document = False
        message.gif = False
        self.assertEqual(await get_media_type(message), 'audio')

    async def test_document(self):
        message = MagicMock()
        message.photo = False
        message.video = False
        message.voice = False
        message.audio = False
        message.document = True
        message.gif = False
        self.assertEqual(await get_media_type(message), 'files')

    async def test_gif(self):
        message = MagicMock()
        message.photo = False
        message.video = False
        message.voice = False
        message.audio = False
        message.document = False
        message.gif = True
        self.assertEqual(await get_media_type(message), 'gifs')

    async def test_other(self):
        message = MagicMock()
        message.photo = False
        message.video = False
        message.voice = False
        message.audio = False
        message.document = False
        message.gif = False
        # To test the default case, we need to make sure it doesn't have gif attr or it is False
        # MagicMock has everything by default, so we might need to delete it if we use hasattr
        if hasattr(message, 'gif'):
            message.gif = False
        self.assertEqual(await get_media_type(message), 'other')

if __name__ == '__main__':
    unittest.main()
