import os
import sys
import asyncio
import logging
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FileReferenceExpiredError
from tqdm.asyncio import tqdm
import uvicorn

app = FastAPI()

# Mount static folder
app.mount("/static", StaticFiles(directory="static"), name="static")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global state
client = None
SESSION_NAME = 'media_downloader_session'
DOWNLOAD_DIR = 'downloads'

class LoginRequest(BaseModel):
    api_id: int
    api_hash: str
    phone: str

class CodeRequest(BaseModel):
    code: str
    password: str = ""

class DownloadRequest(BaseModel):
    chat_name: str
    concurrent_downloads: int = 12
    message_ids: list[int] = [] # Optional: if empty, download all

@app.get("/")
async def index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/api/get_messages")
async def get_messages(chat_name: str, limit: int = 0):
    global client
    if limit == 0: limit = None # Telethon treats None as "Everything"
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Not authorized")
    
    raw_target = chat_name.strip()
    
    # Generate list of formats to try
    targets = [raw_target]
    if not raw_target.startswith('@') and not raw_target.startswith('http') and not raw_target.isdigit():
        targets.insert(0, f"@{raw_target}")
        targets.append(f"https://t.me/{raw_target}")
    
    entity = None
    last_error = None
    
    for t in targets:
        try:
            logger.info(f"Attempting to resolve target: {t}")
            # Try resolving username if it looks like one
            if t.startswith('@'):
                from telethon.tl.functions.contacts import ResolveUsernameRequest
                res = await client(ResolveUsernameRequest(t[1:]))
                if res.chats: entity = res.chats[0]
                elif res.users: entity = res.users[0]
            
            if not entity:
                entity = await client.get_entity(t)
            
            if entity:
                logger.info(f"Successfully resolved to: {getattr(entity, 'title', getattr(entity, 'username', 'Unknown'))}")
                break
        except Exception as e:
            last_error = e
            continue

    if not entity:
        logger.error(f"Failed to resolve {raw_target} after trying {targets}. Last error: {last_error}")
        raise HTTPException(status_code=400, detail=f"Could not find chat/channel: {raw_target}. Please ensure the name is correct or provide a full link (e.g., https://t.me/channelname)")

    try:
        messages = []
        async for msg in client.iter_messages(entity, limit=limit):
            if msg.media:
                media_type = await get_media_type(msg)
                filename = await get_file_name(msg)
                messages.append({
                    "id": msg.id,
                    "type": media_type,
                    "filename": filename,
                    "date": msg.date.isoformat(),
                    "text": msg.text or ""
                })
        return {"messages": messages, "chat_title": getattr(entity, 'title', getattr(entity, 'username', 'Chat'))}
    except Exception as e:
        logger.error(f"Error fetching messages: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/login")
async def login(req: LoginRequest):
    global client
    try:
        if client:
            await client.disconnect()
        
        client = TelegramClient(SESSION_NAME, req.api_id, req.api_hash,
                                connection_retries=None,
                                retry_delay=2,
                                auto_reconnect=True)
        
        # Set high threshold for flood waits
        client.flood_sleep_threshold = 24 * 60 * 60 # 24 hours
        
        await client.connect()
        
        if not await client.is_user_authorized():
            await client.send_code_request(req.phone)
            return {"status": "needs_code"}
        
        return {"status": "authorized"}
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/verify_code")
async def verify_code(req: CodeRequest):
    global client
    try:
        if not client:
            raise HTTPException(status_code=400, detail="Client not initialized")
            
        await client.sign_in(code=req.code, password=req.password)
        return {"status": "authorized"}
    except SessionPasswordNeededError:
        return {"status": "needs_password"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

download_status = {"total": 0, "current": 0, "skipped": 0, "running": False, "message": "Idle", "chat_title": "", "bytes_downloaded": 0, "speed_mbps": 0.0}

# Global coordination
flood_lock = asyncio.Event()
flood_lock.set() # Allow downloads by default

# Shared atomic byte counter for speed calculation
_bytes_lock = asyncio.Lock()
_bytes_downloaded_total = 0

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
    elif message.gif: return 'gifs'
    else: return 'other'

# Global progress bar reference
terminal_progress = None

async def refresh_message(message):
    """Re-fetch a message from Telegram to get fresh file_reference tokens."""
    global client
    try:
        fresh = await client.get_messages(message.chat_id, ids=message.id)
        return fresh if fresh else message
    except Exception as e:
        logger.warning(f"Could not refresh message {message.id}: {e}")
        return message

async def fast_download(client, message, filepath):
    """Highly optimized downloader using maximum 2MB chunks."""
    global _bytes_downloaded_total
    # For very small files, use standard download (no overhead)
    if not message.file or message.file.size < 512 * 1024:
        await client.download_media(message, file=filepath)
        async with _bytes_lock:
            _bytes_downloaded_total += message.file.size if message.file else 0
        return

    # Use low-level download_file with 2048KB (2MB) chunks — Telethon's usable max.
    # Larger chunks = fewer round-trips = dramatically faster on fast connections.
    await client.download_file(
        message.document or message.photo or message.video or message.audio or message.voice,
        file=filepath,
        part_size_kb=2048
    )
    async with _bytes_lock:
        _bytes_downloaded_total += message.file.size if message.file else 0

async def download_worker(queue, chat_dir):
    """High-speed download worker — handles global flood-wait pauses."""
    global client, download_status, terminal_progress, flood_lock
    from telethon.errors import FloodWaitError
    while True:
        try:
            # Wait if a global flood-wait pause is active
            if not flood_lock.is_set():
                await flood_lock.wait()

            message = await queue.get()
            media_type = await get_media_type(message)
            type_dir = os.path.join(chat_dir, media_type)
            os.makedirs(type_dir, exist_ok=True)

            filename = await get_file_name(message)
            filename = "".join([c for c in filename if c.isalnum() or c in ' ._-()']).rstrip()
            if not filename:
                filename = f"file_{message.id}.unknown"

            filepath = os.path.join(type_dir, filename)
            message._export_path = os.path.join(media_type, filename)

            try:
                part_path = filepath + ".part"
                try:
                    await fast_download(client, message, part_path)
                except FileReferenceExpiredError:
                    logger.warning(f"File reference expired for {filename} — re-fetching...")
                    message = await refresh_message(message)
                    await fast_download(client, message, part_path)
                
                if os.path.exists(part_path):
                    os.rename(part_path, filepath)
            except FloodWaitError as e:
                # If we hit a flood wait, pause ALL workers
                if flood_lock.is_set():
                    flood_lock.clear()
                    logger.warning(f"FloodWait: Hitting rate limits. Pausing all workers for {e.seconds}s")
                    
                    # Update status for all to see
                    orig_msg = download_status["message"]
                    download_status["message"] = f"⏳ Rate Limited: Pausing {e.seconds}s..."
                    
                    await asyncio.sleep(e.seconds)
                    
                    download_status["message"] = orig_msg
                    flood_lock.set()
                else:
                    # Another worker already triggered the pause, just wait for it
                    await flood_lock.wait()
                    # Re-queue the message to try again
                    queue.put_nowait(message)
                    queue.task_done()
                    continue
            except Exception as e:
                logger.error(f"Error downloading {filename}: {e}")
                if os.path.exists(filepath + ".part"):
                    try:
                        os.remove(filepath + ".part")
                    except:
                        pass

            download_status["current"] += 1
            if terminal_progress:
                terminal_progress.update(1)
            queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Worker error: {e}")
            queue.task_done()

@app.post("/api/start_download")
async def start_download(req: DownloadRequest):
    global client, download_status
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Not authorized")
        
    if download_status["running"]:
        raise HTTPException(status_code=400, detail="Download already in progress")
        
    raw_target = req.chat_name.strip()
    targets = [raw_target]
    if not raw_target.startswith('@') and not raw_target.startswith('http') and not raw_target.isdigit():
        targets.insert(0, f"@{raw_target}")
        targets.append(f"https://t.me/{raw_target}")
    
    entity = None
    for t in targets:
        try:
            if t.startswith('@'):
                from telethon.tl.functions.contacts import ResolveUsernameRequest
                res = await client(ResolveUsernameRequest(t[1:]))
                if res.chats: entity = res.chats[0]
                elif res.users: entity = res.users[0]
            if not entity:
                entity = await client.get_entity(t)
            if entity: break
        except: continue
        
    if not entity:
        raise HTTPException(status_code=400, detail=f"Could not find chat: {req.chat_name}")
        
    download_status["running"] = True
    download_status["message"] = "Gathering history and media..."
    download_status["current"] = 0
    download_status["total"] = 0
    
    # Create a folder based on the channel/chat name
    chat_title = getattr(entity, 'title', getattr(entity, 'username', 'unknown_chat'))
    chat_title = "".join([c for c in chat_title if c.isalnum() or c in ' ._-']).strip()
    chat_dir = os.path.join(DOWNLOAD_DIR, chat_title)
    os.makedirs(chat_dir, exist_ok=True)
    download_status["chat_title"] = chat_title
    
    asyncio.create_task(run_download_process(entity, req.concurrent_downloads, chat_dir, req.message_ids))
    return {"status": "started"}

async def generate_html_export(chat_title, messages, chat_dir):
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Exported Chat: {chat_title}</title>
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #e7ebf0; margin: 0; padding: 20px; }}
            .page_wrap {{ max-width: 800px; margin: 0 auto; background: white; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); overflow: hidden; }}
            .header {{ background: #517da2; color: white; padding: 20px; }}
            .history {{ padding: 20px; }}
            .message {{ margin-bottom: 15px; display: flex; flex-direction: column; }}
            .message.in {{ align-items: flex-start; }}
            .from_name {{ font-weight: bold; color: #3390ec; margin-bottom: 4px; font-size: 0.9em; }}
            .text {{ background: #f1f1f1; padding: 8px 12px; border-radius: 12px; max-width: 80%; line-height: 1.4; position: relative; }}
            .pull_right {{ float: right; font-size: 0.75em; color: #999; margin-left: 10px; margin-top: 4px; }}
            .media {{ margin-top: 8px; max-width: 300px; border-radius: 8px; overflow: hidden; border: 1px solid #ddd; }}
            .media img {{ width: 100%; display: block; }}
            .media_link {{ display: block; padding: 10px; background: #fafafa; text-decoration: none; color: #3390ec; font-size: 0.9em; }}
            .media_link:hover {{ background: #f0f0f0; }}
            .service {{ text-align: center; color: #8293a1; font-size: 0.85em; margin: 10px 0; }}
        </style>
    </head>
    <body>
        <div class="page_wrap">
            <div class="header">
                <h1>{chat_title}</h1>
                <div class="status">{len(messages)} messages</div>
            </div>
            <div class="history">
    """
    
    for msg in messages:
        if not msg: continue
        
        # Check if it's a service message
        if hasattr(msg, 'action') and msg.action:
            html_content += f'<div class="service">{str(msg.action)}</div>'
            continue
            
        from_name = "User"
        if msg.sender:
            fn = getattr(msg.sender, 'first_name', '') or ''
            ln = getattr(msg.sender, 'last_name', '') or ''
            from_name = f"{fn} {ln}".strip()
            if not from_name:
                from_name = getattr(msg.sender, 'username', '') or 'User'
        
        date_str = msg.date.strftime("%Y-%m-%d %H:%M:%S")
        
        html_content += f'<div class="message in">'
        html_content += f'<div class="from_name">{from_name}</div>'
        html_content += f'<div class="text">'
        
        if msg.text:
            html_content += f'<div>{msg.text}</div>'
            
        if msg.media:
            export_path = getattr(msg, '_export_path', None)
            if export_path:
                media_type = await get_media_type(msg)
                if media_type == 'photos':
                    html_content += f'<div class="media"><a href="{export_path}"><img src="{export_path}"></a></div>'
                else:
                    filename = os.path.basename(export_path)
                    html_content += f'<div class="media"><a class="media_link" href="{export_path}">📎 {filename} ({media_type})</a></div>'
                    
        html_content += f'<div class="pull_right">{date_str}</div>'
        html_content += f'</div></div>'
        
    html_content += """
            </div>
        </div>
    </body>
    </html>
    """
    
    with open(os.path.join(chat_dir, "export_history.html"), "w", encoding="utf-8") as f:
        f.write(html_content)

async def _speed_tracker():
    """Background task: updates speed_mbps in download_status every second."""
    global _bytes_downloaded_total, download_status
    prev_bytes = 0
    while True:
        await asyncio.sleep(1)
        current = _bytes_downloaded_total
        delta = current - prev_bytes
        prev_bytes = current
        download_status["bytes_downloaded"] = current
        download_status["speed_mbps"] = round(delta / 1_048_576, 2)  # bytes → MB/s

async def run_download_process(entity, concurrent_downloads, chat_dir, selected_ids=None):
    global client, download_status, _bytes_downloaded_total
    try:
        all_messages = []
        messages_to_download = []
        selected_set = set(selected_ids) if selected_ids else None

        # Reset byte counter for fresh speed tracking
        _bytes_downloaded_total = 0
        download_status["bytes_downloaded"] = 0
        download_status["speed_mbps"] = 0.0
        download_status["skipped"] = 0

        download_status["message"] = "⚡ Phase 1: Scanning channel history..."

        # Phase 1: Collect all metadata
        async for message in client.iter_messages(entity):
            all_messages.append(message)
            if message.media:
                if not selected_set or message.id in selected_set:
                    messages_to_download.append(message)
            if len(all_messages) % 100 == 0:
                download_status["message"] = f"⚡ Scanning: {len(all_messages)} messages found..."

        total_media = len(messages_to_download)
        download_status["total"] = total_media

        # Phase 2: Generate HTML export
        download_status["message"] = "📄 Phase 2: Generating HTML export..."
        await generate_html_export(download_status["chat_title"], list(reversed(all_messages)), chat_dir)

        if total_media > 0:
            # ── Pre-filter: build skip-set so workers NEVER touch existing complete files ──
            messages_to_queue = []
            skipped = 0
            for m in messages_to_download:
                m_type = await get_media_type(m)
                fname = await get_file_name(m)
                fname = "".join([c for c in fname if c.isalnum() or c in ' ._-()']).rstrip()
                if not fname:
                    fname = f"file_{m.id}.unknown"
                fpath = os.path.join(chat_dir, m_type, fname)
                file_size = m.file.size if m.file else 0
                if os.path.exists(fpath) and os.path.getsize(fpath) == file_size:
                    skipped += 1  # already complete — skip entirely
                else:
                    messages_to_queue.append(m)

            download_status["skipped"] = skipped
            download_status["current"] = skipped  # pre-fill progress bar for skipped files
            new_count = len(messages_to_queue)

            if skipped > 0:
                logger.info(f"Dedup: {skipped}/{total_media} files already complete — skipped.")

            download_status["message"] = f"🚀 Phase 3: Downloading {new_count} new files ({skipped} skipped)..."

            if new_count > 0:
                queue = asyncio.Queue()
                for msg in messages_to_queue:
                    queue.put_nowait(msg)

                global terminal_progress
                terminal_progress = tqdm(
                    total=total_media, initial=skipped,
                    desc="⚡ Flash Download", unit="file", leave=True
                )

                # Start live speed tracker
                speed_task = asyncio.create_task(_speed_tracker())

                # Phase 3: Unleash all workers simultaneously
                workers = [
                    asyncio.create_task(download_worker(queue, chat_dir))
                    for _ in range(concurrent_downloads)
                ]

                await queue.join()

                speed_task.cancel()
                for w in workers:
                    w.cancel()

                terminal_progress.close()
                terminal_progress = None

        download_status["running"] = False
        download_status["message"] = "✅ Extraction completed successfully!"
    except Exception as e:
        logger.error(f"Download process error: {e}")
        download_status["running"] = False
        download_status["message"] = f"❌ Error: {e}"

@app.get("/api/status")
async def get_status():
    return {
        **download_status,
        "speed_mbps": download_status.get("speed_mbps", 0.0),
        "bytes_downloaded": download_status.get("bytes_downloaded", 0),
        "skipped": download_status.get("skipped", 0),
    }

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run(app, host=args.host, port=args.port)
