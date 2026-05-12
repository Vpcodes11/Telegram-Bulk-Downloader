import os
import sys
import asyncio
import logging
from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError, FileReferenceExpiredError
from tqdm import tqdm

# ==========================================
# CONFIGURATION
# ==========================================
API_ID = 23045850
API_HASH = '3e50e577960343532691044a23567c89'
PHONE_NUMBER = '+919099662234'

SESSION_NAME = 'media_downloader_session'
DOWNLOAD_DIR = 'downloads'
CONCURRENT_DOWNLOADS = 12
TIMEOUT_PER_FILE = 600
# ==========================================

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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

async def download_worker(worker_id, queue, client, chat_dir, pbar, done_event):
    global flood_lock, is_paused, _bytes_downloaded
    retries = {}  # Track retry counts per message ID
    MAX_RETRIES = 3
    while True:
        try:
            # If queue is empty and producer is still running, wait a bit
            try:
                message = queue.get_nowait()
            except asyncio.QueueEmpty:
                if done_event.is_set():
                    # Producer finished and queue is empty — we are done
                    return
                # Producer still running, wait for more items
                await asyncio.sleep(0.1)
                continue

            if not flood_lock.is_set():
                await flood_lock.wait()

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
                    pbar.update(1)
                    continue

                if os.path.exists(legacy_filepath) and os.path.getsize(legacy_filepath) == file_size:
                    try:
                        os.rename(legacy_filepath, unique_filepath)
                        pbar.update(1)
                        continue
                    except: pass

                # Download
                try:
                    await asyncio.wait_for(
                        client.download_media(message, file=unique_filepath),
                        timeout=TIMEOUT_PER_FILE
                    )
                    _bytes_downloaded += file_size
                except FloodWaitError as e:
                    if flood_lock.is_set():
                        flood_lock.clear()
                        is_paused = True
                        for remaining in range(e.seconds, 0, -1):
                            pbar.set_postfix_str(f"PAUSED {remaining}s")
                            await asyncio.sleep(1)
                        is_paused = False
                        flood_lock.set()
                    queue.put_nowait(message)
                    continue
                except FileReferenceExpiredError:
                    rid = message.id
                    retries[rid] = retries.get(rid, 0) + 1
                    if retries[rid] <= MAX_RETRIES:
                        message = await refresh_message(client, message)
                        queue.put_nowait(message)
                        continue
                    else:
                        logger.warning(f"Skipping {unique_filename} after {MAX_RETRIES} retries (expired ref)")
                        pbar.update(1)
                except asyncio.TimeoutError:
                    rid = message.id
                    retries[rid] = retries.get(rid, 0) + 1
                    if retries[rid] <= MAX_RETRIES:
                        logger.warning(f"Timeout: {unique_filename} (retry {retries[rid]}/{MAX_RETRIES})")
                        queue.put_nowait(message)
                        continue
                    else:
                        logger.warning(f"Skipping {unique_filename} after {MAX_RETRIES} timeouts")
                        pbar.update(1)
                except Exception as e:
                    logger.error(f"Error {unique_filename}: {e}")

                pbar.update(1)
            except Exception as e:
                logger.error(f"Worker {worker_id} file error: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Worker {worker_id} crash: {e}")

async def main():
    global _bytes_downloaded
    
    print("\n" + "="*50)
    print("  TELEGRAM TURBO DOWNLOADER")
    print("="*50)
    
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

    print(f"\nTarget: {chat_title}")
    
    # ── PHASE 1: SCAN ──
    print("Phase 1: Scanning channel...")
    media_messages = []
    msg_count = 0
    async for msg in client.iter_messages(entity):
        msg_count += 1
        if msg.media:
            media_messages.append(msg)
        if msg_count % 500 == 0:
            print(f"  Scanned {msg_count} messages, found {len(media_messages)} media...")
    
    total_media = len(media_messages)
    print(f"  Scan complete: {msg_count} messages, {total_media} media files.\n")
    
    if total_media == 0:
        print("No media found.")
        await client.disconnect()
        return

    # ── PHASE 2: DOWNLOAD ──
    queue = asyncio.Queue()
    done_event = asyncio.Event()  # Signals when producer is done
    
    for m in media_messages:
        queue.put_nowait(m)
    
    # Producer is already done (we scanned synchronously above)
    done_event.set()
    
    print(f"Phase 2: Downloading {total_media} files with {CONCURRENT_DOWNLOADS} workers...")
    
    pbar = tqdm(total=total_media, desc="DOWNLOADING", unit="file", leave=True)
    
    # Start workers
    workers = [
        asyncio.create_task(download_worker(i, queue, client, chat_dir, pbar, done_event))
        for i in range(CONCURRENT_DOWNLOADS)
    ]

    # Speed monitor
    async def monitor_speed():
        global _bytes_downloaded, is_paused
        prev = 0
        while True:
            await asyncio.sleep(1)
            curr = _bytes_downloaded
            mbps = (curr - prev) / (1024 * 1024)
            prev = curr
            if not is_paused:
                pbar.set_postfix_str(f"{mbps:.1f} MB/s")

    speed_task = asyncio.create_task(monitor_speed())

    # Wait for ALL workers to finish
    await asyncio.gather(*workers)

    speed_task.cancel()
    pbar.close()

    print(f"\nAll done! {total_media} files processed.")
    print(f"Saved to: {os.path.abspath(chat_dir)}")
    await client.disconnect()

if __name__ == '__main__':
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as e:
        print(f"\nError: {e}")
