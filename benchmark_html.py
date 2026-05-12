import sys
from unittest.mock import MagicMock

# Mock dependencies
sys.modules['fastapi'] = MagicMock()
sys.modules['fastapi.responses'] = MagicMock()
sys.modules['fastapi.staticfiles'] = MagicMock()
sys.modules['pydantic'] = MagicMock()
sys.modules['telethon'] = MagicMock()
sys.modules['telethon.errors'] = MagicMock()
sys.modules['tqdm'] = MagicMock()
sys.modules['tqdm.asyncio'] = MagicMock()
sys.modules['uvicorn'] = MagicMock()

import asyncio
import time
import os
import tempfile
from datetime import datetime
import web_app

class DummySender:
    def __init__(self, first_name, last_name, username):
        self.first_name = first_name
        self.last_name = last_name
        self.username = username

class DummyMessage:
    def __init__(self, id, action=None, sender=None, date=None, text=None, media=False, export_path=None):
        self.id = id
        self.action = action
        self.sender = sender
        self.date = date or datetime.now()
        self.text = text
        self.media = media
        self._export_path = export_path

# To mock get_media_type, we monkey-patch it for the benchmark
async def mock_get_media_type(msg):
    return 'photos'

web_app.get_media_type = mock_get_media_type

async def main():
    # Create 50,000 dummy messages to exaggerate the string concatenation cost
    messages = []
    for i in range(50000):
        sender = DummySender(f"First{i}", f"Last{i}", f"User{i}")
        msg = DummyMessage(
            id=i,
            sender=sender,
            text=f"This is a dummy message {i} with some text to make it longer and more realistic.",
            media=(i % 5 == 0),
            export_path=f"photos/photo_{i}.jpg" if (i % 5 == 0) else None
        )
        messages.append(msg)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Warmup (optional, to compile python bytecode)
        await web_app.generate_html_export("Benchmark Chat", messages[:10], tmpdir)

        start_time = time.time()
        await web_app.generate_html_export("Benchmark Chat", messages, tmpdir)
        end_time = time.time()

        duration = end_time - start_time
        file_size = os.path.getsize(os.path.join(tmpdir, "export_history.html"))

        print(f"Time taken: {duration:.4f} seconds")
        print(f"File size: {file_size / 1024 / 1024:.2f} MB")

if __name__ == "__main__":
    asyncio.run(main())
