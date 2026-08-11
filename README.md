# VixSrc Advanced Downloader v2.0
A powerful, multi-threaded command-line tool built in Python to download movies and TV shows securely from VixSrc via TMDb metadata integration. It features concurrent fragment downloads, real-time live progress tracking, parallel subtitle downloading with automatic retry backoff, and seamless FFmpeg multiplexing.
## ✨ Features
 * **Flexible Media Search:** Search for movies or TV shows by **Title Name**, **TMDb ID**, or **IMDb ID**.
 * **Smart TV Show Handling:** Inspect season details, view release statuses (Released vs. Unreleased), and download custom episode ranges (e.g., 1-5, 1,3,5, or all).
 * **Blazing Fast Concurrency:** Utilizes ThreadPoolExecutor and persistent requests.Session connection pooling for lightning-fast parallel fragment and subtitle downloads.
 * **Advanced HLS Token Resolution:** Automatically resolves protected stream tokens, handles iframe fallbacks, and manages Inertia headers seamlessly.
 * **Subtitles Integration:** Automatically extracts, downloads, and merges multi-language .vtt subtitle tracks directly into the final .mp4 container via FFmpeg.
 * **Resilient Architecture:** Includes exponential backoff retry limits, robust network error handling, and safe resume support (continuedl).
 * **Rich Visual Feedback:** Powered by rich and colorama to provide real-time, multi-task granular progress bars for video, audio streams, subtitle segments, and file muxing.
## 🛠️ Prerequisites
Before running the downloader, ensure you have the following installed on your system (or Termux environment):
 1. **Python 3.8+**
 2. **FFmpeg** (Must be accessible in your system's PATH for audio/video merging and subtitle embedding).
### Python Dependencies
Install the required packages using pip:
```bash
pip install yt-dlp requests colorama rich

```
## 🚀 Installation & Setup
 1. Clone or download the script files into your working directory:
   ```bash
   git clone https://github.com/your-username/vixsrc-downloader.git
   cd vixsrc-downloader
   
   ```
 2. Open the script file (vixsrc_downloader.py) and adjust any global configuration settings if needed (such as concurrent workers or debug logging).
## ⚙️ Configuration
You can customize the script behavior directly via the **Global Configuration & User Settings** section at the top of the script:
```python
# Enable or disable debug log folders
ENABLE_LOGGING = True

# Maximum number of episodes to download concurrently 
MAX_CONCURRENT_DOWNLOADS = 1

# Maximum concurrent fragment connections per video stream
MAX_FRAGMENT_CONCURRENT_DOWNLOADS = 5

# Default TMDb API Key (pre-configured)
TMDB_API_KEY = "e192cfe5530437e6eb81a6d7e125e928"

```
## 💻 Usage
Run the script from your terminal:
```bash
python vixsrc_downloader.py

```
### Step-by-Step Prompt Flow:
 1. **Select Media Type:** Choose between a Movie or a TV Show.
 2. **Choose Search Method:** Search by IMDb ID, TMDb ID, or Title Name.
 3. **Select Season / Episodes (TV Shows only):** Input your preferred season and specify target episodes (1-5, all, etc.).
 4. **Select Audio Language:** Choose your preferred language (English, Italian, Hindi, Japanese, etc.).
 5. **Select Quality:** Pick from available resolutions (e.g., 1080p, 720p).
 6. **Download Directory:** Press Enter to use your current working directory or type a custom destination path.
## 📂 Project Structure
```text
vixsrc-downloader/
├── vixsrc_downloader.py    # Main CLI script engine
├── logs/                   # Timestamped debug files & subtitle dumps (if enabled)
└── README.md               # Project documentation

```
## ⚠️ Disclaimer
This tool is designed for educational and personal backup purposes only. Ensure that your use of this software complies with local laws and the terms of service of the respective platforms.
