# ==============================================================================
# GLOBAL CONFIGURATION & USER SETTINGS
# Modify these variables to adjust downloader behavior without touching code
# ==============================================================================

# Enable or disable debug logging (True = save log files to 'logs/', False = disable logging completely)
ENABLE_LOGGING = True

# Maximum number of episodes to download concurrently (remaining episodes will queue automatically)
MAX_CONCURRENT_DOWNLOADS = 10

# Maximum concurrent fragment connections per video stream
MAX_FRAGMENT_CONCURRENT_DOWNLOADS = 15

# Retry limits for network requests and video fragments
NETWORK_RETRIES = 20
FRAGMENT_RETRIES = 50
HTTP_TIMEOUT = 15  # Standardized timeout across all requests

# TMDb API Key for fetching movie and TV show metadata
TMDB_API_KEY = "e192cfe5530437e6eb81a6d7e125e928"

# User-Agent string used for outbound web requests
TRUSTED_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Default download folder (Leave empty "" to prompt user or use current working directory)
DEFAULT_DOWNLOAD_DIR = ""

# ==============================================================================
# IMPORTS & DEPENDENCIES
# ==============================================================================

import os
import re
import sys
import json
import time
import shutil
import requests
import subprocess
from datetime import datetime
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from yt_dlp import YoutubeDL
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError, update_url_query, sanitize_filename

# Visual & Formatting Libraries
from colorama import init, Fore, Style
from rich.console import Console
from rich.progress import (
    Progress,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeRemainingColumn,
    SpinnerColumn
)

# Initialize Colorama and Rich Console
init(autoreset=True)
console = Console()

# ==========================================
# GLOBAL OPTIMIZATIONS (Session & Regex)
# ==========================================

# Use a persistent HTTP session for connection pooling (Fewer TLS handshakes = Faster speeds)
http_session = requests.Session()
http_session.headers.update({'User-Agent': TRUSTED_USER_AGENT})

# Pre-compiled Regexes for performance inside heavy loops
RE_HLS_ATTR = re.compile(r'([A-Z0-9\-]+)=(?:"([^"]+)"|([^,\s]+))')
RE_DURATION = re.compile(r'Duration:\s*(\d+):(\d+):(\d+\.\d+)')
RE_TIME = re.compile(r'time=(\d+):(\d+):(\d+\.\d+)')
RE_PERCENT_STR = re.compile(r'\x1b\[[0-9;]*m')

# ==========================================
# DEBUG LOGGING HELPER FUNCTIONS
# ==========================================

def create_debug_dir(v_id):
    """Creates a timestamped log folder inside 'logs/' if ENABLE_LOGGING is True."""
    if not ENABLE_LOGGING:
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dir_name = os.path.join("logs", f"debug_{v_id}_{timestamp}")
    os.makedirs(dir_name, exist_ok=True)
    return dir_name

def write_debug_file(debug_dir, filename, content):
    """Writes or overwrites a text file inside the active debug directory."""
    if not ENABLE_LOGGING or not debug_dir or not content:
        return
    try:
        path = os.path.join(debug_dir, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        console.print(f"[bold red][!] Failed writing log file '{filename}': {e}[/bold red]")

def append_log_txt(debug_dir, text):
    """Appends lines to log.txt inside the debug directory."""
    if not ENABLE_LOGGING or not debug_dir:
        return
    try:
        path = os.path.join(debug_dir, "log.txt")
        with open(path, 'a', encoding='utf-8') as f:
            f.write(text + "\n")
    except Exception:
        pass

# ==========================================
# SUBTITLE PROCESSING HELPER FUNCTIONS
# ==========================================

def parse_m3u8_subtitles(master_m3u8_text, base_url):
    """Parses #EXT-X-MEDIA:TYPE=SUBTITLES entries from the master playlist using compiled regex."""
    sub_tracks = []
    for line in master_m3u8_text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA:") and "TYPE=SUBTITLES" in line:
            attrs = {}
            for m in RE_HLS_ATTR.finditer(line):
                attrs[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)

            name = attrs.get("NAME", "Unknown")
            lang = attrs.get("LANGUAGE", "und")
            default_flag = (attrs.get("DEFAULT", "NO")).upper() == "YES"
            forced_flag = (attrs.get("FORCED", "NO")).upper() == "YES"
            autoselect_flag = (attrs.get("AUTOSELECT", "NO")).upper() == "YES"
            uri = attrs.get("URI")
            characteristics = attrs.get("CHARACTERISTICS", "")
            is_sdh = "accessibility" in characteristics.lower() or "sdh" in name.lower() or "cc" in name.lower()

            if uri:
                full_uri = urljoin(base_url, uri)
                sub_tracks.append({
                    'name': name,
                    'lang': lang,
                    'default': default_flag,
                    'forced': forced_flag,
                    'autoselect': autoselect_flag,
                    'sdh': is_sdh,
                    'uri': full_uri,
                    'raw_line': line
                })
    return sub_tracks

def merge_vtt_segments(segment_texts):
    """Merges WebVTT subtitle segments into a single file."""
    if not segment_texts:
        return "WEBVTT\n\n"
    merged_lines = []
    for idx, text in enumerate(segment_texts):
        lines = text.strip().splitlines()
        if idx == 0:
            merged_lines.extend(lines)
        else:
            in_header = True
            for line in lines:
                l_str = line.strip()
                if in_header:
                    if l_str.startswith("WEBVTT") or l_str.startswith("X-TIMESTAMP-MAP") or l_str.startswith("KIND") or l_str.startswith("LANGUAGE"):
                        continue
                    if l_str == "":
                        continue
                    in_header = False
                merged_lines.append(line)
        merged_lines.append("")
    return "\n".join(merged_lines) + "\n"

def process_and_download_subtitles(sub_tracks, debug_dir, req_headers, ui_ctx=None):
    """Downloads subtitle track segments concurrently with retry backoff."""
    if not sub_tracks:
        append_log_txt(debug_dir, "[Subtitle Processing] No subtitle tracks detected.")
        return []

    sub_dir = os.path.join(debug_dir, "subtitles") if debug_dir else os.path.join("logs", "subtitles")
    if ENABLE_LOGGING:
        os.makedirs(sub_dir, exist_ok=True)

    downloaded_tracks = []
    used_filenames = set()
    progress = ui_ctx['progress'] if ui_ctx and 'progress' in ui_ctx else None

    for idx, track in enumerate(sub_tracks, 1):
        name = track['name']
        lang = track['lang']
        uri = track['uri']

        clean_name = re.sub(r'[^\w\s\-]', '_', name).strip() or f"track_{idx}"
        filename_base = clean_name
        counter = 1
        while filename_base.lower() in used_filenames:
            filename_base = f"{clean_name}_{counter}"
            counter += 1
        used_filenames.add(filename_base.lower())

        m3u8_filename = f"{filename_base}.m3u8"
        vtt_filename = f"{filename_base}.vtt"
        vtt_file_path = os.path.join(sub_dir, vtt_filename)

        status = "Failed"
        seg_count = 0

        try:
            r = http_session.get(uri, headers=req_headers, timeout=HTTP_TIMEOUT)
            if r.status_code == 200:
                sub_m3u8_text = r.text
                write_debug_file(sub_dir, m3u8_filename, sub_m3u8_text)
                seg_urls = [urljoin(uri, line.strip()) for line in sub_m3u8_text.splitlines() if line.strip() and not line.strip().startswith("#")]
                seg_count = len(seg_urls)
                
                sub_task = None
                if progress and seg_count > 0:
                    sub_task = progress.add_task(f"[blue]Sub ({lang}): {name}", total=seg_count)

                segment_texts = []
                
                def fetch_seg(seg_url):
                    # Robust retry logic with exponential backoff for tiny fragments
                    for attempt in range(4): # Retries: 0s, 2s, 4s, 8s
                        try:
                            seg_r = http_session.get(seg_url, headers=req_headers, timeout=HTTP_TIMEOUT)
                            if seg_r.status_code == 200:
                                return seg_r.text
                        except Exception:
                            time.sleep(2 ** attempt)
                    return None

                with ThreadPoolExecutor(max_workers=MAX_FRAGMENT_CONCURRENT_DOWNLOADS) as executor:
                    for text in executor.map(fetch_seg, seg_urls):
                        if text:
                            segment_texts.append(text)
                        if progress and sub_task is not None:
                            progress.update(sub_task, advance=1)
                
                if progress and sub_task is not None:
                    progress.update(sub_task, completed=seg_count, description=f"[bold green]Sub ({lang}) Done: {name}")

                if segment_texts:
                    merged_vtt = merge_vtt_segments(segment_texts)
                    write_debug_file(sub_dir, vtt_filename, merged_vtt)
                    status = "Success"
                    track['vtt_file'] = vtt_file_path
                    track['filename'] = vtt_filename
                    downloaded_tracks.append(track)
        except Exception as e:
            append_log_txt(debug_dir, f"[!] Error downloading subtitle track '{name}': {e}")

        log_entry = (
            f"[Subtitle Track #{idx}]\n"
            f"- Language: {lang}\n"
            f"- Name: {name}\n"
            f"- Download Status: {status}\n"
            f"- Segments Count: {seg_count}\n"
            f"- Final Merged Filename: {vtt_filename}\n"
        )
        append_log_txt(debug_dir, log_entry)

    return downloaded_tracks

def embed_subtitles_ffmpeg(downloaded_tracks, raw_video_path, final_output_path, ui_ctx=None):
    """Embeds downloaded subtitle tracks into the MP4 container via FFmpeg."""
    if not downloaded_tracks:
        return False

    ffmpeg_bin = shutil.which('ffmpeg')
    if not ffmpeg_bin:
        if ENABLE_LOGGING:
            os.makedirs("logs", exist_ok=True)
            with open(os.path.join("logs", "subtitle_embed.log"), "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now()}] FFmpeg binary not located in system PATH.\n")
        return False

    temp_muxed = final_output_path + ".muxing.mp4"
    cmd = [ffmpeg_bin, '-y', '-i', raw_video_path]

    for track in downloaded_tracks:
        cmd.extend(['-i', track['vtt_file']])

    cmd.extend(['-map', '0:v', '-map', '0:a?'])
    for i in range(len(downloaded_tracks)):
        cmd.extend(['-map', f'{i+1}:0'])

    cmd.extend(['-c:v', 'copy', '-c:a', 'copy', '-c:s', 'mov_text'])

    for idx, track in enumerate(downloaded_tracks):
        lang = track.get('lang', 'und')
        title = track.get('name', f'Track {idx+1}')
        default_flag = track.get('default', False)
        forced_flag = track.get('forced', False)

        cmd.extend([f'-metadata:s:s:{idx}', f'language={lang}'])
        cmd.extend([f'-metadata:s:s:{idx}', f'title={title}'])
        cmd.extend([f'-metadata:s:s:{idx}', f'handler_name={title}'])

        disp = []
        if default_flag: disp.append('default')
        if forced_flag: disp.append('forced')
        disp_str = '+'.join(disp) if disp else '0'
        cmd.extend([f'-disposition:s:{idx}', disp_str])

    cmd.append(temp_muxed)

    progress = ui_ctx['progress'] if ui_ctx and 'progress' in ui_ctx else None
    task_id = None
    if progress:
        filename_base = ui_ctx.get('filename_base', 'Video')
        task_id = progress.add_task(f"[yellow]Muxing Final File: {filename_base}", total=100)

    ffmpeg_output = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
            universal_newlines=True, encoding='utf-8', errors='replace'
        )
        
        duration_secs = 0
        for line in proc.stdout:
            ffmpeg_output.append(line)  # Safely buffer output for error logging
            
            if not duration_secs:
                dur_match = RE_DURATION.search(line)
                if dur_match:
                    h, m, s = float(dur_match.group(1)), float(dur_match.group(2)), float(dur_match.group(3))
                    duration_secs = h * 3600 + m * 60 + s
            
            time_match = RE_TIME.search(line)
            if time_match and duration_secs > 0:
                h, m, s = float(time_match.group(1)), float(time_match.group(2)), float(time_match.group(3))
                current_secs = h * 3600 + m * 60 + s
                pct = min((current_secs / duration_secs) * 100, 100)
                if progress and task_id is not None:
                    progress.update(task_id, completed=pct)
                    
        proc.wait()

        if proc.returncode == 0 and os.path.exists(temp_muxed) and os.path.getsize(temp_muxed) > 0:
            if os.path.exists(final_output_path):
                os.remove(final_output_path)
            os.rename(temp_muxed, final_output_path)
            if progress and task_id is not None:
                progress.update(task_id, completed=100, description=f"[bold green]Muxing Done: {ui_ctx.get('filename_base', '')}")
            return True
        else:
            if progress and task_id is not None:
                progress.update(task_id, description=f"[bold red]Muxing Failed: {ui_ctx.get('filename_base', '')}")
            if ENABLE_LOGGING:
                err_log_content = (
                    f"=== FFmpeg Subtitle Embedding Error ===\n"
                    f"Date: {datetime.now()}\n"
                    f"Command: {' '.join(cmd)}\n"
                    f"Return Code: {proc.returncode}\n"
                    f"OUTPUT:\n{''.join(ffmpeg_output)}\n"
                    f"=======================================\n"
                )
                os.makedirs("logs", exist_ok=True)
                with open(os.path.join("logs", "subtitle_embed.log"), "a", encoding="utf-8") as f:
                    f.write(err_log_content)

            if os.path.exists(temp_muxed):
                try: os.remove(temp_muxed)
                except Exception: pass
            return False
    except Exception as e:
        if ENABLE_LOGGING:
            os.makedirs("logs", exist_ok=True)
            with open(os.path.join("logs", "subtitle_embed.log"), "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now()}] Exception during FFmpeg execution: {e}\n")
        if os.path.exists(temp_muxed):
            try: os.remove(temp_muxed)
            except Exception: pass
        return False

# ==========================================
# CUSTOM YT-DLP EXTRACTOR CLASS
# ==========================================

class VixSrcIE(InfoExtractor):
    _VALID_URL = r'https?://(?:www\.)?vixsrc\.to/(?P<t>movie|tv)/(?P<i>[\w/-]+)(?:[?#].*)?'

    def __init__(self, ui_ctx=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ui_ctx = ui_ctx

    def _log_status(self, msg):
        """Prints live extraction status securely above the progress bars."""
        if hasattr(self, 'ui_ctx') and self.ui_ctx and 'progress' in self.ui_ctx:
            self.ui_ctx['progress'].console.print(f"[dim cyan][Extractor][/dim cyan] {msg}")

    def _real_extract(self, url):
        d, b = self._downloader, 'https://vixsrc.to'
        dw, sr = self._download_webpage, self._search_regex
        
        t, i = self._match_valid_url(url).group('t', 'i')
        v_id, p = i.replace('/', '_'), i.split('/') + ['1', '1']

        debug_dir = create_debug_dir(v_id)
        append_log_txt(debug_dir, f"Original URL: {url}\n")
        
        self._log_status(f"Starting extraction for ID: {v_id}")

        if d:
            d.params.update({
                'concurrent_fragment_downloads': MAX_FRAGMENT_CONCURRENT_DOWNLOADS, 
                'retries': NETWORK_RETRIES, 
                'fragment_retries': FRAGMENT_RETRIES,
                'http_headers': {'User-Agent': TRUSTED_USER_AGENT, 'Referer': url, 'Origin': b}
            })
            d.params.setdefault('retry_sleep', {})['fragment'] = 1.0
        
        tu = f'https://www.themoviedb.org/{t}/{p[0]}{f"/season/{p[1]}/episode/{p[2]}" if t == "tv" else ""}?language=en'
        tp = dw(tu, v_id, fatal=False, headers={'User-Agent': TRUSTED_USER_AGENT}) or ''
        title = re.sub(r'\s*(?:[-—]|&mdash;)\s*The Movie Database.*', '', self._html_search_regex(r'<title>(.+?)</title>', tp, 'title', default=v_id)).strip()
        title = sanitize_filename(title)

        h = {'Referer': f'{b}/', 'User-Agent': TRUSTED_USER_AGENT}
        
        self._log_status("Fetching API configuration...")
        api_url = url.replace('/tv/', '/api/tv/').replace('/movie/', '/api/movie/')
        api_resp = dw(api_url, v_id, headers=h, fatal=False, note='Fetching API JSON')
        
        target_fetch_url = url
        if api_resp:
            try:
                api_json = json.loads(api_resp)
                if 'src' in api_json:
                    target_fetch_url = urljoin(b, api_json['src'])
            except json.JSONDecodeError:
                pass

        self._log_status("Resolving stream token...")
        wp = dw(target_fetch_url, v_id, headers={'Referer': url, 'User-Agent': TRUSTED_USER_AGENT})
        tk = sr(r"['\"]token['\"]\s*:\s*['\"](\w+)['\"]", wp, 'token', default=None)
        
        if not tk:
            self._log_status("Primary token failed. Downloading iframe fallback...")
            wp_fallback = dw(url, v_id, headers=h, note='Downloading fallback webpage')
            for _ in range(3):
                tk = sr(r"['\"]token['\"]\s*:\s*['\"](\w+)['\"]", wp_fallback, 'token', default=None)
                if tk: break
                ip = sr(r'<iframe[^>]+src=["\']([^"\']+)["\']', wp_fallback, 'iframe', default=None)
                if not ip: break
                v = sr(r'data-page=["\'].*?"version"\s*:\s*"([^"]+)"', wp_fallback, 'version', default='')
                if v: h.update({'x-inertia': 'true', 'x-inertia-version': v})
                url_fallback = urljoin(url, ip)
                wp_fallback = dw(url_fallback, v_id, headers=h, note='Downloading iframe')
                
        if not tk:
            raise ExtractorError('Stream token could not be resolved')
        
        raw_url = sr(r"(?:['\"]url['\"]|url)\s*:\s*['\"]([^'\"]+)['\"]", wp, 'url').replace('\\/', '/')
        su = re.sub(r'(/playlist/[^/?]+)(?!\.m3u8)(?=[?#]|$)', r'\1.m3u8', raw_url)
        expires_val = sr(r"['\"]expires['\"]\s*:\s*['\"](\d+)['\"]", wp, 'expires', default='N/A')
        q = {'token': tk, 'expires': expires_val}
        if re.search(r'canPlayFHD\s*=\s*true', wp): q['h'] = '1'

        m3u8_target = update_url_query(su, q)

        self._log_status("Fetching M3U8 Playlist and evaluating subtitles...")
        sub_tracks = []
        try:
            req_headers = {'Referer': url, 'Origin': b, 'User-Agent': TRUSTED_USER_AGENT}
            m3u8_resp = http_session.get(m3u8_target, headers=req_headers, timeout=HTTP_TIMEOUT)
            if m3u8_resp.status_code == 200:
                sub_tracks = parse_m3u8_subtitles(m3u8_resp.text, m3u8_target)
                if hasattr(self, 'ui_ctx') and self.ui_ctx:
                    self._log_status(f"Found {len(sub_tracks)} subtitle tracks. Queueing for post-video download.")
        except Exception:
            pass

        formats = self._extract_m3u8_formats(
            m3u8_target, v_id, 'mp4', m3u8_id='hls', fatal=True, 
            headers={'Referer': url, 'Origin': b, 'User-Agent': TRUSTED_USER_AGENT}
        )

        return {
            'id': v_id, 
            'title': title,
            'formats': formats,
            'sub_tracks': sub_tracks, # Passed securely out to the main worker to delay downloading
            'http_headers': {'Referer': url, 'Origin': b, 'Connection': 'keep-alive', 'User-Agent': TRUSTED_USER_AGENT}
        }

# ==========================================
# CLI INTERFACE & PARALLEL WORKERS
# ==========================================

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def make_request_with_retry(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            response = http_session.get(url, params=params, timeout=HTTP_TIMEOUT)
            if response.status_code == 200:
                return response
        except requests.RequestException:
            if attempt == retries - 1:
                raise
    return None

def tmdb_search(query, media_type):
    url = f"https://api.themoviedb.org/3/search/{media_type}"
    params = {'api_key': TMDB_API_KEY, 'query': query}
    res = make_request_with_retry(url, params)
    return res.json().get('results', []) if res else []

def tmdb_find_by_imdb(imdb_id):
    url = f"https://api.themoviedb.org/3/find/{imdb_id}"
    params = {'api_key': TMDB_API_KEY, 'external_source': 'imdb_id'}
    res = make_request_with_retry(url, params)
    if res:
        data = res.json()
        if data.get('tv_results'):
            return str(data['tv_results'][0]['id']), 'tv', data['tv_results'][0].get('name')
        elif data.get('movie_results'):
            return str(data['movie_results'][0]['id']), 'movie', data['movie_results'][0].get('title')
    return imdb_id, None, "Unknown Title"

def fetch_tv_details(tmdb_id):
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
    params = {'api_key': TMDB_API_KEY}
    res = make_request_with_retry(url, params)
    return res.json() if res else None

def fetch_season_episodes(tmdb_id, season_num):
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_num}"
    params = {'api_key': TMDB_API_KEY}
    res = make_request_with_retry(url, params)
    return res.json().get('episodes', []) if res else []

def prompt_choice(prompt_text, max_val):
    while True:
        try:
            choice = int(input(prompt_text))
            if 1 <= choice <= max_val:
                return choice
            print("Invalid choice. Please select a valid number.")
        except ValueError:
            print("Please enter a number.")

def parse_episode_selection(ep_str, total_episodes):
    """Parses episode selection input like '1-5', '1,3,5', or 'all'."""
    ep_str = ep_str.strip().lower()
    if ep_str == 'all':
        return list(range(1, total_episodes + 1))
    
    selected = set()
    parts = ep_str.split(',')
    for part in parts:
        part = part.strip()
        if '-' in part:
            try:
                start, end = map(int, part.split('-'))
                selected.update(range(start, end + 1))
            except ValueError:
                pass
        else:
            try:
                selected.add(int(part))
            except ValueError:
                pass
    return sorted([e for e in selected if 1 <= e <= total_episodes])

class EpisodeLogger:
    """Custom Logger that integrates with Rich to provide real-time updates without breaking progress bars."""
    def __init__(self, progress_context, filename):
        self.progress = progress_context
        self.filename = filename

    def debug(self, msg):
        msg = msg.strip()
        if any(x in msg for x in ["[vixsrc]", "Destination:", "[Merger]", "[ExtractAudio]"]):
            self.progress.console.print(f"[dim cyan][{self.filename}][/dim cyan] {msg}")

    def warning(self, msg):
        self.progress.console.print(f"[bold yellow][{self.filename} Warning][/bold yellow] {msg}")

    def error(self, msg):
        self.progress.console.print(f"[bold red][{self.filename} Error][/bold red] {msg}")


def download_single_episode(task_info, progress, master_task):
    """Worker function for downloading a single episode with granular progress bars."""
    url = task_info['url']
    out_dir = task_info['out_dir']
    filename_base = sanitize_filename(task_info['filename_base'])
    selected_height = task_info['selected_height']
    selected_lang = task_info['selected_lang']
    
    ui_ctx = {'progress': progress, 'master_task': master_task, 'filename_base': filename_base}
    active_tasks = {}

    def episode_progress_hook(d):
        filepath = d.get('filename', 'unknown')
        if d['status'] == 'downloading':
            if filepath not in active_tasks:
                is_audio = False
                info = d.get('info_dict', {})
                if info.get('vcodec') == 'none' or (info.get('acodec') != 'none' and not info.get('vcodec')):
                    is_audio = True
                if filepath.endswith('.m4a') or filepath.endswith('.aac') or filepath.endswith('.mp3'):
                    is_audio = True

                media_type = "Audio Stream" if is_audio else "Video Stream"
                color = "magenta" if is_audio else "cyan"
                task_name = f"[{color}]{media_type}: {filename_base}"
                active_tasks[filepath] = progress.add_task(task_name, total=100)
            
            task_id = active_tasks[filepath]
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            
            pct = 0
            if total > 0:
                pct = (downloaded / total) * 100
            elif d.get('fragment_count', 0) > 0:
                pct = (d.get('fragment_index', 0) / d['fragment_count']) * 100
            else:
                pct_str = RE_PERCENT_STR.sub('', d.get('_percent_str', '0%')).strip().replace('%','')
                try: pct = float(pct_str)
                except ValueError: pass
            
            progress.update(task_id, completed=pct)

        elif d['status'] == 'finished':
            if filepath in active_tasks:
                task_id = active_tasks[filepath]
                progress.update(task_id, completed=100, description=f"[bold green]Stream Done: {filename_base}")

    def episode_postprocessor_hook(d):
        if d['status'] == 'started' and d.get('postprocessor') == 'Merger':
            active_tasks['yt_merger'] = progress.add_task(f"[yellow]Merging A/V: {filename_base}", total=None)
        elif d['status'] == 'finished' and d.get('postprocessor') == 'Merger':
            if 'yt_merger' in active_tasks:
                progress.update(active_tasks['yt_merger'], completed=100, description=f"[bold green]A/V Merged: {filename_base}", total=100)

    format_selector = f'bestvideo[height={selected_height}]+bestaudio[language~={selected_lang}]/bestvideo[height={selected_height}]+bestaudio/best[height={selected_height}]'
    
    download_opts = {
        'format': format_selector,
        'outtmpl': os.path.join(out_dir, f"{filename_base}.%(ext)s"),
        'merge_output_format': 'mp4',
        'progress_hooks': [episode_progress_hook],
        'postprocessor_hooks': [episode_postprocessor_hook],
        'logger': EpisodeLogger(progress, filename_base),
        'quiet': False, 
        'noprogress': True,
        'continuedl': True,         # CRITICAL FIX: Safe Resume Support
        'hls_prefer_native': True, 
        'retries': NETWORK_RETRIES,
        'fragment_retries': FRAGMENT_RETRIES,
        'concurrent_fragment_downloads': MAX_FRAGMENT_CONCURRENT_DOWNLOADS,
        'http_headers': {
            'User-Agent': TRUSTED_USER_AGENT,
            'Referer': url,
            'Origin': 'https://vixsrc.to'
        },
    }

    try:
        with YoutubeDL(download_opts) as ydl:
            ydl.add_info_extractor(VixSrcIE(ui_ctx=ui_ctx))
            info = ydl.extract_info(url, download=True, ie_key='VixSrc')
            
            # CRITICAL FIX: Muxing Flow Order (Extract -> Video -> Subs -> Mux)
            sub_tracks = info.get('sub_tracks', [])
            target_video_file = os.path.join(out_dir, f"{filename_base}.mp4")
            
            if sub_tracks and os.path.exists(target_video_file):
                req_headers = {'Referer': url, 'Origin': 'https://vixsrc.to', 'User-Agent': TRUSTED_USER_AGENT}
                progress.console.print(f"[dim cyan][Subtitles][/dim cyan] Embedding {len(sub_tracks)} subtitle track(s) for {filename_base}...")
                
                downloaded_sub_tracks = process_and_download_subtitles(sub_tracks, None, req_headers, ui_ctx)
                if downloaded_sub_tracks:
                    embed_subtitles_ffmpeg(downloaded_sub_tracks, target_video_file, target_video_file, ui_ctx)

        progress.update(master_task, advance=1)
        return True, filename_base
    except Exception as e:
        progress.console.print(f"[bold red]Failed: {filename_base} ({str(e)})[/bold red]")
        progress.update(master_task, advance=1)
        return False, f"{filename_base} ({str(e)})"

def main():
    clear_screen()
    console.print(f"{Fore.CYAN}==================================================")
    console.print(f"{Fore.CYAN}         VixSrc Advanced Downloader v2.0          ")
    logging_status = f"{Fore.GREEN}[ LOGGING: ON ]" if ENABLE_LOGGING else f"{Fore.RED}[ LOGGING: OFF ]"
    console.print(f"  {logging_status} {Fore.YELLOW}| Concurrent Workers: {MAX_CONCURRENT_DOWNLOADS}{Style.RESET_ALL}")
    console.print(f"{Fore.CYAN}==================================================\n")
    
    print("1) Movie\n2) TV Show\n")
    media_choice = prompt_choice("Choice: ", 2)
    media_type = "movie" if media_choice == 1 else "tv"

    print("\nSearch by:\n1) IMDb ID\n2) TMDb ID\n3) Search by Name\n")
    search_choice = prompt_choice("Choice: ", 3)

    tmdb_id = ""
    title_name = "Unknown Title"

    if search_choice == 3:
        query = input(f"\nEnter {media_type.replace('tv', 'TV show').capitalize()} name:\n> ")
        results = tmdb_search(query, media_type)
        if not results:
            console.print("[red]No results found.[/red]")
            return

        for idx, res in enumerate(results[:5], 1):
            name = res.get('title') if media_type == 'movie' else res.get('name')
            date = res.get('release_date') if media_type == 'movie' else res.get('first_air_date')
            year = date.split('-')[0] if date else "Unknown Year"
            print(f"{idx}) {name} ({year})")
        
        selection = prompt_choice("\nSelect: ", len(results[:5]))
        selected_media = results[selection - 1]
        tmdb_id = str(selected_media['id'])
        title_name = selected_media.get('title') if media_type == 'movie' else selected_media.get('name')

    elif search_choice == 2:
        tmdb_id = input("\nEnter TMDb ID: ")
        if media_type == "tv":
            res = fetch_tv_details(tmdb_id)
            title_name = res.get('name', 'TV Show') if res else 'TV Show'

    elif search_choice == 1:
        imdb_id = input("\nEnter IMDb ID: ")
        tmdb_id, inferred_type, title_name = tmdb_find_by_imdb(imdb_id)
        if inferred_type:
            media_type = inferred_type

    tasks_to_download = []
    title_name = sanitize_filename(title_name)

    if media_type == "tv":
        tv_details = fetch_tv_details(tmdb_id)
        if not tv_details or 'seasons' not in tv_details:
            console.print(f"{Fore.RED}Could not load show configurations.")
            return
            
        seasons = [s for s in tv_details['seasons'] if s['season_number'] > 0]
        print("\nAvailable Seasons:")
        for s in seasons:
            print(f"Season {s['season_number']} ({s['episode_count']} Episodes)")
            
        season = input("\nSelect Season: ")
        episodes_list = fetch_season_episodes(tmdb_id, season)
        
        if episodes_list:
            print("\nAvailable Episodes:")
            current_date = datetime.now().date()
            for ep in episodes_list:
                ep_num = ep.get('episode_number')
                ep_name = ep.get('name', 'No Title')
                air_date_str = ep.get('air_date')
                is_released = False
                if air_date_str:
                    try:
                        if datetime.strptime(air_date_str, "%Y-%m-%d").date() <= current_date:
                            is_released = True
                    except ValueError: pass
                
                status_str = f"{Fore.GREEN}(Released)" if is_released else f"{Style.DIM}{Fore.WHITE}(Unreleased)"
                print(f"  {ep_num}) {ep_name} {status_str}")
                    
            ep_input = input(f"\nSelect Episode(s) (e.g., '1', '1-5', '1,3,5', or 'all'): ")
            selected_episodes = parse_episode_selection(ep_input, len(episodes_list))
        else:
            ep_single = input("Enter Episode Number manually: ")
            selected_episodes = [int(ep_single)]

        for ep_num in selected_episodes:
            url_target = f"https://vixsrc.to/tv/{tmdb_id}/{season}/{ep_num}"
            filename_base = f"{title_name} - S{int(season):02d}E{int(ep_num):02d}"
            tasks_to_download.append({'url': url_target, 'filename_base': filename_base})
    else:
        url_target = f"https://vixsrc.to/movie/{tmdb_id}"
        filename_base = title_name
        tasks_to_download.append({'url': url_target, 'filename_base': filename_base})

    print("\nLanguage:\n1) English\n2) Italian\n3) Hindi\n4) Japanese\n5) Custom\n")
    lang_choice = prompt_choice("Choice: ", 5)
    lang_map = {1: 'en', 2: 'it', 3: 'hi', 4: 'ja', 5: 'en'}
    selected_lang = lang_map.get(lang_choice, 'en')

    for task in tasks_to_download:
        task['url'] = update_url_query(task['url'], {'lang': selected_lang})

    print("\nProbing available video qualities...")
    probe_url = tasks_to_download[0]['url']
    ydl_opts = {'quiet': True, 'no_warnings': True, 'http_headers': {'User-Agent': TRUSTED_USER_AGENT}}
    
    with YoutubeDL(ydl_opts) as ydl:
        ydl.add_info_extractor(VixSrcIE())
        try:
            info = ydl.extract_info(probe_url, download=False, ie_key='VixSrc')
        except Exception as e:
            console.print(f"\n[red][!] Extraction engine failed: {e}[/red]")
            return

    formats = info.get('formats', [])
    video_formats = sorted([f for f in formats if f.get('height') is not None], key=lambda x: x.get('height', 0), reverse=True)
    
    unique_heights, display_formats = [], []
    for f in video_formats:
        h = f.get('height')
        if h not in unique_heights:
            unique_heights.append(h)
            display_formats.append(f)

    print("\nAvailable qualities:\n")
    for idx, f in enumerate(display_formats, 1):
        res = f"{f.get('height', 'Unknown')}p"
        label = f"Best ({res})" if idx == 1 else res
        print(f"{idx}) {label}")

    quality_choice = prompt_choice("\nSelect quality: ", len(display_formats))
    selected_height = display_formats[quality_choice - 1]['height']

    default_dir = DEFAULT_DOWNLOAD_DIR if DEFAULT_DOWNLOAD_DIR and os.path.exists(DEFAULT_DOWNLOAD_DIR) else os.getcwd()
    out_dir = input(f"\nDownload folder (Press Enter for '{default_dir}'): ").strip() or default_dir
    os.makedirs(out_dir, exist_ok=True)

    for task in tasks_to_download:
        task['out_dir'] = out_dir
        task['selected_height'] = selected_height
        task['selected_lang'] = selected_lang

    confirm = input(f"\nStart processing {len(tasks_to_download)} download task(s)? (Y/n): ").strip().lower()
    if confirm not in ['y', 'yes', '']:
        console.print("[yellow]Download cancelled.[/yellow]")
        return

    console.print(f"\n[bold green]Spawning Multithreaded Queue ({MAX_CONCURRENT_DOWNLOADS} Active Workers)...[/bold green]\n")

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
        expand=True
    )

    with progress:
        master_task = progress.add_task("[bold yellow]Overall Season Batch Progress", total=len(tasks_to_download))
        
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS) as executor:
            futures = [executor.submit(download_single_episode, task, progress, master_task) for task in tasks_to_download]
            for future in as_completed(futures):
                future.result()

    console.print(f"\n[bold green][✓] Batch processing complete! Output folder:[/bold green] {out_dir}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print(f"\n\n[bold red]Process terminated by user.[/bold red]")
        sys.exit(0)


