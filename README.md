# Telegram Media Downloader

A high-performance, asynchronous media downloader for Telegram channels and groups. 

## Features
- **Concurrent Async Downloads**: Fetches multiple files simultaneously for maximum speed.
- **Smart Resume**: Automatically skips already-downloaded files to save time and bandwidth.
- **Auto-Organization**: Sorts media into categorized folders (`photos/`, `videos/`, `documents/`, etc.).
- **Progress Tracking**: Real-time CLI progress bar using `tqdm`.
- **Original Filenames**: Preserves original filenames provided by uploaders.

## Installation

1. Clone the repository:
```bash
git clone https://github.com/yourusername/telegram-media-downloader.git
cd telegram-media-downloader
```

2. Install the dependencies:
```bash
pip install -r requirements.txt
```

## Setup & Usage

1. Open `telegram_downloader.py` and configure the settings at the top of the script:
   - `API_ID`: Your Telegram API ID.
   - `API_HASH`: Your Telegram API Hash.
   - `PHONE_NUMBER`: Your phone number associated with the account.
   - `CHAT_NAME`: The target channel/group username or invite link.

   *(You can obtain your API ID and Hash from [my.telegram.org](https://my.telegram.org/))*

2. Run the script:
```bash
python telegram_downloader.py
```

3. During the first run, Telegram will send a login code to your app. Enter it when prompted in the terminal. The script will save your session in `media_downloader_session.session`.

## Requirements
- Python 3.7+
- `telethon`
- `tqdm`
