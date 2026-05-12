import os
import sys
import asyncio
import logging
from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError, FileReferenceExpiredError
from tqdm.asyncio import tqdm

# ==========================================
# CONFIGURATION
# ==========================================
API_ID = 23045850
API_HASH = '3e50e577960343532691044a23567c89'
PHONE_NUMBER = '+919099662234'

SESSION_NAME = 'media_downloader_session'
DOWNLOAD_DIR = 'downloads'
CONCURRENT_DOWNLOADS = 32 # Maximum workers for high-speed
TIMEOUT_PER_FILE = 600
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
    elif message.video: return 'videos'
    elif message.voice: return 'voice'
    elif message.audio: return 'audio'
    elif message.document: return 'files'
    else: return 'other'

async def refresh_message(client, message):
    try:
        fresh = await client.get_messages(message.chat_id, ids=message.id)
        return fresh if fresh else message
    except Exception as e:
        logger.warning(f"Could not refresh message {message.id}: {e}")
        return message

async def download_worker(queue, client, chat_dir):
    global terminal_progress, flood_lock, is_paused, _bytes_downloaded
    while True:
        try:
            if not flood_lock.is_set():
                await flood_lock.wait()

            message = await queue.get()
            m_type = await get_media_type(message)
            type_dir = os.path.join(chat_dir, m_type)
            os.makedirs(type_dir, exist_ok=True)
            
            orig_name = await get_file_name(message)
            clean_name = "".join([c for c in orig_name if c.isalnum() or c in ' ._-()']).rstrip()
            if not clean_name: clean_name = "file.unknown"
            
            # New Unique Filename
            unique_filename = f"{message.id}_{clean_name}"
            unique_filepath = os.path.join(type_dir, unique_filename)
            
            # Old Legacy Filename (for deduplication check)
            legacy_filepath = os.path.join(type_dir, clean_name)
            
            file_size = message.file.size if message.file else 0
            
            # SMART DEDUPLICATION & MIGRATION
            # 1. If unique file exists, skip
            if os.path.exists(unique_filepath) and os.path.getsize(unique_filepath) == file_size:
                if terminal_progress: terminal_progress.update(1)
                queue.task_done()
                continue
                
            # 2. If legacy file exists with correct size, migrate it to the unique name
            if os.path.exists(legacy_filepath) and os.path.getsize(legacy_filepath) == file_size:
                try:
                    os.rename(legacy_filepath, unique_filepath)
                    if terminal_progress: terminal_progress.update(1)
                    queue.task_done()
                    continue
                except: pass # If migration fails, just download

            # 3. Download
            try:
                # Direct download for max speed
                await client.download_media(message, file=unique_filepath)
                _bytes_downloaded += file_size
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
            except FileReferenceExpiredError:
                message = await refresh_message(client, message)
                queue.put_nowait(message)
                queue.task_done()
                continue
            except Exception as e:
                logger.error(f"Error {unique_filename}: {e}")
            
            if terminal_progress: terminal_progress.update(1)
            queue.task_done()
        except asyncio.CancelledError: break
        except Exception: 
            if not queue.empty(): queue.task_done()

async def producer(client, entity, queue):
    """Scans for messages and puts them in the queue immediately."""
    global terminal_progress
    count = 0
    async for msg in client.iter_messages(entity):
        if msg.media:
            queue.put_nowait(msg)
            count += 1
            if terminal_progress:
                terminal_progress.total = count
                terminal_progress.refresh()
    return count

async def main():
    print("\n🚀 ULTRA-INSTANT TELEGRAM DOWNLOADER (FULL SPEED + NO OVERWRITE)")
    client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
    client.flood_sleep_threshold = 24 * 60 * 60
    await client.start(phone=PHONE_NUMBER)
    
    chat_input = input("\nEnter @channelname or link: ").strip()
    try:
        entity = await client.get_entity(chat_input)
    except Exception as e:
        print(f"Error: {e}")
        return

    chat_title = "".join([c for c in getattr(entity, 'title', 'chat') if c.isalnum() or c in ' ._-']).strip()
    chat_dir = os.path.join(DOWNLOAD_DIR, chat_title)
    os.makedirs(chat_dir, exist_ok=True)

    print(f"⚡ Target: {chat_title} | Concurrency: {CONCURRENT_DOWNLOADS}")
    
    queue = asyncio.Queue()
    global terminal_progress
    terminal_progress = tqdm(total=0, desc="🚀 PROGRESS", unit="file", leave=True)
    
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
    
    # Start scanning (concurrently)
    print("⏳ Streaming messages to workers...")
    total_found = await producer(client, entity, queue)
    
    # Wait for completion
    await queue.join()
    
    # Cleanup
    for w in workers: w.cancel()
    speed_task.cancel()
    terminal_progress.close()
    
    print(f"\n✅ Done! Found {total_found} files in total.")
    await client.disconnect()

if __name__ == '__main__':
    if sys.platform == 'win32': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Stopped by user.")
