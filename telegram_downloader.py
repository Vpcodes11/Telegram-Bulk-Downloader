import os
import sys
import asyncio
import logging
import json
from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError, FileReferenceExpiredError
from telethon.tl.types import InputMessagesFilterDocument
from tqdm.asyncio import tqdm

# ==========================================
# CONFIGURATION
# ==========================================
API_ID = 23045850
API_HASH = '3e50e577960343532691044a23567c89'
PHONE_NUMBER = '+919099662234'

SESSION_NAME = 'media_downloader_session'
DOWNLOAD_DIR = 'downloads'
CONCURRENT_DOWNLOADS = 32 # Increased for even more speed
TIMEOUT_PER_FILE = 600
PART_SIZE_KB = 1024 # 1MB chunks
# ==========================================

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

terminal_progress = None
flood_lock = asyncio.Event()
flood_lock.set()
_bytes_downloaded = 0
is_paused = False

async def get_file_name(message):
    if message.file and hasattr(message.file, 'name') and message.file.name:
        return message.file.name
    ext = (message.file.ext if hasattr(message.file, 'ext') and message.file.ext else '.unknown') if message.file else '.unknown'
    return f"{message.id}{ext}"

async def get_media_type(message):
    if message.photo: return 'photos'
    elif message.video: 
        if message.video.attributes and any(hasattr(a, 'round_message') and a.round_message for a in message.video.attributes):
            return 'round_video'
        return 'videos'
    elif message.voice: return 'voice'
    elif message.audio: return 'audio'
    elif message.document: return 'files'
    elif message.gif: return 'gifs'
    else: return 'other'

async def refresh_message(client, message):
    try:
        fresh = await client.get_messages(message.chat_id, ids=message.id)
        return fresh if fresh else message
    except Exception as e:
        logger.warning(f"Could not refresh message {message.id}: {e}")
        return message

async def fast_download(client, message, filepath):
    global _bytes_downloaded
    if not message.file: return
    
    last_received = 0
    def progress_callback(received, total):
        nonlocal last_received
        global _bytes_downloaded
        delta = received - last_received
        if delta > 0:
            _bytes_downloaded += delta
            last_received = received

    try:
        await asyncio.wait_for(
            client.download_file(
                message.document or message.photo or message.video or message.audio or message.voice,
                file=filepath,
                part_size_kb=PART_SIZE_KB,
                progress_callback=progress_callback
            ),
            timeout=TIMEOUT_PER_FILE
        )
    except asyncio.TimeoutError:
        raise

async def download_worker(queue, client, chat_dir):
    global terminal_progress, flood_lock, is_paused
    while True:
        try:
            if not flood_lock.is_set():
                await flood_lock.wait()

            message = await queue.get()
            m_type = await get_media_type(message)
            type_dir = os.path.join(chat_dir, m_type)
            os.makedirs(type_dir, exist_ok=True)
            
            # UNIQUE FILENAME to prevent collision errors
            orig_name = await get_file_name(message)
            clean_name = "".join([c for c in orig_name if c.isalnum() or c in ' ._-()']).rstrip()
            if not clean_name: clean_name = "file.unknown"
            filename = f"{message.id}_{clean_name}"
            
            filepath = os.path.join(type_dir, filename)
            
            # Deduplication
            if os.path.exists(filepath) and os.path.getsize(filepath) == (message.file.size if message.file else 0):
                if terminal_progress: terminal_progress.update(1)
                queue.task_done()
                continue

            try:
                part_path = filepath + ".part"
                try:
                    await fast_download(client, message, part_path)
                except FileReferenceExpiredError:
                    message = await refresh_message(client, message)
                    await fast_download(client, message, part_path)
                
                if os.path.exists(part_path):
                    if os.path.exists(filepath): os.remove(filepath) # Safety overwrite
                    os.rename(part_path, filepath)
            except FloodWaitError as e:
                if flood_lock.is_set():
                    flood_lock.clear()
                    is_paused = True
                    for remaining in range(e.seconds, 0, -1):
                        if terminal_progress:
                            terminal_progress.set_postfix(status=f"⏳ PAUSED {remaining}s")
                        await asyncio.sleep(1)
                    is_paused = False
                    flood_lock.set()
                
                queue.put_nowait(message)
                queue.task_done()
                continue
            except asyncio.TimeoutError:
                queue.put_nowait(message)
            except Exception as e:
                logger.error(f"Error {filename}: {e}")
                if os.path.exists(filepath + ".part"):
                    try: os.remove(filepath + ".part")
                    except: pass
            
            if terminal_progress: terminal_progress.update(1)
            queue.task_done()
        except asyncio.CancelledError: break
        except Exception: 
            if not queue.empty(): queue.task_done()

async def producer(client, entity, queue, all_messages):
    """Scans for messages and puts them in the queue immediately."""
    global terminal_progress
    count = 0
    # Try filtering for documents first (faster)
    async for msg in client.iter_messages(entity, filter=InputMessagesFilterDocument):
        if not msg.media: continue
        all_messages.append(msg)
        queue.put_nowait(msg)
        count += 1
        if terminal_progress:
            terminal_progress.total = count
            terminal_progress.refresh()
    
    # Also scan normally for other media types (photos, etc)
    async for msg in client.iter_messages(entity):
        # Skip if already found in document filter
        if any(m.id == msg.id for m in all_messages): continue
        all_messages.append(msg)
        if msg.media:
            queue.put_nowait(msg)
            count += 1
            if terminal_progress:
                terminal_progress.total = count
                terminal_progress.refresh()

async def main():
    print("\n🚀 FLASH CMD TELEGRAM DOWNLOADER (ULTRA-INSTANT START)")
    client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH, connection_retries=None, auto_reconnect=True)
    await client.start(phone=PHONE_NUMBER)
    
    chat_input = input("\nEnter @channelname or link: ").strip()
    try:
        entity = await client.get_entity(chat_input)
    except Exception as e:
        print(f"Error finding chat: {e}")
        return

    chat_title = "".join([c for c in getattr(entity, 'title', 'chat') if c.isalnum() or c in ' ._-']).strip()
    chat_dir = os.path.join(DOWNLOAD_DIR, chat_title)
    os.makedirs(chat_dir, exist_ok=True)

    print(f"⚡ Target: {chat_title}")
    
    queue = asyncio.Queue()
    all_messages = []
    
    global terminal_progress
    terminal_progress = tqdm(total=0, desc="🚀 FLASH DOWNLOAD", unit="file", leave=True)
    
    # Start workers
    workers = [asyncio.create_task(download_worker(queue, client, chat_dir)) for _ in range(CONCURRENT_DOWNLOADS)]
    
    # Start speed monitor
    async def monitor_speed():
        global _bytes_downloaded, is_paused
        prev = 0
        while True:
            await asyncio.sleep(1)
            curr = _bytes_downloaded
            mbps = (curr - prev) / (1024 * 1024)
            prev = curr
            if not is_paused and terminal_progress:
                terminal_progress.set_postfix(speed=f"🚀 {mbps:.2f} MB/s")

    speed_task = asyncio.create_task(monitor_speed())
    
    # Start producer (Scanner)
    print("⏳ Scanning & Downloading concurrently...")
    await producer(client, entity, queue, all_messages)
    
    # Wait for all downloads to finish
    await queue.join()
    
    # Cleanup
    for w in workers: w.cancel()
    speed_task.cancel()
    terminal_progress.close()
    
    print(f"✅ Downloads complete! Total: {len(all_messages)} messages processed.")
    await client.disconnect()

if __name__ == '__main__':
    if sys.platform == 'win32': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Stopped by user.")
    except Exception as e:
        print(f"\n❌ Error: {e}")
