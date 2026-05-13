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
CONCURRENT_DOWNLOADS = 24 # Slightly lower for better stability
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
    elif message.video:
        if message.video.attributes and any(isinstance(a, type(message.video.attributes[0])) and hasattr(a, 'round_message') and a.round_message for a in message.video.attributes):
            return 'round_video'
        return 'videos'
    elif message.voice: return 'voice'
    elif message.audio: return 'audio'
    elif message.document: return 'files'
    elif hasattr(message, 'gif') and message.gif: return 'gifs'
    else: return 'other'

async def refresh_message(client, message):
    try:
        fresh = await client.get_messages(message.chat_id, ids=message.id)
        return fresh if fresh else message
    except Exception as e:
        logger.warning(f"Could not refresh message {message.id}: {e}")
        return message

async def download_worker(worker_id, queue, client, chat_dir):
    global terminal_progress, flood_lock, is_paused, _bytes_downloaded
    while True:
        try:
            if not flood_lock.is_set():
                await flood_lock.wait()

            message = await queue.get()
            try:
                m_type = await get_media_type(message)
                type_dir = os.path.join(chat_dir, m_type)
                os.makedirs(type_dir, exist_ok=True)
                
                orig_name = await get_file_name(message)
                clean_name = "".join([c for c in orig_name if c.isalnum() or c in ' ._-()']).rstrip()
                if not clean_name: clean_name = "file.unknown"
                
                unique_filename = f"{message.id}_{clean_name}"
                unique_filepath = os.path.join(type_dir, unique_filename)
                legacy_filepath = os.path.join(type_dir, clean_name)
                
                file_size = message.file.size if message.file else 0
                
                # Deduplication & Migration
                if os.path.exists(unique_filepath) and os.path.getsize(unique_filepath) == file_size:
                    if terminal_progress: terminal_progress.update(1)
                    continue
                    
                if os.path.exists(legacy_filepath) and os.path.getsize(legacy_filepath) == file_size:
                    try:
                        os.rename(legacy_filepath, unique_filepath)
                        if terminal_progress: terminal_progress.update(1)
                        continue
                    except: pass

                # Download
                try:
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
                    continue
                except FileReferenceExpiredError:
                    message = await refresh_message(client, message)
                    queue.put_nowait(message)
                    continue
                except Exception as e:
                    logger.error(f"Error {unique_filename}: {e}")
                
                if terminal_progress: terminal_progress.update(1)
            finally:
                queue.task_done()
        except asyncio.CancelledError: break
        except Exception as e:
            logger.error(f"Worker {worker_id} crash: {e}")
            if not queue.empty(): queue.task_done()

async def producer(client, entity, queue):
    """Scans for messages and puts them in the queue."""
    global terminal_progress
    count = 0
    try:
        async for msg in client.iter_messages(entity):
            if msg.media:
                queue.put_nowait(msg)
                count += 1
                if terminal_progress:
                    terminal_progress.total = count
                    terminal_progress.refresh()
    except Exception as e:
        logger.error(f"Producer error: {e}")
    return count

async def main():
    print("\n🚀 ULTRA-INSTANT TELEGRAM DOWNLOADER")
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

    print(f"⚡ Target: {chat_title}")
    
    queue = asyncio.Queue()
    global terminal_progress
    terminal_progress = tqdm(total=0, desc="🚀 DOWNLOAD", unit="file", leave=True)
    
    # Start workers
    workers = [asyncio.create_task(download_worker(i, queue, client, chat_dir)) for i in range(CONCURRENT_DOWNLOADS)]
    
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
    
    # Start producer as a BACKGROUND task to allow immediate downloading
    print("⏳ Streaming messages...")
    producer_task = asyncio.create_task(producer(client, entity, queue))
    
    # Wait for the producer to finish scanning
    total_found = await producer_task
    
    # Wait for the queue to be fully processed
    await queue.join()
    
    # Cleanup
    for w in workers: w.cancel()
    speed_task.cancel()
    terminal_progress.close()
    
    print(f"\n✅ All done! {total_found} files processed.")
    await client.disconnect()

if __name__ == '__main__':
    if sys.platform == 'win32': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Stopped.")
