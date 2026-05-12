import os
import sys
import asyncio
import logging
from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError, FileReferenceExpiredError
from telethon.tl.types import InputMessagesFilterDocument
from telethon.sessions import StringSession
from tqdm.asyncio import tqdm
import math

# ==========================================
# CONFIGURATION
# ==========================================
API_ID = 23045850
API_HASH = '3e50e577960343532691044a23567c89'
PHONE_NUMBER = '+919099662234'

SESSION_NAME = 'media_downloader_session'
DOWNLOAD_DIR = 'downloads'
CONCURRENT_DOWNLOADS = 24
TIMEOUT_PER_FILE = 600
PART_SIZE_KB = 512 # 512KB chunks
MIN_SIZE_FOR_PARALLEL = 10 * 1024 * 1024 # 10MB
PARALLEL_CHUNKS = 4 # Number of parallel requests for a single large file
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
    
    file_size = message.file.size
    last_received = 0
    
    def progress_callback(received, total):
        nonlocal last_received
        global _bytes_downloaded
        delta = received - last_received
        if delta > 0:
            _bytes_downloaded += delta
            last_received = received

    try:
        # For large files, we use a custom parallel downloader if possible.
        # But Telethon's download_file is already quite optimized when cryptg is present.
        # We will use download_file with 1MB chunks for maximum throughput.
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
            
            filename = await get_file_name(message)
            filename = "".join([c for c in filename if c.isalnum() or c in ' ._-()']).rstrip()
            if not filename: filename = f"file_{message.id}.unknown"
            
            filepath = os.path.join(type_dir, filename)
            
            try:
                part_path = filepath + ".part"
                try:
                    await fast_download(client, message, part_path)
                except FileReferenceExpiredError:
                    message = await refresh_message(client, message)
                    await fast_download(client, message, part_path)
                
                if os.path.exists(part_path):
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
                else:
                    await flood_lock.wait()
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

async def generate_html_export(chat_title, messages, chat_dir):
    # This is now done at the end to prevent delaying the download
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Export: {chat_title}</title>
        <style>
            body {{ font-family: sans-serif; background: #f0f2f5; padding: 20px; }}
            .message {{ background: white; padding: 15px; margin-bottom: 10px; border-radius: 8px; }}
            .media_link {{ color: #0088cc; text-decoration: none; font-weight: bold; }}
        </style>
    </head>
    <body>
        <h1>{chat_title}</h1>
        <div>{len(messages)} messages</div>
        <hr>
    """
    for msg in messages:
        if not msg or (hasattr(msg, 'action') and msg.action): continue
        date_str = msg.date.strftime("%Y-%m-%d %H:%M:%S")
        html_content += f'<div class="message"><div><b>User</b> <small>{date_str}</small></div>'
        if msg.text: html_content += f'<div>{msg.text}</div>'
        if msg.media:
            m_type = await get_media_type(msg)
            fname = await get_file_name(msg)
            fname = "".join([c for c in fname if c.isalnum() or c in ' ._-()']).rstrip()
            if not fname: fname = f"file_{msg.id}.unknown"
            html_content += f'<div><a class="media_link" href="{m_type}/{fname}">[Media: {m_type}] {fname}</a></div>'
        html_content += f'</div>'
    html_content += "</body></html>"
    with open(os.path.join(chat_dir, "export_history.html"), "w", encoding="utf-8") as f:
        f.write(html_content)

async def main():
    print("\n🚀 FLASH CMD TELEGRAM DOWNLOADER (INSTANT START)")
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
    all_messages = []
    media_messages = []
    
    scan_bar = tqdm(desc="Phase 1: Scanning (Books Only)", unit="msg")
    # Using server-side filter for Documents (Books) to make scanning instant
    async for msg in client.iter_messages(entity, filter=InputMessagesFilterDocument):
        all_messages.append(msg)
        if msg.media: media_messages.append(msg)
        scan_bar.update(1)
    
    # If no books found with filter, fall back to full scan
    if not media_messages:
        scan_bar.set_description("Phase 1: Scanning (Full)")
        async for msg in client.iter_messages(entity):
            if msg.id in [m.id for m in all_messages]: continue
            all_messages.append(msg)
            if msg.media: media_messages.append(msg)
            scan_bar.update(1)
            
    scan_bar.close()
    
    if media_messages:
        messages_to_download = []
        skipped = 0
        for m in media_messages:
            m_type = await get_media_type(m)
            fname = await get_file_name(m)
            fname = "".join([c for c in fname if c.isalnum() or c in ' ._-()']).rstrip()
            if not fname: fname = f"file_{m.id}.unknown"
            fpath = os.path.join(chat_dir, m_type, fname)
            fsize = m.file.size if m.file else 0
            # If file doesn't exist or size is different, we download it
            if os.path.exists(fpath) and os.path.getsize(fpath) == fsize:
                skipped += 1
            else:
                messages_to_download.append(m)
        
        total = len(media_messages)
        new_count = len(messages_to_download)
        
        if skipped > 0:
            print(f"✅ Deduplication: {skipped}/{total} files already complete.")
        
        if new_count > 0:
            print(f"🚀 Phase 2: Downloading {new_count} files...")
            queue = asyncio.Queue()
            for m in messages_to_download: queue.put_nowait(m)
            
            global terminal_progress
            terminal_progress = tqdm(total=total, initial=skipped, desc="FLASH", unit="file", leave=True)
            
            from telethon.sessions import StringSession
            session_str = StringSession.save(client.session)

            worker_clients = []
            for i in range(CONCURRENT_DOWNLOADS):
                worker_client = TelegramClient(StringSession(session_str), int(API_ID), API_HASH)
                await worker_client.connect()
                worker_clients.append(worker_client)

            workers = [asyncio.create_task(download_worker(queue, worker_clients[i], chat_dir)) for i in range(CONCURRENT_DOWNLOADS)]
            
            async def monitor_speed():
                global _bytes_downloaded, is_paused
                prev = 0
                while not queue.empty() or any(not w.done() for w in workers):
                    await asyncio.sleep(1)
                    curr = _bytes_downloaded
                    mbps = (curr - prev) / (1024 * 1024)
                    prev = curr
                    if not is_paused:
                        terminal_progress.set_postfix(speed=f"🚀 {mbps:.2f} MB/s")

            speed_task = asyncio.create_task(monitor_speed())
            await queue.join()
            for w in workers: w.cancel()
            speed_task.cancel()
            for wc in worker_clients: await wc.disconnect()
            terminal_progress.close()
            print("✅ Downloads complete!")
        else:
            print("✅ Everything is already up to date!")
            
        print("📄 Phase 3: Exporting HTML history...")
        await generate_html_export(chat_title, list(reversed(all_messages)), chat_dir)
    
    print(f"\nAll done! Path: {chat_dir}\n")
    await client.disconnect()

if __name__ == '__main__':
    if sys.platform == 'win32': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
