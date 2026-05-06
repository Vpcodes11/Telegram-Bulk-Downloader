import os
import sys
import asyncio
import logging
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
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
    concurrent_downloads: int = 5

@app.get("/")
async def index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.post("/api/login")
async def login(req: LoginRequest):
    global client
    try:
        if client:
            await client.disconnect()
        
        client = TelegramClient(SESSION_NAME, req.api_id, req.api_hash)
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

# Downloading logic
download_status = {"total": 0, "current": 0, "running": False, "message": "Idle"}

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
    elif message.document: return 'documents'
    elif message.gif: return 'gifs'
    else: return 'other'

async def download_worker(queue):
    global client, download_status
    while True:
        try:
            message = await queue.get()
            media_type = await get_media_type(message)
            type_dir = os.path.join(DOWNLOAD_DIR, media_type)
            os.makedirs(type_dir, exist_ok=True)
            
            filename = await get_file_name(message)
            filename = "".join([c for c in filename if c.isalpha() or c.isdigit() or c in ' ._-()']).rstrip()
            if not filename: filename = f"file_{message.id}.unknown"
                
            filepath = os.path.join(type_dir, filename)
            
            if not os.path.exists(filepath):
                try:
                    await client.download_media(message, file=filepath)
                except Exception as e:
                    logger.error(f"Error downloading {filename}: {e}")
                    
            download_status["current"] += 1
            queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Worker encountered error: {e}")
            queue.task_done()

@app.post("/api/start_download")
async def start_download(req: DownloadRequest):
    global client, download_status
    if not client or not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Not authorized")
        
    if download_status["running"]:
        raise HTTPException(status_code=400, detail="Download already in progress")
        
    try:
        entity = await client.get_entity(req.chat_name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not find chat: {e}")
        
    download_status["running"] = True
    download_status["message"] = "Gathering media messages..."
    download_status["current"] = 0
    download_status["total"] = 0
    
    asyncio.create_task(run_download_process(entity, req.concurrent_downloads))
    return {"status": "started"}

async def run_download_process(entity, concurrent_downloads):
    global client, download_status
    try:
        messages_with_media = []
        async for message in client.iter_messages(entity):
            if message.media:
                messages_with_media.append(message)
                
        total_media = len(messages_with_media)
        download_status["total"] = total_media
        download_status["message"] = f"Downloading {total_media} files..."
        
        if total_media == 0:
            download_status["running"] = False
            download_status["message"] = "No media found."
            return
            
        queue = asyncio.Queue()
        for msg in messages_with_media:
            queue.put_nowait(msg)
            
        workers = []
        for i in range(concurrent_downloads):
            worker = asyncio.create_task(download_worker(queue))
            workers.append(worker)
            
        await queue.join()
        
        for w in workers:
            w.cancel()
            
        download_status["running"] = False
        download_status["message"] = "Download completed successfully!"
    except Exception as e:
        download_status["running"] = False
        download_status["message"] = f"Error: {e}"

@app.get("/api/status")
async def get_status():
    return download_status

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run(app, host="127.0.0.1", port=8000)
