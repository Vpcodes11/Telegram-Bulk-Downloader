import os
import sys
import asyncio
import logging
import random
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
CONCURRENT_DOWNLOADS = 16
MAX_RETRIES = 2
# ==========================================

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

flood_lock = asyncio.Event()
flood_lock.set()
_bytes_downloaded = 0
is_paused = False

def get_timeout(file_size):
    """Dynamic timeout: 300s base (5 min) + 60s per 10MB. Minimum 300s, Maximum 3600s (1 hour)."""
    if not file_size: return 300
    extra = (file_size / (10 * 1024 * 1024)) * 60
    return min(max(300, 300 + extra), 3600)

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
        logger.warning(f"Could not refresh msg {message.id}: {e}")
        return message

def interleave_by_size(messages):
    """Interleave large and small files so workers stay busy throughout."""
    sized = [((m.file.size or 0) if getattr(m, 'file', None) else 0, m) for m in messages]
    sized.sort(key=lambda x: x[0])
    half = len(sized) // 2
    small = [m for _, m in sized[:half]]
    large = [m for _, m in sized[half:]]
    result = []
    for i in range(max(len(small), len(large))):
        if i < len(small): result.append(small[i])
        if i < len(large): result.append(large[i])
    return result

async def download_worker(worker_id, queue, client, chat_dir, pbar, done_event):
    global flood_lock, is_paused, _bytes_downloaded
    retries = {}
    while True:
        try:
            try:
                message = queue.get_nowait()
            except asyncio.QueueEmpty:
                if done_event.is_set():
                    return
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

                file_size = (message.file.size or 0) if getattr(message, 'file', None) else 0
                timeout = get_timeout(file_size)

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

                # Download using download_file with 512KB chunks for stability, fallback to download_media
                last_received = 0
                def make_progress_cb():
                    nonlocal last_received
                    def cb(received, total):
                        nonlocal last_received
                        global _bytes_downloaded
                        delta = received - last_received
                        if delta > 0:
                            _bytes_downloaded += delta
                            last_received = delta + last_received
                    return cb

                try:
                    media_input = getattr(message, 'document', None) or getattr(message, 'photo', None) or getattr(message, 'video', None) or getattr(message, 'audio', None) or getattr(message, 'voice', None)
                    if media_input:
                        await asyncio.wait_for(
                            client.download_file(
                                media_input,
                                file=unique_filepath,
                                part_size_kb=512,
                                progress_callback=make_progress_cb()
                            ),
                            timeout=timeout
                        )
                    else:
                        await asyncio.wait_for(
                            client.download_media(
                                message, 
                                file=unique_filepath,
                                progress_callback=make_progress_cb()
                            ),
                            timeout=timeout
                        )
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
                        logger.warning(f"Skipped (expired): {unique_filename}")
                        pbar.update(1)
                except asyncio.TimeoutError:
                    rid = message.id
                    retries[rid] = retries.get(rid, 0) + 1
                    if retries[rid] <= MAX_RETRIES:
                        logger.warning(f"Timeout ({int(timeout)}s): {unique_filename} retry {retries[rid]}/{MAX_RETRIES}")
                        # Clean up partial file
                        if os.path.exists(unique_filepath):
                            try: os.remove(unique_filepath)
                            except: pass
                        queue.put_nowait(message)
                        continue
                    else:
                        logger.warning(f"Skipped (timeout): {unique_filename}")
                        if os.path.exists(unique_filepath):
                            try: os.remove(unique_filepath)
                            except: pass
                        pbar.update(1)
                except Exception as e:
                    logger.error(f"Error {unique_filename}: {e}")

                pbar.update(1)
            except Exception as e:
                logger.error(f"Worker {worker_id} error: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Worker {worker_id} crash: {e}")

async def main():
    global _bytes_downloaded

    print("\n" + "="*50)
    print("  TELEGRAM TURBO DOWNLOADER v4")
    print("  Consistent Speed + Smart Timeout")
    print("="*50)

    client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
    client.flood_sleep_threshold = 24 * 60 * 60
    await client.start(phone=PHONE_NUMBER)

    chat_input = input("\nEnter @channelname or link (comma-separated for multiple): ").strip()
    
    chats = [c.strip() for c in chat_input.split(',') if c.strip()]
    if not chats:
        await client.disconnect()
        return

    for chat in chats:
        try:
            entity = await client.get_entity(chat)
        except Exception as e:
            print(f"\nError accessing {chat}: {e}")
            continue

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
            continue

        # ── PHASE 2: INTERLEAVE ──
        media_messages = interleave_by_size(media_messages)

        queue = asyncio.Queue()
        done_event = asyncio.Event()

        for m in media_messages:
            queue.put_nowait(m)

        done_event.set()

        print(f"Phase 2: Downloading {total_media} files with {CONCURRENT_DOWNLOADS} workers...\n")

        pbar = tqdm(total=total_media, desc=f"DL {chat_title}", unit="file", leave=True)

        workers = [
            asyncio.create_task(download_worker(i, queue, client, chat_dir, pbar, done_event))
            for i in range(CONCURRENT_DOWNLOADS)
        ]

        # Speed monitor
        async def monitor_speed(pbar_ref):
            global _bytes_downloaded, is_paused
            prev = 0
            while True:
                await asyncio.sleep(1)
                curr = _bytes_downloaded
                mbps = (curr - prev) / (1024 * 1024)
                prev = curr
                if not is_paused:
                    pbar_ref.set_postfix_str(f"{mbps:.1f} MB/s")

        speed_task = asyncio.create_task(monitor_speed(pbar))

        await asyncio.gather(*workers)

        speed_task.cancel()
        pbar.close()

        # Count actual files
        actual_files = sum(len(files) for _, _, files in os.walk(chat_dir))
        print(f"\nAll done for {chat_title}! {actual_files} files in {os.path.abspath(chat_dir)}\n")
        print("-" * 50)

    print("All requested channels have been processed.")
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
