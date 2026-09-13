import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import traceback
import urllib.parse
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from dotenv import load_dotenv

# Загрузка переменных окружения из .env
load_dotenv()

# ================= КОНФИГУРАЦИЯ =================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
MAX_DURATION = int(os.getenv("MAX_DURATION", "600"))  # В секундах (по умолчанию 10 минут)
INACTIVITY_TIMEOUT = int(os.getenv("INACTIVITY_TIMEOUT", "10"))  # Секунд до выхода при пустом ГС
EMPTY_QUEUE_TIMEOUT = int(os.getenv("EMPTY_QUEUE_TIMEOUT", "20"))  # Секунд до выхода при пустой очереди

def _parse_color(hex_str: str | None, default: int = 0xFFA200) -> int:
    if not hex_str:
        return default
    hex_str = hex_str.strip()
    try:
        return int(hex_str, 16) if hex_str.startswith("0x") else int(hex_str.lstrip("#"), 16)
    except ValueError:
        return default

EMBED_COLOR = _parse_color(os.getenv("EMBED_COLOR"), 0xFFA200)
ERROR_COLOR = 0xFF7777  # Точный hsla(0, 100%, 73.3%, 1)

# Базовые ID эмодзи кнопок
EMOJIS = {
    'cross': '<:MusicBot_cross:1423564527584280677>',
    'play': '<:MusicBot_play:1423564532097351691>',
    'pause': '<:MusicBot_pause:1423564531279331388>',
    'prev': '<:MusicBot_previous:1423564533347258418>',
    'next': '<:MusicBot_next:1423564530016977098>',
    'stop': '<:MusicBot_stop:1423564541119434752>',
    'queue': '<:MusicBot_queue:1423564539974258740>',
    'tick': '<:MusicBot_tick:1423564541974941736>',
    'spotify': '',
    'youtube': '',
    'soundcloud': ''
}
# ================================================

# Инициализация клиента Spotify
sp = None
if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
    try:
        sp = spotipy.Spotify(
            auth_manager=SpotifyClientCredentials(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET
            )
        )
    except Exception as e:
        print(f"Предупреждение: Не удалось подключить Spotify API: {e}")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, allowed_mentions=discord.AllowedMentions.none())

COOKIE_FILE = None
for candidate in [
    os.getenv("COOKIE_PATH"),
    os.path.join(os.getenv("DATA_DIR", ""), "cookies.txt") if os.getenv("DATA_DIR") else None,
    "/app/data/cookies.txt",
    "/app/cookies.txt",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt"),
    "cookies.txt"
]:
    if candidate and os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
        COOKIE_FILE = candidate
        break

def get_safe_cookies_path() -> str | None:
    """Возвращает путь к изолированной копии cookies.txt, чтобы yt-dlp не затирал исходные куки гостевыми Set-Cookie"""
    if not COOKIE_FILE or not os.path.exists(COOKIE_FILE):
        return None
    try:
        import tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.txt', prefix='cookies_')
        tmp.close()
        shutil.copyfile(COOKIE_FILE, tmp.name)
        return tmp.name
    except Exception:
        return COOKIE_FILE

YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'remote_components': ['ejs:github'],
}

YTDL_SEARCH_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'extract_flat': True,
    'skip_download': True,
    'source_address': '0.0.0.0',
    'socket_timeout': 10,
    'remote_components': ['ejs:github'],
}

active_cookie_file = get_safe_cookies_path()
if active_cookie_file:
    print(f"Найден файл cookies: {COOKIE_FILE}, подключаем к yt-dlp...")
    YTDL_OPTIONS['cookiefile'] = active_cookie_file
    YTDL_SEARCH_OPTIONS['cookiefile'] = active_cookie_file

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

def parse_colon_time(s: str) -> int:
    try:
        parts = s.strip().split(':')
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except Exception:
        pass
    return 0

def extract_artist_from_view_model(view_model: dict) -> str | None:
    if not view_model or not isinstance(view_model, dict):
        return None
    try:
        lockup = view_model.get('metadata', {}).get('lockupMetadataViewModel', {})
        content_mdvm = lockup.get('metadata', {}).get('contentMetadataViewModel', {})
        for row in content_mdvm.get('metadataRows', []):
            for part in row.get('metadataParts', []):
                text_obj = part.get('text', {})
                if isinstance(text_obj, dict):
                    c = text_obj.get('content')
                    if c and isinstance(c, str):
                        c_strip = c.strip()
                        if c_strip and not any(w in c_strip.lower() for w in ['view', 'views', 'просмотр', 'ago', 'назад', 'streamed', 'трансляци']):
                            return c_strip
                    runs = text_obj.get('runs', [])
                    for r in runs:
                        t = r.get('text')
                        if t and isinstance(t, str):
                            t_strip = t.strip()
                            if t_strip and not any(w in t_strip.lower() for w in ['view', 'views', 'просмотр', 'ago', 'назад', 'streamed', 'трансляци']):
                                return t_strip

        title_obj = lockup.get('title', {})
        label = (title_obj.get('accessibility', {}).get('accessibilityData', {}).get('label')
                 or view_model.get('accessibilityData', {}).get('label') or '')
        if label:
            m = re.search(r'\b(?:by|от(?:\s+исполнителя|\s+канала)?)\s+([^0-9\n\r•]+?)(?:\s+\d+\s*(?:minute|second|hour|минут|секунд|час|view|просмотр)|\s+\d+:\d+|$)', label, re.IGNORECASE)
            if m:
                cand = m.group(1).strip(' ,.-')
                if cand and len(cand) < 60:
                    return cand
    except Exception:
        pass
    return None

def extract_duration_from_view_model(view_model: dict) -> int:
    if not view_model or not isinstance(view_model, dict):
        return 0
    try:
        content_img = view_model.get('contentImage', {})
        thumb_vm = content_img.get('thumbnailViewModel', {})
        overlays = thumb_vm.get('overlays', []) or []
        for ov in overlays:
            if not isinstance(ov, dict):
                continue
            time_status = ov.get('thumbnailOverlayTimeStatusRenderer', {})
            if time_status:
                text_data = time_status.get('text', {})
                s = text_data.get('simpleText') or ''
                if not s and 'runs' in text_data:
                    s = ''.join(r.get('text', '') for r in text_data['runs'])
                if s and ':' in s:
                    dur = parse_colon_time(s)
                    if dur > 0:
                        return dur

            badges = (ov.get('thumbnailBottomOverlayViewModel', {}).get('badges', [])
                      or ov.get('thumbnailOverlayBadgeViewModel', {}).get('thumbnailBadges', []))
            for b in badges:
                badge_vm = b.get('thumbnailBadgeViewModel', {})
                txt = badge_vm.get('text', '')
                if isinstance(txt, dict):
                    txt = txt.get('content', '')
                if isinstance(txt, str) and ':' in txt:
                    dur = parse_colon_time(txt)
                    if dur > 0:
                        return dur

        lockup = view_model.get('metadata', {}).get('lockupMetadataViewModel', {})
        title_obj = lockup.get('title', {})
        label = (title_obj.get('accessibility', {}).get('accessibilityData', {}).get('label')
                 or view_model.get('accessibilityData', {}).get('label') or '')
        if label:
            m_c = re.search(r'\b(?:(\d+):)?(\d+):(\d+)\b', label)
            if m_c:
                g1, g2, g3 = m_c.groups()
                if g1:
                    return int(g1) * 3600 + int(g2) * 60 + int(g3)
                return int(g2) * 60 + int(g3)
            h = 0
            m = 0
            s = 0
            m_h = re.search(r'(\d+)\s*(?:hours?|часа?|часов|ч\b)', label, re.IGNORECASE)
            if m_h:
                h = int(m_h.group(1))
            m_m = re.search(r'(\d+)\s*(?:minutes?|минуты?|минут|мин\b)', label, re.IGNORECASE)
            if m_m:
                m = int(m_m.group(1))
            m_s = re.search(r'(\d+)\s*(?:seconds?|секунды?|секунд|сек\b)', label, re.IGNORECASE)
            if m_s:
                s = int(m_s.group(1))
            if h or m or s:
                return h * 3600 + m * 60 + s
    except Exception:
        pass
    return 0

try:
    from yt_dlp.extractor.youtube._tab import YoutubeTabBaseInfoExtractor
    _orig_extract_lockup_view_model = YoutubeTabBaseInfoExtractor._extract_lockup_view_model

    def _patched_extract_lockup_view_model(self, view_model):
        res = _orig_extract_lockup_view_model(self, view_model)
        if not res or not isinstance(res, dict):
            return res

        try:
            if not res.get('uploader') or not res.get('channel') or res.get('uploader') in ('Unknown', 'Unknown artist'):
                artist = extract_artist_from_view_model(view_model)
                if artist:
                    res['channel'] = artist
                    res['uploader'] = artist

            if not res.get('duration'):
                dur = extract_duration_from_view_model(view_model)
                if dur > 0:
                    res['duration'] = dur
        except Exception:
            pass

        return res

    YoutubeTabBaseInfoExtractor._extract_lockup_view_model = _patched_extract_lockup_view_model

    _orig_extract_video = YoutubeTabBaseInfoExtractor._extract_video

    def _patched_extract_video(self, renderer):
        res = _orig_extract_video(self, renderer)
        if not res or not isinstance(res, dict):
            return res
        try:
            if not res.get('duration'):
                lt = self._get_text(renderer, 'lengthText')
                if lt and ':' in lt:
                    d = parse_colon_time(lt)
                    if d > 0:
                        res['duration'] = d
            if not res.get('uploader') or not res.get('channel'):
                ch = self._get_text(renderer, 'ownerText', 'shortBylineText')
                if ch:
                    res['channel'] = ch
                    res['uploader'] = ch
        except Exception:
            pass
        return res

    YoutubeTabBaseInfoExtractor._extract_video = _patched_extract_video
except Exception as _patch_err:
    print(f"Предупреждение: не удалось применить патч к YoutubeTabBaseInfoExtractor: {_patch_err}")

def parse_entry_duration(entry: dict) -> int:
    if not entry or not isinstance(entry, dict):
        return 0
    dur = entry.get('duration')
    if dur is not None:
        try:
            val = int(float(dur))
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass
    for k in ('duration_string', 'lengthText', 'approx_duration_ms'):
        v = entry.get(k)
        if not v:
            continue
        if k == 'approx_duration_ms':
            try:
                return int(float(v) / 1000)
            except Exception:
                continue
        s = str(v).strip()
        if ':' in s:
            parts = s.split(':')
            try:
                if len(parts) == 2:
                    return int(parts[0]) * 60 + int(parts[1])
                elif len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            except ValueError:
                pass
    return 0

def parse_entry_artist(entry: dict) -> str:
    if not entry or not isinstance(entry, dict):
        return 'Unknown artist'
    for k in ('uploader', 'channel', 'artist', 'creator', 'uploader_id'):
        val = entry.get(k)
        if val and isinstance(val, str):
            s = val.strip()
            if s and s.lower() not in ('unknown', 'unknown artist', 'none'):
                return s
    title = entry.get('title') or ''
    if ' - ' in title:
        cand = title.split(' - ')[0].strip()
        if cand and len(cand) < 50 and cand.lower() not in ('unknown', 'various artists'):
            return cand
    return 'Unknown artist'

def extract_first_entry(data: dict | None) -> dict:
    if not data or not isinstance(data, dict):
        return {}
    if 'entries' in data:
        entries = data.get('entries')
        if entries is not None:
            if not isinstance(entries, list):
                try:
                    entries = list(entries)
                except Exception:
                    entries = []
            for item in entries:
                if item and isinstance(item, dict):
                    return item
        return {}
    return data

ytdl_search = yt_dlp.YoutubeDL(YTDL_SEARCH_OPTIONS)

def format_time(seconds: int | float) -> str:
    seconds = int(seconds or 0)
    mins = seconds // 60
    secs = seconds % 60
    return f"{mins:02d}:{secs:02d}"

def probe_stream_metadata(url: str) -> dict:
    """Извлекает duration, title, artist из аудиопотока/файла через ffprobe или ffmpeg"""
    result = {'duration': 0, 'title': None, 'artist': None}

    # 1. Попытка через ffprobe
    ffprobe_exe = shutil.which('ffprobe')
    if ffprobe_exe:
        try:
            cmd = [
                ffprobe_exe,
                '-v', 'quiet',
                '-print_format', 'json',
                '-show_format',
                '-show_streams',
                url
            ]
            flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags, timeout=10)
            if proc.returncode == 0 and proc.stdout:
                info = json.loads(proc.stdout)
                fmt = info.get('format', {})
                raw_dur = fmt.get('duration')
                if raw_dur and raw_dur != 'N/A':
                    try:
                        result['duration'] = int(float(raw_dur))
                    except (ValueError, TypeError):
                        pass

                if not result['duration']:
                    for st in info.get('streams', []):
                        st_dur = st.get('duration')
                        if st_dur and st_dur != 'N/A':
                            try:
                                result['duration'] = int(float(st_dur))
                                break
                            except (ValueError, TypeError):
                                pass

                tags = fmt.get('tags') or {}
                lower_tags = {k.lower(): v for k, v in tags.items()}
                result['title'] = lower_tags.get('title')
                result['artist'] = lower_tags.get('artist') or lower_tags.get('album_artist') or lower_tags.get('composer')
                if result['duration'] > 0:
                    return result
        except Exception as e:
            print(f"ffprobe probe error: {e}")

    # 2. Фолбэк на ffmpeg (-hide_banner -i)
    ffmpeg_exe = shutil.which('ffmpeg')
    if ffmpeg_exe:
        try:
            cmd = [
                ffmpeg_exe,
                '-hide_banner',
                '-i', url
            ]
            flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags, timeout=10)
            output = (proc.stderr or "") + "\n" + (proc.stdout or "")

            dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
            if dur_match:
                hours = int(dur_match.group(1))
                minutes = int(dur_match.group(2))
                seconds = float(dur_match.group(3))
                result['duration'] = int(hours * 3600 + minutes * 60 + seconds)

            if not result['title']:
                title_match = re.search(r"^\s*title\s*:\s*(.+)$", output, re.IGNORECASE | re.MULTILINE)
                if title_match:
                    result['title'] = title_match.group(1).strip()

            if not result['artist']:
                artist_match = re.search(r"^\s*artist\s*:\s*(.+)$", output, re.IGNORECASE | re.MULTILINE)
                if artist_match:
                    result['artist'] = artist_match.group(1).strip()
        except Exception as e:
            print(f"ffmpeg probe error: {e}")

    return result

async def ensure_emojis():
    """Синхронизирует MusicBot Application Emojis для корректного отображения кастомных эмодзи в тексте"""
    try:
        app_emojis = await bot.fetch_application_emojis()
        existing = {e.name: e for e in app_emojis}

        emoji_sources = {
            'MusicBot_cross': '1423564527584280677',
            'MusicBot_play': '1423564532097351691',
            'MusicBot_pause': '1423564531279331388',
            'MusicBot_previous': '1423564533347258418',
            'MusicBot_next': '1423564530016977098',
            'MusicBot_stop': '1423564541119434752',
            'MusicBot_queue': '1423564539974258740',
            'MusicBot_tick': '1423564541974941736',
            'MusicBot_spotify': 'https://cdn3.emoji.gg/emojis/870570-spotify.png',
            'MusicBot_youtube': 'https://cdn3.emoji.gg/emojis/YouTube.png',
            'MusicBot_soundcloud': 'https://cdn3.emoji.gg/emojis/4678_SoundCloud.png'
        }

        async with aiohttp.ClientSession() as session:
            for name, orig_id in emoji_sources.items():
                if name not in existing and name.lower() not in existing:
                    try:
                        url = orig_id if orig_id.startswith("http") else f"https://cdn.discordapp.com/emojis/{orig_id}.png"
                        async with session.get(url) as resp:
                            if resp.status == 200:
                                img_data = await resp.read()
                                new_emoji = await bot.create_application_emoji(name=name, image=img_data)
                                existing[name] = new_emoji
                                print(f"Загружен собственный Application Emoji: {name} ({new_emoji.id})")
                    except Exception as err:
                        print(f"Не удалось загрузить эмодзи {name}: {err}")

        # Также проверяем серверные эмодзи гильдий
        for guild in bot.guilds:
            for e in guild.emojis:
                if e.name not in existing:
                    existing[e.name] = e

        key_map = {
            'cross': 'MusicBot_cross',
            'play': 'MusicBot_play',
            'pause': 'MusicBot_pause',
            'prev': 'MusicBot_previous',
            'next': 'MusicBot_next',
            'stop': 'MusicBot_stop',
            'queue': 'MusicBot_queue',
            'tick': 'MusicBot_tick',
            'spotify': 'MusicBot_spotify',
            'youtube': 'MusicBot_youtube',
            'soundcloud': 'MusicBot_soundcloud'
        }
        for key, name in key_map.items():
            target = existing.get(name) or existing.get(name.lower())
            if target:
                EMOJIS[key] = str(target)

        print(f"Готово! Активные эмодзи MusicBot: {list(existing.keys())}")
    except Exception as e:
        print(f"Инфо по Application Emojis: {e}")

DATA_DIR = os.getenv("DATA_DIR")
if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)
    DB_PATH = os.path.join(DATA_DIR, "blacklist.db")
else:
    DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blacklist.db")

class BlacklistDB:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._cache: dict[int, set[int]] = {}  # guild_id -> set of user_ids
        self._init_db()
        self._load_cache()

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        try:
            with self._get_conn() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS blacklist (
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        added_by INTEGER,
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (guild_id, user_id)
                    )
                """)
                conn.commit()
        except Exception as e:
            print(f"Ошибка инициализации базы данных blacklist: {e}")

    def _load_cache(self):
        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT guild_id, user_id FROM blacklist")
                for guild_id, user_id in cursor.fetchall():
                    if guild_id not in self._cache:
                        self._cache[guild_id] = set()
                    self._cache[guild_id].add(user_id)
        except Exception as e:
            print(f"Ошибка загрузки кэша blacklist: {e}")

    def is_blacklisted(self, guild_id: int | None, user_id: int | None) -> bool:
        if not guild_id or not user_id:
            return False
        return user_id in self._cache.get(guild_id, set())

    def add(self, guild_id: int, user_id: int, added_by: int | None = None) -> bool:
        if self.is_blacklisted(guild_id, user_id):
            return False
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO blacklist (guild_id, user_id, added_by) VALUES (?, ?, ?)",
                    (guild_id, user_id, added_by)
                )
                conn.commit()
        except Exception as e:
            print(f"Ошибка добавления в blacklist: {e}")
            return False

        if guild_id not in self._cache:
            self._cache[guild_id] = set()
        self._cache[guild_id].add(user_id)
        return True

    def remove(self, guild_id: int, user_id: int) -> bool:
        if not self.is_blacklisted(guild_id, user_id):
            return False
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "DELETE FROM blacklist WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id)
                )
                conn.commit()
        except Exception as e:
            print(f"Ошибка удаления из blacklist: {e}")
            return False

        if guild_id in self._cache:
            self._cache[guild_id].discard(user_id)
        return True

    def get_blacklisted_details(self, guild_id: int) -> list[dict]:
        try:
            with self._get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT user_id, added_by, added_at FROM blacklist WHERE guild_id = ? ORDER BY added_at ASC",
                    (guild_id,)
                )
                rows = cursor.fetchall()
                return [{'user_id': r[0], 'added_by': r[1], 'added_at': r[2]} for r in rows]
        except Exception as e:
            print(f"Ошибка получения списка blacklist: {e}")
            return []

db_blacklist = BlacklistDB()

def is_admin_or_manager(member: discord.Member | discord.User | None) -> bool:
    if not member or not hasattr(member, 'guild') or not member.guild:
        return False
    if member.guild.owner_id == member.id:
        return True
    perms = getattr(member, 'guild_permissions', None)
    if not perms:
        return False
    return perms.administrator or perms.manage_guild

class Track:
    def __init__(self, title, artist, duration, url, stream_url, requester):
        self.title = title
        self.artist = artist
        self.duration = duration
        self.url = url
        self.stream_url = stream_url
        self.requester = requester
        self.start_time = None

class StatusMessageView(discord.ui.LayoutView):
    def __init__(self, text: str, footer: str | None = None):
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(discord.ui.TextDisplay(text))
        if footer:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(footer))
        self.add_item(container)

class ErrorMessageView(discord.ui.LayoutView):
    def __init__(self, text: str):
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_color=ERROR_COLOR)
        container.add_item(discord.ui.TextDisplay(text))
        self.add_item(container)

class QueuedMessageView(discord.ui.LayoutView):
    def __init__(self, track_obj: Track, pos: int):
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(discord.ui.TextDisplay(
            f"### Queued at position #{pos}\n"
            f"[{track_obj.title}]({track_obj.url}) by [{track_obj.artist}]({track_obj.url}) [{format_time(track_obj.duration)}]"
        ))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            "-# Not the correct track? Try being more specific or use `/search`"
        ))
        self.add_item(container)

class PlayerControlView(discord.ui.LayoutView):
    def __init__(self, player: 'MusicPlayer', action_user: discord.User | discord.Member | None = None):
        super().__init__(timeout=None)
        self.player = player
        self.action_user = action_user
        self.build_ui()

    def build_ui(self):
        self.clear_items()
        curr = self.player.current
        if not curr:
            return

        vc = self.player.voice_client
        pause_emoji = EMOJIS['play'] if (vc and vc.is_paused()) else EMOJIS['pause']
        prev_disabled = len(self.player.history) <= 1

        container = discord.ui.Container(accent_color=EMBED_COLOR)

        # 1. Заголовок и информация о треке
        track_info = (
            f"### ▶ Now Playing\n"
            f"[{curr.title}]({curr.url}) by [{curr.artist}]({curr.url}) [{format_time(curr.duration)}]"
        )
        container.add_item(discord.ui.TextDisplay(track_info))

        # 2. Первый разделитель
        container.add_item(discord.ui.Separator())

        # 3. Кнопки управления треком
        row = discord.ui.ActionRow()

        self.pause_resume_btn = discord.ui.Button(
            style=discord.ButtonStyle.success,
            emoji=discord.PartialEmoji.from_str(pause_emoji),
            custom_id="btn_pause"
        )
        self.pause_resume_btn.callback = self.on_pause_resume
        row.add_item(self.pause_resume_btn)

        self.prev_btn = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            emoji=discord.PartialEmoji.from_str(EMOJIS['prev']),
            disabled=prev_disabled,
            custom_id="btn_prev"
        )
        self.prev_btn.callback = self.on_prev
        row.add_item(self.prev_btn)

        self.next_btn = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            emoji=discord.PartialEmoji.from_str(EMOJIS['next']),
            custom_id="btn_next"
        )
        self.next_btn.callback = self.on_next
        row.add_item(self.next_btn)

        self.stop_btn = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            emoji=discord.PartialEmoji.from_str(EMOJIS['stop']),
            custom_id="btn_stop"
        )
        self.stop_btn.callback = self.on_stop
        row.add_item(self.stop_btn)

        self.queue_btn = discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            emoji=discord.PartialEmoji.from_str(EMOJIS['queue']),
            custom_id="btn_queue"
        )
        self.queue_btn.callback = self.on_queue
        row.add_item(self.queue_btn)

        container.add_item(row)

        # 4. Второй разделитель
        container.add_item(discord.ui.Separator())

        # 5. Маленькая подпись после второго разделителя
        footer_text = f"-# Track requested by {curr.requester.mention}"
        if self.action_user:
            footer_text += f" • Action by {self.action_user.mention}"
        container.add_item(discord.ui.TextDisplay(footer_text))

        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cross_emoji = EMOJIS.get('cross', '')
        vc = self.player.voice_client or (interaction.guild.voice_client if interaction.guild else None)
        user_voice = interaction.user.voice if isinstance(interaction.user, discord.Member) else None

        # 1. Проверка нахождения в том же голосовом канале с ботом
        if not vc or not vc.channel:
            view = ErrorMessageView(f"### {cross_emoji} Бот не находится в голосовом канале.")
            await interaction.response.send_message(view=view, ephemeral=True)
            return False

        if not user_voice or not user_voice.channel or user_voice.channel.id != vc.channel.id:
            view = ErrorMessageView(f"### {cross_emoji} Вы должны находиться в том же голосовом канале, что и бот ({vc.channel.name}), чтобы использовать панель управления!")
            await interaction.response.send_message(view=view, ephemeral=True)
            return False

        # 2. Проверка чёрного списка (только если пользователь уже находится в нужном ГС)
        if db_blacklist.is_blacklisted(interaction.guild_id, interaction.user.id):
            view = ErrorMessageView(f"### {cross_emoji} Вы находитесь в чёрном списке этого сервера и не можете взаимодействовать с ботом.")
            await interaction.response.send_message(view=view, ephemeral=True)
            return False

        return True

    async def on_pause_resume(self, interaction: discord.Interaction):
        vc = self.player.voice_client
        if not vc:
            return await interaction.response.send_message("Бот не в голосовом канале.", ephemeral=True)

        if vc.is_playing():
            vc.pause()
        elif vc.is_paused():
            vc.resume()

        pause_emoji = EMOJIS['play'] if (vc and vc.is_paused()) else EMOJIS['pause']
        self.pause_resume_btn.emoji = discord.PartialEmoji.from_str(pause_emoji)
        self.action_user = interaction.user
        self.build_ui()
        await interaction.response.edit_message(view=self)

    async def on_prev(self, interaction: discord.Interaction):
        if len(self.player.history) > 1:
            self.player.last_action_user = interaction.user
            curr = self.player.history.pop()
            prev = self.player.history.pop()
            self.player.queue.insert(0, curr)
            self.player.queue.insert(0, prev)
            self.player.voice_client.stop()
            await interaction.response.defer()
        else:
            await interaction.response.send_message("Нет предыдущего трека.", ephemeral=True)

    async def on_next(self, interaction: discord.Interaction):
        vc = self.player.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            self.player.last_action_user = interaction.user
            vc.stop()
            await interaction.response.defer()
        else:
            await interaction.response.send_message("Очередь пуста.", ephemeral=True)

    async def on_stop(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.player.stop_current(interaction.user)

    async def on_queue(self, interaction: discord.Interaction):
        curr = self.player.current
        if not curr:
            return await interaction.response.send_message("Сейчас ничего не играет.", ephemeral=True)

        elapsed = time.time() - (curr.start_time or time.time())
        left = max(0, curr.duration - elapsed)

        desc = (
            f"### ▶ Now Playing\n"
            f"[{curr.title}]({curr.url}) by [{curr.artist}]({curr.url}) [{format_time(left)} осталось]\n\n"
        )

        queue_icon = EMOJIS['queue']
        if self.player.queue:
            desc += f"### {queue_icon} Up Next\n"
            total_sec = sum(t.duration for t in self.player.queue)
            for i, tr in enumerate(self.player.queue[:10], start=1):
                desc += f"**{i}.** [{tr.title}]({tr.url}) by [{tr.artist}]({tr.url}) [{format_time(tr.duration)}]\n"
            desc += f"\n-# Page 1/1 • Tracks in queue: {len(self.player.queue)} • Length: {format_time(total_sec)}"
        else:
            desc += "-# Очередь пуста."

        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_color=EMBED_COLOR)
        container.add_item(discord.ui.TextDisplay(desc))
        view.add_item(container)
        await interaction.response.send_message(view=view, ephemeral=True)

class MusicPlayer:
    def __init__(self, bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.text_channel: discord.TextChannel | None = None  # Чат вызова команды
        self.queue = []
        self.history = []
        self.current: Track | None = None
        self.current_message: discord.Message | None = None
        self.voice_client: discord.VoiceClient | None = None
        self.inactivity_task: asyncio.Task | None = None
        self.empty_queue_task: asyncio.Task | None = None
        self.watchdog_task: asyncio.Task | None = None
        self.is_destroying: bool = False
        self.manual_stopped: bool = False
        self.last_action_user: discord.User | discord.Member | None = None

    def get_human_count(self) -> int:
        """Точный подсчет пользователей-людей в текущем голосовом канале через voice_states"""
        try:
            vc = self.voice_client or self.guild.voice_client
            if not vc or not vc.channel:
                return 0
            channel = vc.channel
            count = 0
            bot_user_id = self.bot.user.id if self.bot.user else None
            for member_id in channel.voice_states.keys():
                if bot_user_id and member_id == bot_user_id:
                    continue
                member = self.guild.get_member(member_id) or self.bot.get_user(member_id)
                if member and member.bot:
                    continue
                count += 1
            return count
        except Exception as e:
            print(f"Ошибка в get_human_count: {e}")
            return 0

    def check_channel_empty(self):
        """Проверяет наличие людей в голосовом канале и управляет таймерами выхода"""
        vc = self.voice_client or self.guild.voice_client
        if not vc or not vc.is_connected() or not vc.channel:
            return

        humans = self.get_human_count()
        if humans == 0:
            # Никого нет в канале -> отменяем таймер пустой очереди и запускаем 10-секундный таймер выхода
            self.cancel_empty_queue_timer()
            self.start_inactivity_timer()
        else:
            # Люди в канале -> отменяем таймер неактивности
            self.cancel_inactivity_timer()
            # Если очередь пуста и ничего не играет -> запускаем таймер пустой очереди на 20 секунд
            is_playing = vc.is_playing() or vc.is_paused()
            if not is_playing and not self.current and not self.queue:
                self.start_empty_queue_timer()
            else:
                self.cancel_empty_queue_timer()

    def start_watchdog(self):
        if self.watchdog_task and not self.watchdog_task.done():
            return

        async def watchdog_loop():
            try:
                while True:
                    await asyncio.sleep(2)
                    vc = self.voice_client or self.guild.voice_client
                    if not vc or not vc.is_connected():
                        break
                    self.check_channel_empty()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"Ошибка в watchdog: {e}")

        self.watchdog_task = asyncio.create_task(watchdog_loop())

    def cancel_watchdog(self):
        if self.watchdog_task and not self.watchdog_task.done():
            if asyncio.current_task() != self.watchdog_task:
                self.watchdog_task.cancel()
        self.watchdog_task = None

    def start_inactivity_timer(self):
        if self.inactivity_task and not self.inactivity_task.done():
            return

        async def leave_after_delay():
            try:
                await asyncio.sleep(INACTIVITY_TIMEOUT)
                vc = self.voice_client or self.guild.voice_client
                if vc and vc.is_connected() and self.get_human_count() == 0:
                    asyncio.create_task(self.destroy(reason_text="Destroyed the player and left the voice channel due to inactivity"))
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"Ошибка в inactivity_timer: {e}")

        self.inactivity_task = asyncio.create_task(leave_after_delay())

    def cancel_inactivity_timer(self):
        if self.inactivity_task and not self.inactivity_task.done():
            if asyncio.current_task() != self.inactivity_task:
                self.inactivity_task.cancel()
        self.inactivity_task = None

    def start_empty_queue_timer(self):
        # Если в канале никого нет, должен работать таймер неактивности
        if self.get_human_count() == 0:
            self.cancel_empty_queue_timer()
            self.start_inactivity_timer()
            return

        if self.empty_queue_task and not self.empty_queue_task.done():
            return

        async def empty_queue_delay():
            try:
                await asyncio.sleep(EMPTY_QUEUE_TIMEOUT)
                vc = self.voice_client or self.guild.voice_client
                if vc and vc.is_connected() and not self.current and not self.queue:
                    if self.get_human_count() == 0:
                        reason = "Destroyed the player and left the voice channel due to inactivity"
                    else:
                        reason = "Left the voice channel due to empty queue"
                    asyncio.create_task(self.destroy(reason_text=reason))
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"Ошибка в empty_queue_timer: {e}")

        self.empty_queue_task = asyncio.create_task(empty_queue_delay())

    def cancel_empty_queue_timer(self):
        if self.empty_queue_task and not self.empty_queue_task.done():
            if asyncio.current_task() != self.empty_queue_task:
                self.empty_queue_task.cancel()
        self.empty_queue_task = None

    async def play_next(self):
        action_user = self.last_action_user
        self.last_action_user = None

        if not self.queue:
            self.current = None
            if self.current_message:
                try:
                    await self.current_message.delete()
                except Exception:
                    pass
                self.current_message = None

            if self.text_channel:
                try:
                    footer = f"-# Action by {action_user.mention}" if action_user else None
                    await self.text_channel.send(view=StatusMessageView("### Reached the end of the queue. Please queue some track(s) again!", footer=footer), allowed_mentions=discord.AllowedMentions.none())
                except Exception:
                    pass

            # Проверяем состояние канала (запустит 10с если никого нет, либо 20с если в ГС есть люди)
            self.check_channel_empty()
            return

        # Если есть следующий трек — отменяем таймер пустой очереди
        self.cancel_empty_queue_timer()

        self.current = self.queue.pop(0)
        self.current.start_time = time.time()
        self.history.append(self.current)

        source = discord.FFmpegPCMAudio(self.current.stream_url, **FFMPEG_OPTIONS)
        self.voice_client.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(self.on_track_end(), self.bot.loop)
        )
        await self.send_now_playing(action_user=action_user)
        # Проверяем, есть ли люди в голосовом канале
        self.check_channel_empty()

    async def on_track_end(self):
        if self.manual_stopped:
            self.manual_stopped = False
            return
        if self.is_destroying:
            return
        vc = self.voice_client or self.guild.voice_client
        if not vc or not vc.is_connected():
            return
        await self.play_next()

    async def send_now_playing(self, action_user: discord.User | discord.Member | None = None):
        if self.current_message:
            try:
                await self.current_message.delete()
            except Exception:
                pass
            self.current_message = None

        view = PlayerControlView(self, action_user=action_user)
        if self.text_channel:
            self.current_message = await self.text_channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())

    async def stop_current(self, user: discord.User | discord.Member | None = None):
        """Остановка воспроизведения пользователем через кнопку Стоп: трек сразу останавливается, бот сразу выходит"""
        footer = f"-# Action by {user.mention}" if user else None
        await self.destroy(reason_text="Left the voice channel", footer_text=footer)

    async def destroy(self, reason_text: str, footer_text: str | None = None):
        """Полная остановка плеера и выход из голосового канала"""
        if self.is_destroying:
            return
        self.is_destroying = True

        try:
            self.cancel_watchdog()
            self.cancel_inactivity_timer()
            self.cancel_empty_queue_timer()
            self.queue.clear()
            self.history.clear()
            self.current = None
            self.last_action_user = None

            # 1. Сначала удаляем панель управления треком
            if self.current_message:
                try:
                    await self.current_message.delete()
                except Exception:
                    pass
                self.current_message = None

            # 2. Мгновенно отправляем пользователям статус выхода
            if self.text_channel and reason_text:
                try:
                    await self.text_channel.send(view=StatusMessageView(f"### {reason_text}", footer=footer_text), allowed_mentions=discord.AllowedMentions.none())
                except Exception:
                    pass

            # 3. Отключаемся от голосового канала
            vc = self.voice_client or self.guild.voice_client
            if vc:
                if vc.is_playing() or vc.is_paused():
                    try:
                        self.manual_stopped = True
                        vc.stop()
                    except Exception:
                        pass
                if vc.is_connected():
                    try:
                        await asyncio.wait_for(vc.disconnect(force=True), timeout=2.0)
                    except Exception:
                        try:
                            vc.cleanup()
                        except Exception:
                            pass
            self.voice_client = None
        except Exception as e:
            print(f"Ошибка в destroy: {e}")
        finally:
            self.is_destroying = False

players = {}

def get_player(guild: discord.Guild) -> MusicPlayer:
    if guild.id not in players:
        players[guild.id] = MusicPlayer(bot, guild)
    return players[guild.id]

async def find_best_youtube_stream(query: str, target_artist: str = '', target_title: str = '', target_duration: int = 0) -> tuple[str, int]:
    """Ищет наилучший студийный аудиопоток с умной фильтрацией (отсеивая Live, концерты и каверы) с максимальной скоростью"""
    loop = asyncio.get_running_loop()
    clean_artist = target_artist.strip()
    clean_title = target_title.strip()
    search_term = f"{clean_artist} - {clean_title}".strip() if (clean_artist and clean_title) else query

    # Быстрый плоский поиск кандидатов на YouTube (один легкий запрос ~0.3 сек)
    yt_query = f"ytsearch10:{search_term}"
    try:
        data = await loop.run_in_executor(None, lambda: ytdl_search.extract_info(yt_query, download=False))
        entries = data.get('entries', []) if data else []

        if entries:
            def yt_score(e):
                if not e:
                    return -999
                score = 0
                title_lower = (e.get('title') or '').lower()
                uploader_lower = (e.get('uploader') or e.get('channel') or parse_entry_artist(e)).lower()
                orig_title_lower = clean_title.lower()

                # Официальный канал лейбла / исполнителя (- Topic или Official/Vevo)
                if '- topic' in uploader_lower:
                    score += 120
                elif 'official' in uploader_lower or 'vevo' in uploader_lower:
                    score += 50

                if 'official audio' in title_lower:
                    score += 50
                elif 'audio' in title_lower:
                    score += 25

                # Жесткий штраф за живые концерты, каверы, караоке, реакции
                negative_keywords = ['live', 'concert', 'cover', 'fancam', 'reaction', 'tour', 'karaoke', 'bass cover', 'guitar cover', 'drum cover', 'live at']
                for kw in negative_keywords:
                    if kw in title_lower and kw not in orig_title_lower:
                        score -= 150

                # Точное совпадение по длительности с оригиналом Spotify
                cand_dur = parse_entry_duration(e)
                if target_duration and cand_dur:
                    diff = abs(cand_dur - target_duration)
                    if diff <= 2:
                        score += 70
                    elif diff <= 5:
                        score += 40
                    elif diff <= 10:
                        score += 15
                    elif diff > 20:
                        score -= 80

                return score

            best_entry = max(entries, key=yt_score)
            best_id = best_entry.get('id')
            best_url = best_entry.get('url') or (f"https://www.youtube.com/watch?v={best_id}" if best_id else query)
            track_data = await loop.run_in_executor(None, lambda: ytdl.extract_info(best_url, download=False))
            track_data = extract_first_entry(track_data)
            if track_data:
                stream_url = track_data.get('url')
                if not stream_url and track_data.get('formats'):
                    audio_fmts = [f for f in track_data['formats'] if f.get('url') and (f.get('acodec') != 'none' or f.get('vcodec') == 'none')]
                    if audio_fmts:
                        stream_url = audio_fmts[-1]['url']
                if stream_url and stream_url.startswith(('http://', 'https://')):
                    dur = track_data.get('duration') or best_entry.get('duration') or target_duration
                    return stream_url, dur
    except Exception as e:
        print(f"Ошибка умного поиска YouTube: {e}")

    # Fallback
    try:
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
        data = extract_first_entry(data)
        stream_url = data.get('url')
        if not stream_url and data.get('formats'):
            audio_fmts = [f for f in data['formats'] if f.get('url') and (f.get('acodec') != 'none' or f.get('vcodec') == 'none')]
            if audio_fmts:
                stream_url = audio_fmts[-1]['url']
        if stream_url and stream_url.startswith(('http://', 'https://')):
            return stream_url, data.get('duration') or target_duration
    except Exception as e2:
        print(f"Ошибка fallback поиска YouTube: {e2}")

    raise RuntimeError("YouTube заблокировал запрос (Sign in to confirm you’re not a bot). Пожалуйста, добавьте cookies.txt на сервере.")

async def play_track_logic(requester: discord.Member, guild: discord.Guild, text_channel, link: str, send_error, send_queued):
    cross_emoji = EMOJIS['cross']

    user_channel = requester.voice.channel
    player = get_player(guild)
    player.text_channel = text_channel

    # Подключение к голосовому каналу
    if not guild.voice_client:
        player.voice_client = await user_channel.connect()
    else:
        player.voice_client = guild.voice_client
        if player.voice_client.channel != user_channel:
            await player.voice_client.move_to(user_channel)

    player.start_watchdog()

    # Отменяем таймеры выхода, если добавляется трек
    player.cancel_empty_queue_timer()
    player.cancel_inactivity_timer()

    search_query = link
    orig_url = link
    orig_title = None
    orig_artist = None

    match = re.search(r"spotify\.com/track/([a-zA-Z0-9]+)", link)
    if match:
        if not sp:
            await send_error(ErrorMessageView(f"### {cross_emoji} Spotify API не настроен! Укажите SPOTIFY_CLIENT_ID и SPOTIFY_CLIENT_SECRET в файле .env"))
            return

        sp_id = match.group(1)
        try:
            sp_track = sp.track(sp_id)
            orig_artist = sp_track['artists'][0]['name'] if sp_track.get('artists') else 'Unknown artist'
            orig_title = sp_track.get('name', 'Unknown title')
            orig_duration = int(sp_track.get('duration_ms', 0) / 1000)
            orig_url = f"https://open.spotify.com/track/{sp_id}"
        except Exception as e:
            await send_error(ErrorMessageView(f"### {cross_emoji} Ошибка Spotify API: {e}"))
            return

        try:
            stream_url, duration = await find_best_youtube_stream(
                query=f"{orig_artist} - {orig_title}",
                target_artist=orig_artist,
                target_title=orig_title,
                target_duration=orig_duration
            )
            extracted_title = orig_title
            extracted_artist = orig_artist
        except Exception as e:
            await send_error(ErrorMessageView(f"### {cross_emoji} Не удалось найти аудиопоток: {e}"))
            return
    else:
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(search_query, download=False))
            data = extract_first_entry(data)

            duration = data.get('duration') or 0
            stream_url = data.get('url')
            if not stream_url and data.get('formats'):
                audio_fmts = [f for f in data['formats'] if f.get('url') and (f.get('acodec') != 'none' or f.get('vcodec') == 'none')]
                if audio_fmts:
                    stream_url = audio_fmts[-1]['url']
            stream_url = stream_url or link

            # Если длительность не определена (например, прямая ссылка на MP3/аудио), зондируем через ffprobe/ffmpeg
            probe_res = {}
            if not duration or duration == 0:
                probe_res = await loop.run_in_executor(None, lambda: probe_stream_metadata(stream_url))
                if probe_res.get('duration'):
                    duration = probe_res['duration']
                if not orig_title and probe_res.get('title'):
                    orig_title = probe_res['title']
                if not orig_artist and probe_res.get('artist'):
                    orig_artist = probe_res['artist']

            # Если название всё ещё не определено красиво (или равно ссылке / Unknown), берём имя файла из URL
            extracted_title = orig_title or data.get('title')
            if not extracted_title or extracted_title in ('Unknown title', 'watch', link):
                path = urllib.parse.urlparse(link).path
                fname = os.path.basename(path)
                if fname:
                    clean_fname = urllib.parse.unquote(fname)
                    extracted_title = os.path.splitext(clean_fname)[0]

            extracted_artist = orig_artist or data.get('uploader')
            if not extracted_artist or extracted_artist == 'Unknown artist':
                if probe_res.get('artist'):
                    extracted_artist = probe_res['artist']
                else:
                    extracted_artist = 'Unknown artist'
        except Exception as e:
            await send_error(ErrorMessageView(f"### {cross_emoji} Не удалось извлечь трек: {e}"))
            return

    if duration and duration > MAX_DURATION:
        max_dur_str = f"{MAX_DURATION // 60} мин." if MAX_DURATION >= 60 else f"{MAX_DURATION} сек."
        await send_error(ErrorMessageView(f"### {cross_emoji} Трек длится больше {max_dur_str}!"))
        return

    track_obj = Track(
        title=extracted_title if extracted_title else 'Unknown title',
        artist=extracted_artist if extracted_artist else 'Unknown artist',
        duration=duration,
        url=orig_url,
        stream_url=stream_url,
        requester=requester
    )

    is_playing = player.voice_client.is_playing() or player.voice_client.is_paused()
    pos = len(player.queue) + 1

    player.queue.append(track_obj)
    await send_queued(QueuedMessageView(track_obj, pos))

    if not is_playing:
        await player.play_next()

    # Сразу проверяем, остался ли кто-то в голосовом канале (на случай если ливнули сразу)
    player.check_channel_empty()

@bot.tree.command(name="play", description="Воспроизвести музыку по ссылке или поисковому запросу")
@app_commands.describe(link="Ссылка (Spotify, YouTube, SoundCloud, MP3) или название")
async def play_command(interaction: discord.Interaction, link: str):
    cross_emoji = EMOJIS['cross']

    if not interaction.user.voice or not interaction.user.voice.channel:
        view = ErrorMessageView(f"### {cross_emoji} Please join a voice channel, or rejoin if you are in one")
        await interaction.response.send_message(view=view, ephemeral=True)
        return

    await interaction.response.defer()

    async def send_error(view):
        await interaction.followup.send(view=view, ephemeral=True)

    async def send_queued(view):
        await interaction.followup.send(view=view)

    await play_track_logic(interaction.user, interaction.guild, interaction.channel, link, send_error, send_queued)

@bot.command(name="play")
async def play_text_command(ctx: commands.Context, *, link: str = ""):
    cross_emoji = EMOJIS['cross']
    clean_link = link.strip()
    if clean_link.lower().startswith("link:"):
        clean_link = clean_link[5:].strip()
    if not clean_link:
        view = ErrorMessageView(f"### {cross_emoji} Укажите ссылку или название трека.")
        await ctx.send(view=view)
        return

    if not ctx.author.voice or not ctx.author.voice.channel:
        view = ErrorMessageView(f"### {cross_emoji} Please join a voice channel, or rejoin if you are in one")
        await ctx.send(view=view)
        return

    async def send_error(view):
        await ctx.send(view=view)

    async def send_queued(view):
        await ctx.send(view=view)

    await play_track_logic(ctx.author, ctx.guild, ctx.channel, clean_link, send_error, send_queued)

async def search_tracks(query: str, platform: str = "youtube") -> list:
    loop = asyncio.get_running_loop()
    results = []

    if platform == "spotify":
        if not sp:
            return []
        try:
            data = await loop.run_in_executor(None, lambda: sp.search(q=query, type='track', limit=10))
            items = data.get('tracks', {}).get('items', []) if data else []
            for item in items:
                artist = item['artists'][0]['name'] if item.get('artists') else 'Unknown artist'
                title = item.get('name', 'Unknown title')
                duration = int(item.get('duration_ms', 0) / 1000)
                url = item.get('external_urls', {}).get('spotify') or f"https://open.spotify.com/track/{item.get('id')}"
                results.append({
                    'title': title,
                    'artist': artist,
                    'duration': duration,
                    'url': url,
                    'stream_url': None
                })
        except Exception as e:
            print(f"Ошибка поиска Spotify: {e}")
        return results

    if platform == "youtube":
        search_q = f"ytsearch15:{query}"
        try:
            data = await loop.run_in_executor(None, lambda: ytdl_search.extract_info(search_q, download=False))
            entries = data.get('entries', []) if data else []

            q_lower = query.lower()
            wants_live = 'live' in q_lower or 'concert' in q_lower or 'концерт' in q_lower
            wants_cover = 'cover' in q_lower or 'кавер' in q_lower

            scored_entries = []
            for entry in entries:
                if not entry:
                    continue
                # Исключаем плейлисты, миксы и вкладки из результатов поиска отдельных треков
                if entry.get('_type') == 'playlist' or entry.get('ie_key') == 'YoutubeTab':
                    continue
                url_chk = entry.get('url') or entry.get('webpage_url') or ''
                if '/playlist?list=' in url_chk:
                    continue

                title = entry.get('title') or ''
                uploader = parse_entry_artist(entry)
                duration = parse_entry_duration(entry)
                t_lower = title.lower()
                u_lower = uploader.lower()

                score = 0
                if '- topic' in u_lower:
                    score += 100
                elif 'official' in u_lower or 'vevo' in u_lower:
                    score += 40

                if 'official audio' in t_lower or 'original' in t_lower:
                    score += 50
                elif 'audio' in t_lower:
                    score += 20

                if not wants_live:
                    for kw in ['live', 'concert', 'tour', 'концерт', 'лайв', 'fancam', 'live at']:
                        if kw in t_lower:
                            score -= 150
                            break

                if not wants_cover:
                    for kw in ['cover', 'кавер', 'karaoke', 'караоке', 'reaction', 'реакция', 'guitar cover', 'drum cover', 'bass cover']:
                        if kw in t_lower:
                            score -= 100
                            break

                scored_entries.append((score, entry))

            # Сортируем: наверх поднимаются студийные и официальные треки
            scored_entries.sort(key=lambda x: x[0], reverse=True)

            def make_track_dict(e):
                t = e.get('title') or 'Unknown title'
                a = parse_entry_artist(e)
                d = parse_entry_duration(e)
                vid_id = e.get('id')
                u = e.get('webpage_url') or e.get('url')
                if not u and vid_id:
                    u = f"https://www.youtube.com/watch?v={vid_id}"
                return {
                    'title': t,
                    'artist': a,
                    'duration': d,
                    'url': u or '',
                    'stream_url': None
                }

            # 1-й проход: добавляем треки со скором >= 0 (студийные, официальные, нормальные)
            for score, entry in scored_entries:
                if score < 0:
                    continue
                results.append(make_track_dict(entry))
                if len(results) >= 10:
                    break

            # 2-й проход: если нормальных результатов не набралось (< 5), добираем оставшиеся
            if len(results) < 5:
                for score, entry in scored_entries:
                    if score < 0:
                        results.append(make_track_dict(entry))
                        if len(results) >= 10:
                            break
        except Exception as e:
            print(f"Ошибка поиска YouTube: {e}")

        return results[:10]

    prefix = "scsearch10:" if platform == "soundcloud" else "ytsearch10:"
    search_q = f"{prefix}{query}"
    try:
        data = await loop.run_in_executor(None, lambda: ytdl_search.extract_info(search_q, download=False))
        entries = data.get('entries', []) if data else []
        for entry in entries:
            if not entry:
                continue
            title = entry.get('title') or 'Unknown title'
            artist = parse_entry_artist(entry)
            duration = parse_entry_duration(entry)
            url = entry.get('webpage_url') or entry.get('url') or entry.get('permalink_url') or ''
            results.append({
                'title': title,
                'artist': artist,
                'duration': duration,
                'url': url,
                'stream_url': None
            })
    except Exception as e:
        print(f"Ошибка поиска ({platform}): {e}")

    return results[:10]

class SearchResultButton(discord.ui.Button):
    def __init__(self, index: int, search_view: 'SearchView'):
        tick_emoji = None
        try:
            tick_emoji = discord.PartialEmoji.from_str(EMOJIS.get('tick', '<:MusicBot_tick:1423564541974941736>'))
        except Exception:
            tick_emoji = "✓"

        super().__init__(
            style=discord.ButtonStyle.primary,
            emoji=tick_emoji,
            custom_id=f"search_track_{index}"
        )
        self.index = index
        self.search_view = search_view

    async def callback(self, interaction: discord.Interaction):
        await self.search_view.on_select_track(interaction, self.index)

class SearchView(discord.ui.LayoutView):
    def __init__(self, query: str, platform: str, results: list, author: discord.User | discord.Member):
        super().__init__(timeout=30.0)
        self.query = query
        self.platform = platform
        self.results = results
        self.author = author
        self.cache = {platform: results}
        self.message: discord.Message | None = None
        self.build_ui()

    def build_ui(self):
        self.clear_items()
        container = discord.ui.Container(accent_color=EMBED_COLOR)

        count = len(self.results)
        container.add_item(discord.ui.TextDisplay(f"### There are {count} results"))
        container.add_item(discord.ui.Separator())

        for i, track in enumerate(self.results[:10]):
            title = track.get('title', 'Unknown title')
            artist = track.get('artist', 'Unknown artist')
            duration = track.get('duration', 0)
            text_desc = f"### {title}\n-# Duration: {format_time(duration)}  •  Author: {artist}"
            btn = SearchResultButton(i, self)
            section = discord.ui.Section(discord.ui.TextDisplay(text_desc), accessory=btn)
            container.add_item(section)

        # Разделитель перед блоком управления
        container.add_item(discord.ui.Separator())

        # Ряд кнопок выбора сервиса (после Separator, но до Cancel)
        platform_row = discord.ui.ActionRow()

        yt_style = discord.ButtonStyle.success if self.platform == "youtube" else discord.ButtonStyle.secondary
        yt_emoji = None
        if 'youtube' in EMOJIS and EMOJIS['youtube']:
            try:
                yt_emoji = discord.PartialEmoji.from_str(EMOJIS['youtube'])
            except Exception:
                pass
        self.yt_btn = discord.ui.Button(
            style=yt_style,
            label="YouTube",
            emoji=yt_emoji,
            custom_id="search_platform_yt"
        )
        self.yt_btn.callback = self.on_yt_click
        platform_row.add_item(self.yt_btn)

        sp_style = discord.ButtonStyle.success if self.platform == "spotify" else discord.ButtonStyle.secondary
        sp_emoji = None
        if 'spotify' in EMOJIS and EMOJIS['spotify']:
            try:
                sp_emoji = discord.PartialEmoji.from_str(EMOJIS['spotify'])
            except Exception:
                pass

        self.sp_btn = discord.ui.Button(
            style=sp_style,
            label="Spotify",
            emoji=sp_emoji,
            disabled=(sp is None),
            custom_id="search_platform_sp"
        )
        self.sp_btn.callback = self.on_sp_click
        platform_row.add_item(self.sp_btn)

        sc_style = discord.ButtonStyle.success if self.platform == "soundcloud" else discord.ButtonStyle.secondary
        sc_emoji = None
        if 'soundcloud' in EMOJIS and EMOJIS['soundcloud']:
            try:
                sc_emoji = discord.PartialEmoji.from_str(EMOJIS['soundcloud'])
            except Exception:
                pass
        self.sc_btn = discord.ui.Button(
            style=sc_style,
            label="SoundCloud",
            emoji=sc_emoji,
            custom_id="search_platform_sc"
        )
        self.sc_btn.callback = self.on_sc_click
        platform_row.add_item(self.sc_btn)

        container.add_item(platform_row)

        # Отдельный ряд с кнопкой Cancel (ниже кнопок сервисов)
        cancel_row = discord.ui.ActionRow()

        cancel_emoji = None
        try:
            cancel_emoji = discord.PartialEmoji.from_str(EMOJIS.get('cross', '<:MusicBot_cross:1423564527584280677>'))
        except Exception:
            cancel_emoji = "❌"

        self.cancel_btn = discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="Cancel",
            emoji=cancel_emoji,
            custom_id="search_cancel"
        )
        self.cancel_btn.callback = self.on_cancel
        cancel_row.add_item(self.cancel_btn)

        container.add_item(cancel_row)
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if db_blacklist.is_blacklisted(interaction.guild_id, interaction.user.id):
            try:
                await interaction.response.defer()
            except Exception:
                pass
            return False
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("Только автор команды может использовать эти кнопки.", ephemeral=True)
            return False
        return True

    async def on_cancel(self, interaction: discord.Interaction):
        try:
            await interaction.message.delete()
        except Exception:
            pass

    async def on_yt_click(self, interaction: discord.Interaction):
        await self.on_switch_platform(interaction, "youtube")

    async def on_sp_click(self, interaction: discord.Interaction):
        await self.on_switch_platform(interaction, "spotify")

    async def on_sc_click(self, interaction: discord.Interaction):
        await self.on_switch_platform(interaction, "soundcloud")

    async def on_switch_platform(self, interaction: discord.Interaction, new_platform: str):
        if self.platform == new_platform:
            await interaction.response.defer()
            return

        if new_platform == "spotify" and not sp:
            await interaction.response.send_message("Spotify API не настроен.", ephemeral=True)
            return

        await interaction.response.defer()
        self.platform = new_platform

        # Получаем из кэша либо делаем запрос и кэшируем
        if new_platform in self.cache:
            self.results = self.cache[new_platform]
        else:
            new_results = await search_tracks(self.query, self.platform)
            self.cache[new_platform] = new_results
            self.results = new_results

        self.build_ui()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                await interaction.edit_original_response(view=self)
        else:
            await interaction.edit_original_response(view=self)

    async def on_select_track(self, interaction: discord.Interaction, index: int):
        if index >= len(self.results):
            return

        cross_emoji = EMOJIS.get('cross', '')
        if not interaction.user.voice or not interaction.user.voice.channel:
            view = ErrorMessageView(f"### {cross_emoji} Please join a voice channel, or rejoin if you are in one")
            await interaction.response.send_message(view=view, ephemeral=True)
            return

        await interaction.response.defer()

        try:
            await interaction.message.delete()
        except Exception:
            pass

        selected = self.results[index]
        user_channel = interaction.user.voice.channel
        player = get_player(interaction.guild)
        player.text_channel = interaction.channel

        if not interaction.guild.voice_client:
            player.voice_client = await user_channel.connect()
        else:
            player.voice_client = interaction.guild.voice_client
            if player.voice_client.channel != user_channel:
                await player.voice_client.move_to(user_channel)

        player.start_watchdog()
        player.cancel_empty_queue_timer()
        player.cancel_inactivity_timer()

        track_url = selected['url']
        track_title = selected['title']
        track_artist = selected['artist']
        duration = selected['duration']
        stream_url = selected.get('stream_url')

        loop = asyncio.get_running_loop()
        try:
            if self.platform == "spotify":
                stream_url, duration = await find_best_youtube_stream(
                    query=f"{track_artist} - {track_title}",
                    target_artist=track_artist,
                    target_title=track_title,
                    target_duration=duration
                )
            elif not stream_url:
                data = await loop.run_in_executor(None, lambda: ytdl.extract_info(track_url, download=False))
                data = extract_first_entry(data)
                stream_url = data.get('url')
                if not stream_url and data.get('formats'):
                    audio_fmts = [f for f in data['formats'] if f.get('url') and (f.get('acodec') != 'none' or f.get('vcodec') == 'none')]
                    if audio_fmts:
                        stream_url = audio_fmts[-1]['url']

                if not stream_url:
                    # Фоллбэк: ищем трек по исполнителю и названию напрямую через ytsearch1
                    fb_q = f"ytsearch1:{track_artist} {track_title}"
                    fb_data = await loop.run_in_executor(None, lambda: ytdl.extract_info(fb_q, download=False))
                    fb_data = extract_first_entry(fb_data)
                    stream_url = fb_data.get('url')
                    if not stream_url and fb_data.get('formats'):
                        audio_fmts = [f for f in fb_data['formats'] if f.get('url') and (f.get('acodec') != 'none' or f.get('vcodec') == 'none')]
                        if audio_fmts:
                            stream_url = audio_fmts[-1]['url']

                stream_url = stream_url or track_url
                if not duration:
                    duration = data.get('duration') or 0
                if not duration or duration == 0:
                    probe_res = await loop.run_in_executor(None, lambda: probe_stream_metadata(stream_url))
                    if probe_res.get('duration'):
                        duration = probe_res['duration']

            if duration and duration > MAX_DURATION:
                max_dur_str = f"{MAX_DURATION // 60} мин." if MAX_DURATION >= 60 else f"{MAX_DURATION} сек."
                await interaction.followup.send(view=ErrorMessageView(f"### {cross_emoji} Трек длится больше {max_dur_str}!"), ephemeral=True)
                return

            track_obj = Track(
                title=track_title,
                artist=track_artist,
                duration=duration,
                url=track_url,
                stream_url=stream_url,
                requester=interaction.user
            )
        except Exception as e:
            traceback.print_exc()
            await interaction.channel.send(view=ErrorMessageView(f"### {cross_emoji} Не удалось воспроизвести выбранный трек: {e}"), allowed_mentions=discord.AllowedMentions.none())
            return

        is_playing = player.voice_client.is_playing() or player.voice_client.is_paused()
        pos = len(player.queue) + 1
        player.queue.append(track_obj)

        await interaction.channel.send(view=QueuedMessageView(track_obj, pos), allowed_mentions=discord.AllowedMentions.none())

        if not is_playing:
            await player.play_next()

        player.check_channel_empty()

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.delete()
            except Exception:
                pass

@bot.tree.command(name="search", description="Поиск музыки по названию")
@app_commands.describe(
    input="Название трека или поисковый запрос",
    source="Сервис для поиска"
)
@app_commands.choices(source=[
    app_commands.Choice(name="YouTube", value="youtube"),
    app_commands.Choice(name="Spotify", value="spotify"),
    app_commands.Choice(name="SoundCloud", value="soundcloud"),
])
async def search_command(interaction: discord.Interaction, input: str, source: str = "youtube"):
    cross_emoji = EMOJIS.get('cross', '')
    if not interaction.user.voice or not interaction.user.voice.channel:
        view = ErrorMessageView(f"### {cross_emoji} Please join a voice channel, or rejoin if you are in one")
        await interaction.response.send_message(view=view, ephemeral=True)
        return

    await interaction.response.defer()

    results = await search_tracks(input, source)
    if not results:
        view = ErrorMessageView(f"### {cross_emoji} Ничего не найдено по запросу `{input}`.")
        await interaction.followup.send(view=view)
        return

    view = SearchView(query=input, platform=source, results=results, author=interaction.user)
    msg = await interaction.followup.send(view=view, allowed_mentions=discord.AllowedMentions.none())
    view.message = msg

@bot.command(name="search")
async def search_text_command(ctx: commands.Context, *, args: str = ""):
    cross_emoji = EMOJIS.get('cross', '')
    if not ctx.author.voice or not ctx.author.voice.channel:
        view = ErrorMessageView(f"### {cross_emoji} Please join a voice channel, or rejoin if you are in one")
        await ctx.send(view=view)
        return

    query = args.strip()
    source = "youtube"

    if "source:" in query.lower():
        parts = re.split(r"source:", query, flags=re.IGNORECASE)
        query = parts[0].strip()
        s_val = parts[1].strip().lower()
        if "spot" in s_val:
            source = "spotify"
        elif "sound" in s_val:
            source = "soundcloud"
        elif "you" in s_val:
            source = "youtube"

    if query.lower().startswith("input:"):
        query = query[6:].strip()

    if not query:
        view = ErrorMessageView(f"### {cross_emoji} Укажите поисковый запрос.")
        await ctx.send(view=view)
        return

    results = await search_tracks(query, source)
    if not results:
        view = ErrorMessageView(f"### {cross_emoji} Ничего не найдено по запросу `{query}`.")
        await ctx.send(view=view)
        return

    view = SearchView(query=query, platform=source, results=results, author=ctx.author)
    msg = await ctx.send(view=view, allowed_mentions=discord.AllowedMentions.none())
    view.message = msg

@bot.tree.interaction_check
async def global_tree_interaction_check(interaction: discord.Interaction) -> bool:
    if not interaction.guild_id:
        return True

    # Команда /bl доступна для проверки собственного статуса
    if interaction.command and interaction.command.name == "bl":
        return True

    if db_blacklist.is_blacklisted(interaction.guild_id, interaction.user.id):
        return False

    return True

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return
    traceback.print_exc()

@bot.check
async def global_command_check(ctx: commands.Context) -> bool:
    if ctx.guild and db_blacklist.is_blacklisted(ctx.guild.id, ctx.author.id):
        try:
            await ctx.message.delete()
        except Exception:
            pass
        return False
    return True

@bot.tree.command(name="bl", description="Управление чёрным списком сервера")
@app_commands.describe(
    action="add (a) — добавить, remove (r) — удалить, list — список",
    user="Пользователь, которого нужно добавить, удалить или проверить"
)
@app_commands.choices(action=[
    app_commands.Choice(name="add (a) — Добавить в чёрный список", value="add"),
    app_commands.Choice(name="remove (r) — Удалить из чёрного списка", value="remove"),
    app_commands.Choice(name="list — Список заблокированных на сервере", value="list"),
])
async def bl_slash_command(
    interaction: discord.Interaction,
    action: str | None = None,
    user: discord.Member | None = None
):
    if not interaction.guild:
        await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
        return

    cross_emoji = EMOJIS.get('cross', '')
    tick_emoji = EMOJIS.get('tick', '')

    is_admin = is_admin_or_manager(interaction.user)
    act = (action or "").strip().lower()

    # 1. Просмотр списка или проверка статуса
    if not act or act == "list":
        if user is not None:
            if not is_admin and user.id != interaction.user.id:
                view = ErrorMessageView(f"### {cross_emoji} У вас нет прав для просмотра статуса других пользователей.")
                await interaction.response.send_message(view=view, ephemeral=True)
                return

            if db_blacklist.is_blacklisted(interaction.guild_id, user.id):
                view = ErrorMessageView(f"### {cross_emoji} Пользователь {user.mention} **находится** в чёрном списке этого сервера.")
            else:
                view = StatusMessageView(f"### {tick_emoji} Пользователь {user.mention} **не находится** в чёрном списке этого сервера.")
            await interaction.response.send_message(view=view, ephemeral=True)
            return

        if is_admin:
            blacklisted = db_blacklist.get_blacklisted_details(interaction.guild_id)
            if not blacklisted:
                view = StatusMessageView(f"### {tick_emoji} Чёрный список на этом сервере пуст.")
                await interaction.response.send_message(view=view, ephemeral=True)
                return

            desc = f"### Чёрный список сервера ({len(blacklisted)}):\n\n"
            for item in blacklisted:
                added_by_str = f" • Добавил: <@{item['added_by']}>" if item.get('added_by') else ""
                desc += f"• <@{item['user_id']}> (`{item['user_id']}`){added_by_str}\n"

            view = StatusMessageView(desc)
            await interaction.response.send_message(view=view, ephemeral=True)
            return
        else:
            if db_blacklist.is_blacklisted(interaction.guild_id, interaction.user.id):
                view = ErrorMessageView(f"### {cross_emoji} Вы **находитесь** в чёрном списке этого сервера.")
            else:
                view = StatusMessageView(f"### {tick_emoji} Вы **не находитесь** в чёрном списке этого сервера.")
            await interaction.response.send_message(view=view, ephemeral=True)
            return

    # 2. Добавление в чёрный список (add / a)
    if act in ("add", "a"):
        if not is_admin:
            view = ErrorMessageView(f"### {cross_emoji} У вас нет прав для управления чёрным списком (требуются права Администратора или Управление сервером).")
            await interaction.response.send_message(view=view, ephemeral=True)
            return

        if not user:
            view = ErrorMessageView(f"### {cross_emoji} Укажите пользователя, которого хотите добавить в чёрный список.")
            await interaction.response.send_message(view=view, ephemeral=True)
            return

        if user.id == bot.user.id:
            view = ErrorMessageView(f"### {cross_emoji} Нельзя добавить бота в чёрный список.")
            await interaction.response.send_message(view=view, ephemeral=True)
            return

        if user.id == interaction.guild.owner_id:
            view = ErrorMessageView(f"### {cross_emoji} Нельзя добавить создателя сервера в чёрный список.")
            await interaction.response.send_message(view=view, ephemeral=True)
            return

        if db_blacklist.is_blacklisted(interaction.guild_id, user.id):
            view = ErrorMessageView(f"### {cross_emoji} Пользователь {user.mention} уже находится в чёрном списке.")
            await interaction.response.send_message(view=view, ephemeral=True)
            return

        db_blacklist.add(interaction.guild_id, user.id, added_by=interaction.user.id)
        view = StatusMessageView(f"### {tick_emoji} Пользователь {user.mention} успешно добавлен в чёрный список.")
        await interaction.response.send_message(view=view, ephemeral=True)
        return

    # 3. Удаление из чёрного списка (remove / r)
    if act in ("remove", "r"):
        if not is_admin:
            view = ErrorMessageView(f"### {cross_emoji} У вас нет прав для управления чёрным списком (требуются права Администратора или Управление сервером).")
            await interaction.response.send_message(view=view, ephemeral=True)
            return

        if not user:
            view = ErrorMessageView(f"### {cross_emoji} Укажите пользователя, которого хотите удалить из чёрного списка.")
            await interaction.response.send_message(view=view, ephemeral=True)
            return

        if not db_blacklist.is_blacklisted(interaction.guild_id, user.id):
            view = ErrorMessageView(f"### {cross_emoji} Пользователя {user.mention} нет в чёрном списке.")
            await interaction.response.send_message(view=view, ephemeral=True)
            return

        db_blacklist.remove(interaction.guild_id, user.id)
        view = StatusMessageView(f"### {tick_emoji} Пользователь {user.mention} успешно удален из чёрного списка.")
        await interaction.response.send_message(view=view, ephemeral=True)
        return

@bot.command(name="bl")
async def bl_prefix_command(ctx: commands.Context, action: str | None = None, user: discord.Member | None = None):
    if not ctx.guild:
        return

    cross_emoji = EMOJIS.get('cross', '')
    tick_emoji = EMOJIS.get('tick', '')
    is_admin = is_admin_or_manager(ctx.author)

    act = (action or "").strip().lower()

    if action and not user:
        match = re.match(r"<@!?(\d+)>", action)
        if match:
            target_id = int(match.group(1))
            user = ctx.guild.get_member(target_id)
            act = "check"

    if not act or act in ("list", "check"):
        if user is not None:
            if not is_admin and user.id != ctx.author.id:
                return await ctx.send(view=ErrorMessageView(f"### {cross_emoji} У вас нет прав для просмотра статуса других пользователей."))
            if db_blacklist.is_blacklisted(ctx.guild.id, user.id):
                return await ctx.send(view=ErrorMessageView(f"### {cross_emoji} Пользователь {user.mention} **находится** в чёрном списке этого сервера."))
            else:
                return await ctx.send(view=StatusMessageView(f"### {tick_emoji} Пользователь {user.mention} **не находится** в чёрном списке этого сервера."))

        if is_admin:
            blacklisted = db_blacklist.get_blacklisted_details(ctx.guild.id)
            if not blacklisted:
                return await ctx.send(view=StatusMessageView(f"### {tick_emoji} Чёрный список на этом сервере пуст."))
            desc = f"### Чёрный список сервера ({len(blacklisted)}):\n\n"
            for item in blacklisted:
                added_by_str = f" • Добавил: <@{item['added_by']}>" if item.get('added_by') else ""
                desc += f"• <@{item['user_id']}> (`{item['user_id']}`){added_by_str}\n"
            return await ctx.send(view=StatusMessageView(desc))
        else:
            if db_blacklist.is_blacklisted(ctx.guild.id, ctx.author.id):
                return await ctx.send(view=ErrorMessageView(f"### {cross_emoji} Вы **находитесь** в чёрном списке этого сервера."))
            else:
                return await ctx.send(view=StatusMessageView(f"### {tick_emoji} Вы **не находитесь** в чёрном списке этого сервера."))

    if act in ("add", "a"):
        if not is_admin:
            return await ctx.send(view=ErrorMessageView(f"### {cross_emoji} У вас нет прав для управления чёрным списком."))
        if not user:
            return await ctx.send(view=ErrorMessageView(f"### {cross_emoji} Укажите пользователя: `!bl add @пользователь`"))
        if user.id == bot.user.id:
            return await ctx.send(view=ErrorMessageView(f"### {cross_emoji} Нельзя добавить бота в чёрный список."))
        if user.id == ctx.guild.owner_id:
            return await ctx.send(view=ErrorMessageView(f"### {cross_emoji} Нельзя добавить создателя сервера в чёрный список."))
        if db_blacklist.is_blacklisted(ctx.guild.id, user.id):
            return await ctx.send(view=ErrorMessageView(f"### {cross_emoji} Пользователь {user.mention} уже находится в чёрном списке."))

        db_blacklist.add(ctx.guild.id, user.id, added_by=ctx.author.id)
        return await ctx.send(view=StatusMessageView(f"### {tick_emoji} Пользователь {user.mention} успешно добавлен в чёрный список."))

    if act in ("remove", "r"):
        if not is_admin:
            return await ctx.send(view=ErrorMessageView(f"### {cross_emoji} У вас нет прав для управления чёрным списком."))
        if not user:
            return await ctx.send(view=ErrorMessageView(f"### {cross_emoji} Укажите пользователя: `!bl remove @пользователь`"))
        if not db_blacklist.is_blacklisted(ctx.guild.id, user.id):
            return await ctx.send(view=ErrorMessageView(f"### {cross_emoji} Пользователя {user.mention} нет в чёрном списке."))

        db_blacklist.remove(ctx.guild.id, user.id)
        return await ctx.send(view=StatusMessageView(f"### {tick_emoji} Пользователь {user.mention} успешно удален из чёрного списка."))

@bot.event
async def on_voice_state_update(member, before, after):
    try:
        # Если событие касается самого бота
        if bot.user and member.id == bot.user.id:
            if before.channel and not after.channel:
                player = get_player(member.guild)
                if player.is_destroying:
                    return
                player.cancel_watchdog()
                player.cancel_inactivity_timer()
                player.cancel_empty_queue_timer()
                player.voice_client = None
                if player.current_message:
                    try:
                        await player.current_message.delete()
                    except Exception:
                        pass
                    player.current_message = None
            return

        vc = member.guild.voice_client
        if not vc or not vc.channel:
            return

        bot_chan_id = vc.channel.id
        before_chan_id = before.channel.id if before.channel else None
        after_chan_id = after.channel.id if after.channel else None

        # Если действие произошло в том же голосовом канале, где находится бот
        if before_chan_id == bot_chan_id or after_chan_id == bot_chan_id:
            player = get_player(member.guild)
            player.check_channel_empty()
    except Exception as e:
        print(f"Ошибка в on_voice_state_update: {e}")

bot_initialized = False

@bot.event
async def on_ready():
    global bot_initialized
    if not bot_initialized:
        bot_initialized = True
        await ensure_emojis()
        print(f"Бот {bot.user} запущен и готов к работе!")

        async def sync_commands():
            try:
                print("Синхронизация слэш-команд с Discord...")
                # Очищаем команды гильдий, чтобы Discord не дублировал их с глобальными
                for guild in bot.guilds:
                    try:
                        bot.tree.clear_commands(guild=guild)
                        await bot.tree.sync(guild=guild)
                    except Exception as ge:
                        print(f"Не удалось очистить команды гильдии для {guild.name}: {ge}")

                synced = await bot.tree.sync()
                print(f"Глобальные слэш-команды успешно синхронизированы ({len(synced)} шт.): {[c.name for c in synced]}")
                for cmd in synced:
                    opts = [opt.name for opt in cmd.options] if hasattr(cmd, 'options') else []
                    print(f"  • /{cmd.name} (параметры: {opts})")
            except Exception as e:
                print(f"Ошибка при синхронизации команд: {e}")

        asyncio.create_task(sync_commands())
    else:
        print(f"Бот {bot.user} переподключился к Discord.")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
        return
    print(f"Ошибка команды: {error}")

if not DISCORD_TOKEN or DISCORD_TOKEN in ("ВАШ_ТОКЕН_DISCORD", "your_discord_bot_token_here"):
    print("Ошибка: DISCORD_TOKEN не задан! Пожалуйста, укажите токен в файле .env")
    exit(1)

bot.run(DISCORD_TOKEN)