import os
import sys
import asyncio
import logging
from telethon import TelegramClient
from tqdm.asyncio import tqdm

# ==========================================
# CONFIGURATION
# ==========================================
# Get these from https://my.telegram.org/
API_ID = 23045850        # Replace with your API ID (integer)
API_HASH = '3e50e577960343532691044a23567c89'    # Replace with your API HASH (string)
PHONE_NUMBER = '+919099662234'   # Replace with your Phone Number (e.g., +1234567890)

# Target chat/channel/group
CHAT_NAME = 'ethiodipirstudiesbooksreferences'  

SESSION_NAME = 'media_downloader_session'
DOWNLOAD_DIR = 'downloads'
CONCURRENT_DOWNLOADS = 5      # Number of concurrent download workers
# ==========================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def get_file_name(message):
    """Extracts original filename from message if available."""
    if message.file and hasattr(message.file, 'name') and message.file.name:
        return message.file.name
    
    # Generate a default name based on type and id
    ext = (message.file.ext if hasattr(message.file, 'ext') and message.file.ext else '.unknown') if message.file else '.unknown'
    return f"{message.id}{ext}"

async def get_media_type(message):
    """Determines the media type for organizing into folders."""
    if message.photo:
        return 'photos'
    elif message.video:
        return 'videos'
    elif message.voice:
        return 'voice'
    elif message.audio:
        return 'audio'
    elif message.document:
        return 'documents'
    elif message.gif:
        return 'gifs'
    else:
        return 'other'

async def download_worker(name, queue, client, progress_bar, chat_dir):
    """Worker task to process downloads concurrently from the queue."""
    while True:
        try:
            message = await queue.get()
            
            media_type = await get_media_type(message)
            type_dir = os.path.join(chat_dir, media_type)
            os.makedirs(type_dir, exist_ok=True)
            
            filename = await get_file_name(message)
            # Clean filename to avoid path issues
            filename = "".join([c for c in filename if c.isalpha() or c.isdigit() or c in ' ._-()']).rstrip()
            if not filename:
                filename = f"file_{message.id}.unknown"
                
            filepath = os.path.join(type_dir, filename)
            
            # Skip if already downloaded (Resume/Skip support)
            if os.path.exists(filepath):
                # Checks if it's already there to skip
                progress_bar.update(1)
                queue.task_done()
                continue
                
            # Download the file
            try:
                # Telethon handles the async downloading here
                await client.download_media(message, file=filepath)
            except Exception as e:
                logger.error(f"Error downloading {filename}: {e}")
                
            progress_bar.update(1)
            queue.task_done()
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Worker {name} encountered an error: {e}")
            queue.task_done()

async def main():
    logger.info("Starting Telegram Media Downloader...")
    
    # Validate configuration
    if API_ID == 'YOUR_API_ID' or (isinstance(API_ID, str) and not API_ID.isdigit()):
        logger.error("Configuration Error: Please open the script and replace 'YOUR_API_ID' with your actual numeric API_ID.")
        return
    if API_HASH == 'YOUR_API_HASH':
        logger.error("Configuration Error: Please open the script and replace 'YOUR_API_HASH' with your actual API_HASH string.")
        return
    if not PHONE_NUMBER or PHONE_NUMBER.startswith('YOUR'):
        logger.error("Configuration Error: Please open the script and replace 'YOUR_PHONE' with your actual phone number.")
        return
    if not CHAT_NAME or CHAT_NAME.startswith('YOUR'):
        logger.error("Configuration Error: Please open the script and replace 'YOUR_CHAT_ID_OR_USERNAME' with the target chat name.")
        return

    # Initialize the Telethon Client
    client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
    await client.start(phone=PHONE_NUMBER)
    logger.info("Successfully logged into Telegram.")
    
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    
    logger.info(f"Fetching messages from {CHAT_NAME}...")
    
    try:
        # Get the entity (user, chat, or channel)
        entity = await client.get_entity(CHAT_NAME)
        # Create a folder based on the channel/chat name
        chat_title = getattr(entity, 'title', getattr(entity, 'username', 'unknown_chat'))
        # Clean the title for filesystem compatibility
        chat_title = "".join([c for c in chat_title if c.isalnum() or c in ' ._-']).strip()
        chat_dir = os.path.join(DOWNLOAD_DIR, chat_title)
        os.makedirs(chat_dir, exist_ok=True)
        
    except ValueError:
        logger.error(f"Could not find chat: {CHAT_NAME}. Make sure you are a member or it's public.")
        return
    except Exception as e:
        logger.error(f"Error getting entity: {e}")
        return

    logger.info(f"Downloading to: {chat_dir}")
    logger.info("Gathering media messages. This might take a while for large channels...")
    messages_with_media = []
    
    # Iterate through all messages
    async for message in client.iter_messages(entity):
        if message.media:
            messages_with_media.append(message)
            
    total_media = len(messages_with_media)
    logger.info(f"Found {total_media} media files to process.")
    
    if total_media == 0:
        logger.info("No media found.")
        await client.disconnect()
        return
        
    # Set up queue for concurrent processing
    queue = asyncio.Queue()
    for msg in messages_with_media:
        queue.put_nowait(msg)
        
    # Create progress bar using tqdm
    progress_bar = tqdm(total=total_media, desc="Downloading Media", unit="file")
    
    # Start concurrent workers
    workers = []
    for i in range(CONCURRENT_DOWNLOADS):
        worker = asyncio.create_task(download_worker(f"worker-{i}", queue, client, progress_bar, chat_dir))
        workers.append(worker)
        
    # Wait for the queue to finish processing
    await queue.join()
    
    # Cancel workers once queue is empty
    for w in workers:
        w.cancel()
        
    progress_bar.close()
    logger.info("Download completed successfully!")
    
    await client.disconnect()

if __name__ == '__main__':
    # Fix for Windows asyncio loop
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nProcess interrupted by user.")
