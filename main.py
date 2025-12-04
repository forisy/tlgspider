import os
import json
import asyncio
import re
import time
import signal
import sys
import logging
import psutil
import argparse
import requests
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import MessageMediaDocument, DocumentAttributeFilename
from telethon.tl.functions.messages import GetDialogsRequest
from telethon.tl.types import InputPeerEmpty
from tqdm import tqdm
from asyncio import Semaphore
from collections import deque
from mutagen.id3 import ID3NoHeaderError
from mutagen.flac import FLAC
from mutagen import File

# 配置常量
DISABLE_TQDM = os.getenv('TGDL_DISABLE_TQDM', 'false').lower() == 'true'
DATA_DIR = os.getenv('TGDL_DATA_DIR', './data')
CONFIG_DIR = os.path.join(DATA_DIR, 'config')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
CHANNELS_FILE = os.path.join(CONFIG_DIR, 'channels.txt')
SESSION_DIR = os.path.join(CONFIG_DIR, 'sessions')
MEDIA_DIR = os.path.join(DATA_DIR, 'downloads')

# 配置时区（支持环境变量配置）
TIMEZONE = os.getenv('TZ', 'Asia/Shanghai')
os.environ['TZ'] = TIMEZONE
try:
    time.tzset()
except AttributeError:
    # Windows系统不支持tzset，使用time.localtime
    pass

# 配置日志
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

log_file_handler = logging.FileHandler('telegram_downloader.log', encoding='utf-8')
log_file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
log_file_handler.formatter.converter = time.localtime
logger.addHandler(log_file_handler)

if DISABLE_TQDM:
    class PrintHandler(logging.Handler):
        def emit(self, record):
            print(self.format(record), flush=True)
    print_handler = PrintHandler()
    print_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    print_handler.formatter.converter = time.localtime
    logger.addHandler(print_handler)
else:
    class TqdmHandler(logging.Handler):
        def emit(self, record):
            try:
                tqdm.write(self.format(record))
            except Exception:
                self.handleError(record)
    # 控制台 tqdm 兼容输出
    tqdm_handler = TqdmHandler()
    tqdm_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    tqdm_handler.formatter.converter = time.localtime
    logger.addHandler(tqdm_handler)

# 初始化目录
for directory in [DATA_DIR, CONFIG_DIR, SESSION_DIR, MEDIA_DIR]:
    os.makedirs(directory, exist_ok=True)
    logger.debug(f'确保目录存在: {directory}')

# 全局状态
stop_event = asyncio.Event()

def fmtWithUnits(value: float | None, unit: str = '') -> str:
    """格式化数值为带单位的字符串"""
    if value is None:
        return 'N/A'
    if unit == 'MB':
        return f'{value / (1024 * 1024):.2f} MB'
    elif unit == 'kbps':
        return f'{value:.2f} kbps'
    elif unit == 's':
        return f'{value:.2f} s'
    else:
        return str(value)

class ConfigManager:
    @staticmethod
    def load_config() -> dict:
        logger.info('加载配置文件')
        if not os.path.exists(CONFIG_FILE):
            logger.info('配置文件不存在，开始初始化配置')
            config = ConfigManager._create_initial_config()
        else:
            logger.info('读取现有配置文件')
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            # 添加新的配置项（如果不存在）
            if 'audio_quality_check' not in config:
                config['audio_quality_check'] = {
                    'enabled': False,
                    'check_type': 'size',  # 'size', 'duration' 或 'both'
                    'min_size_mb': 1,  # 最小文件大小（MB）
                    'min_duration_seconds': 0  # 最小音频时长（秒）
                }
            # 添加下载参数配置（如果不存在）
            if 'download_settings' not in config:
                config['download_settings'] = {
                    'max_file_size_mb': int(os.getenv('TGDL_MAX_FILE_SIZE_MB', '500')),
                    'min_file_size_mb': int(os.getenv('TGDL_MIN_FILE_SIZE_MB', '0')),
                    'wait_interval_seconds': int(os.getenv('TGDL_WAIT_INTERVAL_SECONDS', '300')),
                    'initial_retry_delay': int(os.getenv('TGDL_INITIAL_RETRY_DELAY', '1')),
                    'max_retry_delay': int(os.getenv('TGDL_MAX_RETRY_DELAY', '1800')),
                    'max_retries': int(os.getenv('TGDL_MAX_RETRIES', '0')),
                    'max_concurrent_downloads': int(os.getenv('TGDL_MAX_CONCURRENT_DOWNLOADS', '3')),
                    'batch_size': int(os.getenv('TGDL_BATCH_SIZE', '15')),
                    'progress_step': int(os.getenv('TGDL_PROGRESS_STEP', '10')),
                    'exclude_patterns': os.getenv('TGDL_EXCLUDE_PATTERNS', '').split(',') if os.getenv('TGDL_EXCLUDE_PATTERNS') else [],
                    'downloading_dir': os.getenv('TGDL_DOWNLOADING_DIR', os.path.join(MEDIA_DIR, 'downloading')),
                    'completed_dir': os.getenv('TGDL_COMPLETED_DIR', os.path.join(MEDIA_DIR, 'completed'))
                }
            # 添加语言过滤配置（如果不存在）
            if 'language_filter' not in config:
                config['language_filter'] = {
                    'enabled': False,
                    'languages': ['cn', 'zh'],  # 要下载的语言列表，例如 ['cn', 'zh', 'en']
                    'detection_threshold': 0.7  # 语言检测阈值，用于从文件名判断语言的可信度
                }
                ConfigManager.save_config(config)
            if 'link_submission' not in config:
                config['link_submission'] = {
                    'enabled': os.getenv('TGDL_LINK_SUBMIT_ENABLED', '0').lower() in ('1', 'true', 'yes'),
                    'api_url': os.getenv('TGDL_LINK_SUBMIT_API_URL', '')
                }
                ConfigManager.save_config(config)
        return config

    @staticmethod
    def _create_initial_config() -> dict:
        print("[首次配置] 请填写以下信息：")
        config = {
            'api_id': int(input("API ID: ")),
            'api_hash': input("API HASH: "),
            'phone_number': input("Phone Number: "),
            'media_types': input("下载哪些类型（逗号分隔 video,audio,document）: ").split(','),
            'proxy': {
                'enabled': input("是否使用代理(yes/no): ").lower() == 'yes',
                'type': input("代理类型(socks5/http/mtproxy): ") if input("是否使用代理(yes/no): ").lower() == 'yes' else '',
                'host': input("代理主机: ") if input("是否使用代理(yes/no): ").lower() == 'yes' else '',
                'port': int(input("代理端口: ")) if input("是否使用代理(yes/no): ").lower() == 'yes' else 0,
                'username': input("代理用户名(可选，直接回车跳过): ") or None if input("是否使用代理(yes/no): ").lower() == 'yes' else None,
                'password': input("代理密码(可选，直接回车跳过): ") or None if input("是否使用代理(yes/no): ").lower() == 'yes' else None
            },
            'selected_channels': [],
            'audio_quality_check': {
                'enabled': input("是否启用音频质量检查(yes/no): ").lower() == 'yes',
                'check_type': input("质量检查方式(size/duration/both): ") if input("是否启用音频质量检查(yes/no): ").lower() == 'yes' else 'size',
                'min_size_mb': float(input("最小文件大小(MB): ")) if input("是否启用音频质量检查(yes/no): ").lower() == 'yes' else 1,
                'min_duration_seconds': float(input("最小音频时长(秒): ")) if input("是否启用音频质量检查(yes/no): ").lower() == 'yes' else 0
            },
            'download_settings': {
                'max_file_size_mb': int(os.getenv('TGDL_MAX_FILE_SIZE_MB', '500')),
                'min_file_size_mb': int(os.getenv('TGDL_MIN_FILE_SIZE_MB', '0')),
                'wait_interval_seconds': int(os.getenv('TGDL_WAIT_INTERVAL_SECONDS', '300')),
                'initial_retry_delay': int(os.getenv('TGDL_INITIAL_RETRY_DELAY', '1')),
                'max_retry_delay': int(os.getenv('TGDL_MAX_RETRY_DELAY', '1800')),
                'max_retries': int(os.getenv('TGDL_MAX_RETRIES', '0')),
                'max_concurrent_downloads': int(os.getenv('TGDL_MAX_CONCURRENT_DOWNLOADS', '3')),
                'batch_size': int(os.getenv('TGDL_BATCH_SIZE', '15')),
                'progress_step': int(os.getenv('TGDL_PROGRESS_STEP', '10')),
                'exclude_patterns': os.getenv('TGDL_EXCLUDE_PATTERNS', '').split(',') if os.getenv('TGDL_EXCLUDE_PATTERNS') else [],
                'downloading_dir': os.getenv('TGDL_DOWNLOADING_DIR', os.path.join(MEDIA_DIR, 'downloading')),
                'completed_dir': os.getenv('TGDL_COMPLETED_DIR', os.path.join(MEDIA_DIR, 'completed')),
                'min_disk_space_mb': int(os.getenv('TGDL_MIN_DISK_SPACE_MB', '500'))
            },
            'language_filter': {
                'enabled': input("是否启用语言过滤(yes/no): ").lower() == 'yes',
                'languages': input("要下载的语言列表（逗号分隔 cn,zh,en）: ").split(',') if input("是否启用语言过滤(yes/no): ").lower() == 'yes' else ['cn', 'zh'],
                'detection_threshold': float(input("语言检测阈值(0-1): ")) if input("是否启用语言过滤(yes/no): ").lower() == 'yes' else 0.7
            },
            'link_submission': {
                'enabled': os.getenv('TGDL_LINK_SUBMIT_ENABLED', '0').lower() in ('1', 'true', 'yes'),
                'api_url': os.getenv('TGDL_LINK_SUBMIT_API_URL', '')
            },
        }
        logger.info('创建新的配置文件')
        ConfigManager.save_config(config)
        return config

    @staticmethod
    def save_config(config: dict) -> None:
        logger.debug('保存配置文件')
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

    @staticmethod
    def get_proxy_config(config: dict) -> dict:
        proxy = None
        if config.get('proxy', {}).get('enabled', False):
            proxy_config = config['proxy']
            if not proxy_config.get('host') and not proxy_config.get('port'):
                proxy_host = os.getenv('HTTPS_PROXY') or os.getenv('HTTP_PROXY') or os.getenv('ALL_PROXY')
                if proxy_host:
                    # 解析代理URL
                    if proxy_host.startswith('socks5://'):
                        proxy_type = 'socks5'
                        proxy_host = proxy_host[9:]
                    elif proxy_host.startswith('http://'):
                        proxy_type = 'http'
                        proxy_host = proxy_host[7:]
                    else:
                        proxy_type = 'http'
                    # 解析代理地址和端口
                    if '@' in proxy_host:
                        auth, proxy_host = proxy_host.split('@')
                        username, password = auth.split(':')
                    else:
                        username = password = None
                    host, port = proxy_host.split(':')
                    port = int(port)
                    proxy_config.update({
                        'type': proxy_type,
                        'host': host,
                        'port': port,
                        'username': username,
                        'password': password
                    })
                    logger.info(f'从环境变量读取代理配置: {proxy_type}://{host}:{port}')
            if proxy_config['type'] == 'socks5':
                proxy = {
                    'proxy_type': 'socks5',
                    'addr': proxy_config['host'],
                    'port': proxy_config['port'],
                    'username': proxy_config['username'],
                    'password': proxy_config['password']
                }
            elif proxy_config['type'] == 'http':
                proxy = {
                    'proxy_type': 'http',
                    'addr': proxy_config['host'],
                    'port': proxy_config['port'],
                    'username': proxy_config['username'],
                    'password': proxy_config['password']
                }
            elif proxy_config['type'] == 'mtproxy':
                proxy = {
                    'proxy_type': 'mtproxy',
                    'addr': proxy_config['host'],
                    'port': proxy_config['port'],
                    'secret': proxy_config.get('password')
                }
            logger.info(f'使用{proxy_config["type"]}代理: {proxy_config["host"]}:{proxy_config["port"]}')
        return proxy

    @staticmethod
    def get_download_settings(config: dict) -> dict:
        """获取下载设置，优先使用配置文件中的值，如果不存在则使用环境变量，最后使用默认值"""
        download_settings = config.get('download_settings', {})
        raw = os.getenv('TGDL_EXCLUDE_PATTERNS', None)
        patterns = [p.strip() for p in raw.split(',')] if raw else [p for p in download_settings.get('exclude_patterns', [])]
        patterns = [p for p in patterns if p]
        return {
            'max_file_size_mb': int(os.getenv('TGDL_MAX_FILE_SIZE_MB', str(download_settings.get('max_file_size_mb', 500)))) ,
            'min_file_size_mb': int(os.getenv('TGDL_MIN_FILE_SIZE_MB', str(download_settings.get('min_file_size_mb', 0)))) ,
            'wait_interval_seconds': int(os.getenv('TGDL_WAIT_INTERVAL_SECONDS', str(download_settings.get('wait_interval_seconds', 300)))) ,
            'initial_retry_delay': int(os.getenv('TGDL_INITIAL_RETRY_DELAY', str(download_settings.get('initial_retry_delay', 1)))) ,
            'max_retry_delay': int(os.getenv('TGDL_MAX_RETRY_DELAY', str(download_settings.get('max_retry_delay', 1800)))) ,
            'max_retries': int(os.getenv('TGDL_MAX_RETRIES', str(download_settings.get('max_retries', 0)))) ,
            'max_concurrent_downloads': int(os.getenv('TGDL_MAX_CONCURRENT_DOWNLOADS', str(download_settings.get('max_concurrent_downloads', 3)))) ,
            'batch_size': int(os.getenv('TGDL_BATCH_SIZE', str(download_settings.get('batch_size', 15)))) ,
            'progress_step': int(os.getenv('TGDL_PROGRESS_STEP', str(download_settings.get('progress_step', 10)))) ,
            'exclude_patterns': patterns,
            'downloading_dir': os.getenv('TGDL_DOWNLOADING_DIR', download_settings.get('downloading_dir', os.path.join(MEDIA_DIR, 'downloading'))),
            'completed_dir': os.getenv('TGDL_COMPLETED_DIR', download_settings.get('completed_dir', os.path.join(MEDIA_DIR, 'completed'))),
            'min_disk_space_mb': int(os.getenv('TGDL_MIN_DISK_SPACE_MB', str(download_settings.get('min_disk_space_mb', 500))))
        }

class StateManager:
    """用于持久化每个频道的 last_id，避免重复处理已处理消息"""
    STATE_FILE = os.path.join(CONFIG_DIR, 'state.json')

    @staticmethod
    def _ensure_state_file() -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        if not os.path.exists(StateManager.STATE_FILE):
            try:
                with open(StateManager.STATE_FILE, 'w', encoding='utf-8') as f:
                    json.dump({'channels': {}}, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.error(f'创建状态文件失败: {e}')

    @staticmethod
    def load_state() -> dict:
        StateManager._ensure_state_file()
        try:
            with open(StateManager.STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f'读取状态文件失败，使用空状态: {e}')
            return {'channels': {}}

    @staticmethod
    def get_last_id(channel_id: int) -> int:
        state = StateManager.load_state()
        try:
            return int(state.get('channels', {}).get(str(channel_id), {}).get('last_id', 0))
        except Exception:
            return 0

    @staticmethod
    def set_last_id(channel_id: int, last_id: int) -> None:
        StateManager._ensure_state_file()
        state = StateManager.load_state()
        channels = state.setdefault('channels', {})
        ch = channels.setdefault(str(channel_id), {})
        ch['last_id'] = int(last_id)
        try:
            with open(StateManager.STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f'写入状态文件失败: {e}')

class FileManager:
    @staticmethod
    def sanitize_filename(name: str) -> str:
        return re.sub(r'[^\w\-_. ]', '_', name)
        
    @staticmethod
    def check_disk_space(path: str, min_space_mb: int = 500) -> bool:
        """检查指定路径所在磁盘的可用空间是否足够
        
        Args:
            path: 要检查的路径
            min_space_mb: 最小可用空间（MB）
            
        Returns:
            bool: 如果可用空间大于等于最小要求，返回True；否则返回False
        """
        try:
            if not os.path.exists(path):
                # 如果路径不存在，检查其父目录
                path = os.path.dirname(path)
                if not os.path.exists(path):
                    # 如果父目录也不存在，使用当前目录
                    path = '.'
            
            # 获取磁盘可用空间（以字节为单位）
            free_space = psutil.disk_usage(path).free
            free_space_mb = free_space / (1024 * 1024)  # 转换为MB
            
            logger.info(f'磁盘可用空间: {free_space_mb:.2f}MB, 最小要求: {min_space_mb}MB')
            return free_space_mb >= min_space_mb
        except Exception as e:
            logger.error(f'检查磁盘空间时出错: {e}')
            # 出错时返回True，避免因检查失败而停止下载
            return True

    @staticmethod
    def get_filepath(msg, channel_title: str) -> tuple:
        doc = msg.media.document
        mime = doc.mime_type or ''
        filename = None
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                filename = attr.file_name
                break
        if not filename:
            filename = f"{mime.replace('/', '_')}"
        safe_name = FileManager.sanitize_filename(f"{filename}")
        tmp_name = FileManager.sanitize_filename(f"{msg.id}_{filename}")
        
        # 获取下载设置
        config = ConfigManager.load_config()
        download_settings = ConfigManager.get_download_settings(config)
        
        # 确保目录存在
        downloading_dir = download_settings.get('downloading_dir', os.path.join(MEDIA_DIR, 'downloading'))
        completed_dir = download_settings.get('completed_dir', os.path.join(MEDIA_DIR, 'completed'))
        os.makedirs(downloading_dir, exist_ok=True)
        os.makedirs(completed_dir, exist_ok=True)
        
        # 生成文件路径
        tmp_path = os.path.join(downloading_dir, tmp_name) + '.part'
        save_path = os.path.join(completed_dir, safe_name)
        logger.debug(f'生成文件路径: 临时={tmp_path}, 保存={save_path}')
        return tmp_path, tmp_name, save_path, safe_name

    @staticmethod
    def should_exclude_file(filename: str, config: dict) -> bool:
        """检查文件名是否应该被排除

        Args:
            filename: 文件名
            config: 配置字典

        Returns:
            bool: 如果文件名匹配任何排除模式则返回True
        """
        exclude_patterns = config.get('download_settings', {}).get('exclude_patterns', [])
        if not exclude_patterns:
            return False
        fname_lower = filename.lower()
        for pattern in exclude_patterns:
            p = str(pattern).strip()
            if not p:
                continue
            if p.lower().startswith('re:'):
                pat = p[3:].strip()
                try:
                    if re.search(pat, filename, flags=re.IGNORECASE):
                        logger.debug(f'文件名 {filename} 匹配排除模式 {pattern}')
                        return True
                except re.error as e:
                    logger.warning(f'排除模式 {pattern} 无效: {e}')
                    continue
            else:
                if p.lower() in fname_lower:
                    logger.debug(f'文件名 {filename} 包含关键字 {pattern}')
                    return True
        return False

    @staticmethod
    def cleanup_unfinished_files(download_settings: dict) -> int:
        downloading_dir = download_settings.get('downloading_dir', os.path.join(MEDIA_DIR, 'downloading'))
        os.makedirs(downloading_dir, exist_ok=True)
        removed = 0
        try:
            for name in os.listdir(downloading_dir):
                if name.endswith('.part'):
                    path = os.path.join(downloading_dir, name)
                    try:
                        os.remove(path)
                        removed += 1
                    except Exception as e:
                        logger.warning(f'清理未完成文件失败: {path}, 错误: {e}')
        except Exception as e:
            logger.warning(f'扫描未完成文件失败: {downloading_dir}, 错误: {e}')
        logger.info(f'启动前清理未完成文件: {removed} 个')
        return removed

class MediaValidator:
    @staticmethod
    def should_download_media(message, media_types: list, config: dict) -> bool:
        if not message.media or not isinstance(message.media, MessageMediaDocument):
            logger.debug(f'消息 {message.id} 不包含可下载的媒体')
            return False

        doc = message.media.document
        mime = doc.mime_type or ''
        
        # 获取文件名
        filename = None
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                filename = attr.file_name
                break
        if not filename:
            # filename = f"{mime.replace('/', '_')}"
            return False  # 如果没有文件名，则不下载
            
        # 检查文件名是否应该被排除
        if FileManager.should_exclude_file(filename, config):
            logger.debug(f'消息 {message.id} 的文件名 {filename} 匹配排除模式，跳过下载')
            return False
            
        # 检查语言过滤
        language_filter = config.get('language_filter', {})
        if language_filter.get('enabled', False) and language_filter.get('languages'):
            detected_lang = LanguageDetector.detect_language(
                filename, 
                threshold=language_filter.get('detection_threshold', 0.7)
            )
            if detected_lang and detected_lang not in language_filter.get('languages', []):
                logger.debug(f'消息 {message.id} 的文件名 {filename} 检测到语言 {detected_lang}，不在允许的语言列表中，跳过下载')
                return False
            elif not detected_lang and 'unknown' not in language_filter.get('languages', []):
                logger.debug(f'消息 {message.id} 的文件名 {filename} 无法检测语言，跳过下载')
                return False

        should_download = any(
            (t == 'video' and 'video' in mime) or
            (t == 'audio' and 'audio' in mime) or
            (t == 'document' and 'application' in mime)
            for t in media_types
        )
        logger.debug(f'消息 {message.id} 媒体类型: {mime}, 是否下载: {should_download}')
        return should_download

    @staticmethod
    def check_file_size(size: int, config: dict) -> bool:
        download_settings = ConfigManager.get_download_settings(config)
        max_size = download_settings['max_file_size_mb'] * 1024 * 1024
        min_size = download_settings.get('min_file_size_mb', 0) * 1024 * 1024
        is_valid = size <= max_size and size >= min_size
        logger.debug(f'检查文件大小: {size/1024/1024:.2f}MB, 最小: {download_settings.get("min_file_size_mb", 0)}MB, 最大: {download_settings["max_file_size_mb"]}MB, 是否有效: {is_valid}')
        return is_valid

class CloudLinkProcessor:
    def find_links(self, text: str) -> list:
        return []

class BaiduPanProcessor(CloudLinkProcessor):
    def find_links(self, text: str) -> list:
        results = []
        for m in re.finditer(r'https?://pan\.baidu\.com/s/[\w-]+(?:\?[^\s]*)?', text):
            url = m.group(0)
            code = ''
            q = re.search(r'[?&]pwd=([A-Za-z0-9]{4,6})', url)
            if q:
                code = q.group(1)
            else:
                code_match = re.search(r'(提取码|密码)[\s:：]*([A-Za-z0-9]{4,6})', text)
                code = code_match.group(2) if code_match else ''
            results.append({'provider': 'baidupan', 'url': url, 'code': code})
        return results

class AliyunDriveProcessor(CloudLinkProcessor):
    def find_links(self, text: str) -> list:
        results = []
        for m in re.finditer(r'https?://(?:www\.)?(?:aliyundrive\.com|alipan\.com)/s/[\w-]+', text):
            url = m.group(0)
            code_match = re.search(r'(提取码|密码)[\s:：]*([A-Za-z0-9]{4,6})', text)
            results.append({'provider': 'aliyundrive', 'url': url, 'code': code_match.group(2) if code_match else ''})
        return results

class GoogleDriveProcessor(CloudLinkProcessor):
    def find_links(self, text: str) -> list:
        results = []
        patterns = [
            r'https?://drive\.google\.com/file/d/[^\s]+',
            r'https?://drive\.google\.com/drive/folders/[^\s]+',
            r'https?://drive\.google\.com/open\?id=[^\s]+'
        ]
        for p in patterns:
            for m in re.finditer(p, text):
                results.append({'provider': 'gdrive', 'url': m.group(0), 'code': ''})
        return results

class DropboxProcessor(CloudLinkProcessor):
    def find_links(self, text: str) -> list:
        results = []
        for m in re.finditer(r'https?://(?:www\.)?dropbox\.com/s/[^\s]+', text):
            results.append({'provider': 'dropbox', 'url': m.group(0), 'code': ''})
        return results

class OneDriveProcessor(CloudLinkProcessor):
    def find_links(self, text: str) -> list:
        results = []
        patterns = [r'https?://1drv\.ms/[^\s]+', r'https?://[^\s]*onedrive\.live\.com/[^\s]+']
        for p in patterns:
            for m in re.finditer(p, text):
                results.append({'provider': 'onedrive', 'url': m.group(0), 'code': ''})
        return results

class MegaProcessor(CloudLinkProcessor):
    def find_links(self, text: str) -> list:
        results = []
        for m in re.finditer(r'https?://mega\.nz/(?:file|folder)/[^\s]+', text):
            url = m.group(0)
            key_match = re.search(r'#([A-Za-z0-9_-]+)', url)
            results.append({'provider': 'mega', 'url': url, 'code': key_match.group(1) if key_match else ''})
        return results

class QuarkProcessor(CloudLinkProcessor):
    def find_links(self, text: str) -> list:
        results = []
        for m in re.finditer(r'https?://pan\.quark\.cn/s/[^\s]+', text):
            results.append({'provider': 'quark', 'url': m.group(0), 'code': ''})
        return results

class XunleiProcessor(CloudLinkProcessor):
    def find_links(self, text: str) -> list:
        results = []
        for m in re.finditer(r'https?://pan\.xunlei\.com/s/[^\s]+', text):
            results.append({'provider': 'xunlei', 'url': m.group(0), 'code': ''})
        for m in re.finditer(r'thunder://[^\s]+', text):
            results.append({'provider': 'xunlei', 'url': m.group(0), 'code': ''})
        return results

class UCDriveProcessor(CloudLinkProcessor):
    def find_links(self, text: str) -> list:
        results = []
        for m in re.finditer(r'https?://(?:www\.)?drive\.uc\.cn/s/[^\s]+', text):
            results.append({'provider': 'ucdrive', 'url': m.group(0), 'code': ''})
        return results

class ResourceExtractor:
    PROCESSORS = [
        BaiduPanProcessor(),
        AliyunDriveProcessor(),
        GoogleDriveProcessor(),
        DropboxProcessor(),
        OneDriveProcessor(),
        MegaProcessor(),
        QuarkProcessor(),
        XunleiProcessor(),
        UCDriveProcessor(),
    ]

    @staticmethod
    def extract_links(text: str) -> list:
        if not text:
            return []
        found = []
        for proc in ResourceExtractor.PROCESSORS:
            try:
                found.extend(proc.find_links(text))
            except Exception:
                pass
        return found

    @staticmethod
    def build_full_url(provider: str, url: str, code: str) -> str:
        try:
            if provider == 'baidupan':
                if re.search(r'[?&]pwd=', url):
                    return url
                if code:
                    return url + ('&' if '?' in url else '?') + f'pwd={code}'
                return url
            return url
        except Exception:
            return url

    @staticmethod
    def parse_bot_deeplinks(text: str) -> list:
        links = []
        if not text:
            return links
        pattern = r'https?://t\.me/([A-Za-z0-9_]+)\?start=([A-Za-z0-9_\-]+)'
        for m in re.finditer(pattern, text):
            bot = m.group(1)
            payload = m.group(2)
            parts = payload.split('_')
            action = parts[0] if parts else ''
            is_get_link = False
            if len(parts) >= 2 and parts[0] == 'get' and parts[1] == 'link':
                is_get_link = True
                parts = parts[2:]
            elif action in ('getlink', 'get_link'):
                is_get_link = True
                parts = parts[1:]
            if is_get_link and len(parts) >= 3:
                chat_id_s, msg_id_s, provider = parts[0], parts[1], parts[2]
                provider_raw = provider.lower()
                provider_map = {
                    'baidu': 'baidupan',
                    'baidupan': 'baidupan',
                    'alipan': 'aliyundrive',
                    'aliyundrive': 'aliyundrive',
                    'aliyun': 'aliyundrive',
                    'quark': 'quark',
                    'xunlei': 'xunlei',
                    'thunder': 'xunlei',
                    'uc': 'ucdrive',
                    'ucdrive': 'ucdrive',
                }
                provider = provider_map.get(provider_raw, provider_raw)
                links.append({
                    'bot': bot,
                    'action': 'get_link',
                    'chat_id': chat_id_s,
                    'message_id': msg_id_s,
                    'provider': provider,
                    'provider_raw': provider_raw
                })
            else:
                links.append({'bot': bot, 'action': 'start', 'payload': payload})
        return links

    @staticmethod
    def extract_entity_urls(msg) -> list:
        urls = []
        try:
            text = getattr(msg, 'message', '') or ''
            entities = getattr(msg, 'entities', []) or []
            for ent in entities:
                u = getattr(ent, 'url', None)
                if u:
                    urls.append(u)
                else:
                    offset = getattr(ent, 'offset', None)
                    length = getattr(ent, 'length', None)
                    if offset is not None and length is not None and text:
                        seg = text[offset:offset+length]
                        if seg:
                            urls.append(seg)
            seen = set()
            deduped = []
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    deduped.append(u)
            return deduped
        except Exception:
            return []

    @staticmethod
    def extract_from_message(msg) -> list:
        text = getattr(msg, 'message', '') or ''
        links = ResourceExtractor.extract_links(text)
        entity_urls = ResourceExtractor.extract_entity_urls(msg)
        for u in entity_urls:
            for proc in ResourceExtractor.PROCESSORS:
                try:
                    links.extend(proc.find_links(u))
                except Exception:
                    pass
        tasks = []
        for link in links:
            tasks.append({
                'kind': 'cloud_link',
                'message_id': msg.id,
                'provider': link.get('provider', ''),
                'url': link.get('url', ''),
                'code': link.get('code', ''),
                'full_url': ResourceExtractor.build_full_url(link.get('provider', ''), link.get('url', ''), link.get('code', '')),
            })
        return tasks

class MessageFormatter:
    @staticmethod
    def _summarize_text(text: str, max_len: int = 160) -> str:
        s = re.sub(r'\s+', ' ', text or '').strip()
        if len(s) > max_len:
            return s[:max_len - 1] + '…'
        return s

    @staticmethod
    def _human_size(size: int | None) -> str:
        try:
            if not size:
                return '0MB'
            return f'{size / 1024 / 1024:.2f}MB'
        except Exception:
            return str(size or 0)

    @staticmethod
    def format(msg) -> str:
        parts = []
        try:
            mid = getattr(msg, 'id', None)
            dt = getattr(msg, 'date', None)
            dt_str = ''
            if dt:
                try:
                    dt_str = dt.astimezone().strftime('%Y-%m-%d %H:%M:%S')
                except Exception:
                    dt_str = str(dt)
            parts.append(f'#{mid} {dt_str}')
            text = getattr(msg, 'message', '') or ''
            if text:
                parts.append(f'text="{MessageFormatter._summarize_text(text)}"')
            if getattr(msg, 'media', None) and hasattr(msg.media, 'document'):
                doc = msg.media.document
                size = getattr(doc, 'size', 0)
                mime = getattr(doc, 'mime_type', '') or ''
                fname = ''
                try:
                    for attr in getattr(doc, 'attributes', []) or []:
                        if isinstance(attr, DocumentAttributeFilename):
                            fname = attr.file_name
                            break
                except Exception:
                    fname = ''
                parts.append(f'media={mime or "-"} name="{fname or "-"}" size={MessageFormatter._human_size(size)}')
            try:
                found_text = ResourceExtractor.extract_links(text)
                entity_urls = ResourceExtractor.extract_entity_urls(msg)
                found_entities = []
                for u in entity_urls:
                    for proc in ResourceExtractor.PROCESSORS:
                        try:
                            found_entities.extend(proc.find_links(u))
                        except Exception:
                            pass
                found = found_text + found_entities
                if found:
                    urls = []
                    for l in found[:3]:
                        u = l.get('url') or ''
                        urls.append(u)
                    more = '' if len(found) <= 3 else f' (+{len(found)-3})'
                    parts.append('links=' + ','.join(urls) + more)
            except Exception:
                pass
            try:
                dls_text = ResourceExtractor.parse_bot_deeplinks(text)
                entity_urls = ResourceExtractor.extract_entity_urls(msg)
                dls_entities = []
                for u in entity_urls:
                    try:
                        dls_entities.extend(ResourceExtractor.parse_bot_deeplinks(u))
                    except Exception:
                        pass
                dls = dls_text + dls_entities
                if dls:
                    items = []
                    for dl in dls[:3]:
                        p = dl.get('provider','')
                        items.append(f'{dl.get("bot","")}:{dl.get("action","")}{":"+p if p else ""}')
                    more = '' if len(dls) <= 3 else f' (+{len(dls)-3})'
                    parts.append('deeplinks=' + ','.join(items) + more)
            except Exception:
                pass
        except Exception:
            parts.append(str(getattr(msg, 'id', ''))) 
        return ' | '.join([p for p in parts if p])

class LanguageDetector:
    """用于从文件名检测语言的工具类"""
    
    # 常见语言关键词映射
    LANGUAGE_KEYWORDS = {
        'cn': ['中文', '汉语', '普通话', '国语', 'chinese', 'mandarin', 'cn', 'zh'],
        'en': ['英文', '英语', 'english', 'en'],
        'jp': ['日文', '日语', 'japanese', 'jp'],
        'kr': ['韩文', '韩语', 'korean', 'kr'],
        'fr': ['法文', '法语', 'french', 'fr'],
        'de': ['德文', '德语', 'german', 'de'],
        'es': ['西班牙文', '西班牙语', 'spanish', 'es'],
        'ru': ['俄文', '俄语', 'russian', 'ru'],
    }
    
    # 语言标记正则表达式模式
    LANGUAGE_TAG_PATTERNS = {
        'cn': [r'\[中文\]', r'\[cn\]', r'\[zh\]', r'\[chinese\]', r'【中文】', r'【中文字幕】', r'\.cn\.'],
        'en': [r'\[en\]', r'\[eng\]', r'\[english\]', r'【英文】', r'【英语】', r'\.en\.'],
        'jp': [r'\[jp\]', r'\[japanese\]', r'【日文】', r'【日语】', r'\.jp\.'],
        'kr': [r'\[kr\]', r'\[korean\]', r'【韩文】', r'【韩语】', r'\.kr\.'],
    }
    
    # 歌曲文件名常见分隔符模式
    MUSIC_FILENAME_PATTERNS = [
        r'^(.+?)\s*[-–—_]\s*(.+)$',  # 歌手 - 歌曲
        r'^(.+?)\s*[:\：]\s*(.+)$',   # 歌手: 歌曲
        r'^(.+?)\s*[\[\(【]\s*(.+?)\s*[\]\)】]',  # 歌手 [歌曲] 或 歌手 (歌曲)
    ]
    
    @staticmethod
    def detect_language(filename: str, threshold: float = 0.7) -> str:
        """
        从文件名中检测可能的语言
        
        Args:
            filename: 文件名
            threshold: 检测阈值，越高越严格
            
        Returns:
            检测到的语言代码，如果无法确定则返回空字符串
        """
        filename = filename.lower()
        
        # 1. 首先检查是否有明确的语言标记 (最高优先级)
        for lang_code, patterns in LanguageDetector.LANGUAGE_TAG_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, filename, re.IGNORECASE):
                    return lang_code
        
        # 2. 检查文件名中是否包含语言关键词
        for lang_code, keywords in LanguageDetector.LANGUAGE_KEYWORDS.items():
            for keyword in keywords:
                # 使用单词边界检查，避免部分匹配
                if re.search(r'\b' + re.escape(keyword.lower()) + r'\b', filename):
                    return lang_code
        
        # 3. 尝试分离歌手名和歌曲名，主要分析歌曲名部分
        song_title = filename
        for pattern in LanguageDetector.MUSIC_FILENAME_PATTERNS:
            match = re.match(pattern, filename)
            if match:
                # 使用第二部分（通常是歌曲名）进行语言检测
                song_title = match.group(2)
                break
        
        # 4. 如果没有明确的语言关键词，尝试通过字符集特征判断
        # 提取可能的文本部分（排除数字、特殊符号等）
        text_parts = re.findall(r'[a-zA-Z\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7a3]+', song_title)
        if not text_parts:
            return ''
            
        # 连接所有文本部分
        text = ''.join(text_parts)
        text_len = max(len(text), 1)
        
        # 计算中文字符比例
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        chinese_ratio = chinese_chars / text_len
        
        # 计算日文特有字符比例
        japanese_chars = sum(1 for c in text if ('\u3040' <= c <= '\u309f') or ('\u30a0' <= c <= '\u30ff'))
        japanese_ratio = japanese_chars / text_len
        
        # 计算韩文字符比例
        korean_chars = sum(1 for c in text if '\uac00' <= c <= '\ud7a3')
        korean_ratio = korean_chars / text_len
        
        # 使用较低的阈值，因为我们已经过滤了非文本字符
        adjusted_threshold = threshold * 0.6
        
        # 检查是否有任何语言超过阈值
        if chinese_ratio > adjusted_threshold:
            return 'cn'
        elif japanese_ratio > adjusted_threshold:
            return 'jp'
        elif korean_ratio > adjusted_threshold:
            return 'kr'
        
        # 如果文本主要是拉丁字母，假设是英文
        latin_chars = sum(1 for c in text if 'a' <= c <= 'z' or 'A' <= c <= 'Z')
        if latin_chars / text_len > 0.8:  # 如果80%以上是拉丁字母
            return 'en'
            
        # 如果没有明显特征，默认返回空字符串
        return ''

class ProgressTracker:
    def __init__(self, step: int = 10):
        self.step = step
        self.last_triggered: dict[str, int] = {}

    def check(self, safe_name: str, current: float, total: float):
        if total == 0:
            return

        percentage = (current / total) * 100
        rounded = int(percentage // self.step) * self.step

        last = self.last_triggered.get(safe_name, -1)
        if rounded != last:
            self.last_triggered[safe_name] = rounded
            current_mb = current / 1024 / 1024
            total_mb = total / 1024 / 1024
            logger.info(
                f'下载进度 {safe_name}: {rounded}% ({current_mb:.2f}/{total_mb:.2f}MB)'
            )

    def clear(self, safe_name: str):
        """在下载完成后清除该文件的记录，释放内存"""
        self.last_triggered.pop(safe_name, None)


class AudioQualityChecker:
    def __init__(self, config):
        self.config = config
        self.quality_check_config = config.get('audio_quality_check', {})

    def _get_audio_metadata(self, file_path: str) -> dict:
        """获取本地音频文件的元数据（时长和比特率）"""
        metadata = {'duration': 0, 'bitrate': 0}
        try:
            try:
                audio = FLAC(file_path)
                metadata['duration'] = audio.info.length
                metadata['bitrate'] = audio.info.length * audio.info.bits_per_sample * audio.info.sample_rate / 1000 # FLAC没有直接的bitrate，估算
            except Exception:
                # Fallback for other formats or if FLAC fails
                audio = File(file_path)
                if audio and audio.info:
                    metadata['duration'] = audio.info.length
                    if hasattr(audio.info, 'bitrate'):
                        metadata['bitrate'] = audio.info.bitrate / 1000
                    elif hasattr(audio.info, 'sample_rate') and hasattr(audio.info, 'bits_per_sample') and hasattr(audio.info, 'channels'):
                        # Estimate bitrate for formats like WAV, FLAC if not directly available
                        metadata['bitrate'] = (audio.info.sample_rate * audio.info.bits_per_sample * audio.info.channels) / 1000
                else:
                    logger.warning(f'无法获取文件 {file_path} 的元数据。')

        except ID3NoHeaderError:
            logger.warning(f'文件 {file_path} 没有ID3标签，尝试作为普通文件处理。')
            # 可以尝试其他方式获取，例如ffprobe，但这里简化处理
        except Exception as e:
            logger.error(f'获取音频元数据失败: {file_path}, 错误: {e}')
        return metadata

    def should_replace_audio(self, save_path: str, doc, size: int) -> bool:
        """检查是否需要替换现有的音频文件
        
        Args:
            save_path: 现有文件的完整路径
            doc: Telegram文档对象
            size: 新文件的大小
            
        Returns:
            bool: 是否需要替换现有文件
        """
        if not self.quality_check_config.get('enabled', False):
            return False

        existing_file_exists = os.path.exists(save_path)
        # 获取新文件的比特率和时长
        new_duration = None
        for attr in doc.attributes:
            if hasattr(attr, 'duration'):
                new_duration = attr.duration

        existing_size = 0
        existing_duration = None
        existing_bitrate = None
        if existing_file_exists:
            existing_metadata = self._get_audio_metadata(save_path)
            existing_duration = existing_metadata['duration']
            existing_bitrate = existing_metadata['bitrate']
            existing_size = os.path.getsize(save_path)

        logger.info(f"文件替换对比 {save_path}: 新: size={fmtWithUnits(size, 'MB')}, duration={fmtWithUnits(new_duration, 's')} 旧: size={fmtWithUnits(existing_size, 'MB')}, bitrate={fmtWithUnits(existing_bitrate, 'kbps')}, duration={fmtWithUnits(existing_duration, 's')}")

        check_type = self.quality_check_config.get('check_type', 'size')
        min_size = self.quality_check_config.get('min_size_mb', 1) * 1024 * 1024
        min_duration_seconds = self.quality_check_config.get('min_duration_seconds', 0)

        # 检查新文件是否满足最低要求
        if new_duration is not None and new_duration < min_duration_seconds:
            logger.info(f'新音频文件时长 不满足最低要求 {min_duration_seconds:.2f}s，跳过下载: {save_path}')
            return False
        if size < min_size:
            logger.info(f'新音频文件大小 不满足最低要求 {min_size/1024/1024:.2f}MB，跳过下载: {save_path}')
            return False

        if not existing_file_exists:
            # 如果文件不存在，且新文件满足所有最低要求，则下载
            logger.info(f'文件不存在，新音频文件满足所有最低要求，开始下载: {save_path}')
            return True

        # 比较新旧文件质量
        should_replace = False
        log_reason = ''

        if check_type == 'size':
            should_replace = size > existing_size
            log_reason = '大小更大'
        elif check_type == 'duration':
            should_replace = new_duration is not None and (existing_duration is None or new_duration > existing_duration)
            log_reason = '时长更长'
        elif check_type == 'both':
            # 综合判断：新文件在大小、比特率、时长上都优于旧文件，或者至少不差且有一项更优
            # 如果旧文件没有比特率或时长信息，则认为新文件在这方面更优
            should_replace = False
            if existing_file_exists:
                # 只有当所有指标都更好时才替换
                if size > existing_size and \
                   (new_duration is not None and (existing_duration is None or new_duration > existing_duration)):
                    should_replace = True
                    log_reason = '大小、比特率、时长都更优'
                # 或者，如果大小相同，但比特率或时长更好
                elif size == existing_size and \
                     ((new_duration is not None and (existing_duration is None or new_duration > existing_duration))):
                    should_replace = True
                    log_reason = '大小相同，但比特率或时长更优'
            else:
                # 如果旧文件不存在，则直接替换
                should_replace = True
                log_reason = '旧文件不存在'

        # 补充：如果新文件比旧文件差，则不替换
        if existing_file_exists:
            if check_type == 'size' and size <= existing_size:
                logger.info(f'新音频文件大小 不如现有文件 {existing_size/1024/1024:.2f}MB，跳过下载: {save_path}')
                return False
            if check_type == 'duration' and new_duration is not None and new_duration <= existing_duration:
                logger.info(f'新音频文件时长 不如现有文件 {existing_duration:.2f}s，跳过下载: {save_path}')
                return False
            if check_type == 'both' and not (size > existing_size and \
                                             (new_duration is not None and (existing_duration is None or new_duration > existing_duration))):
                logger.info(f'新音频文件在大小、比特率、时长上不完全优于现有文件，跳过下载: {save_path}')
                return False

        if should_replace:
            logger.info(f'新文件 {log_reason}，准备替换: {save_path}')
        else:
            logger.info(f'新文件质量不满足替换要求，跳过下载: {save_path}')
        return should_replace

class MessagePreprocessor:
    def __init__(self, client: TelegramClient, media_types: list, config: dict):
        self.client = client
        self.media_types = media_types
        self.config = config
        self.download_settings = ConfigManager.get_download_settings(config)
        # 按频道维度记录已见消息及进度，避免跨频道互相影响
        self.channel_seen_ids: dict[int, set[int]] = {}
        self.channel_seen_queues: dict[int, deque] = {}
        self.channel_last_id: dict[int, int] = {}

    async def fetch_valid_messages(self, entity) -> list:
        """
        尝试获取 batch_size 条满足下载条件的消息（媒体类型 + 文件大小）
        如果已无新消息，可能返回不足 batch_size 条
        """
        valid_resources = []
        exhausted = False
        channel_id = getattr(entity, 'id', None)
        title = getattr(entity, 'title', str(channel_id))

        if channel_id is None:
            logger.warning('无法识别频道ID，跳过本次抓取')
            return valid_resources

        # 初始化频道状态
        seen_ids = self.channel_seen_ids.setdefault(channel_id, set())
        seen_queue = self.channel_seen_queues.setdefault(channel_id, deque(maxlen=500))
        if channel_id not in self.channel_last_id:
            persisted = StateManager.get_last_id(channel_id)
            self.channel_last_id[channel_id] = persisted
        last_id = self.channel_last_id[channel_id]

        logger.info(f'频道 {title} 拉取参数: min_id={last_id}, limit={self.download_settings["batch_size"] * 2}')

        while len(valid_resources) < self.download_settings['batch_size'] and not exhausted:
            candidate_messages = []
            # 使用 min_id 获取比 last_id 更新的消息，而不是 offset_id（offset_id 会取更旧的消息）
            async for msg in self.client.iter_messages(entity, limit=self.download_settings['batch_size'] * 2, min_id=last_id):
                if msg.id in seen_ids:
                    continue
                candidate_messages.append(msg)
                seen_ids.add(msg.id)
                seen_queue.append(msg.id)
                # 当deque达到上限，预先从集合中移除最旧的ID，避免集合膨胀
                if len(seen_queue) == seen_queue.maxlen:
                    oldest_id = seen_queue[0]
                    seen_ids.discard(oldest_id)
                # 记录该频道的最新已看到消息ID（仅运行期使用，不持久化）
                new_seen = max(self.channel_last_id.get(channel_id, 0), msg.id)
                if new_seen != self.channel_last_id.get(channel_id, 0):
                    self.channel_last_id[channel_id] = new_seen
                    last_id = new_seen

            if candidate_messages:
                max_id = max((m.id for m in candidate_messages), default=last_id)
                logger.info(f'频道 {title} 候选消息 {len(candidate_messages)} 条，最高ID={max_id}，当前运行期 last_seen_id={last_id}')
            if not candidate_messages:
                exhausted = True
                logger.info(f'频道 {title} 无新消息（min_id={last_id}），结束本轮抓取')
                break

            for msg in candidate_messages:
                try:
                    logger.info(MessageFormatter.format(msg))
                except Exception:
                    pass
                if MediaValidator.should_download_media(msg, self.media_types, self.config):
                    doc = msg.media.document
                    size = getattr(doc, 'size', 0)
                    if MediaValidator.check_file_size(size, self.config):
                        valid_resources.append({'kind': 'telegram_media', 'message': msg, 'message_id': msg.id})
                        if len(valid_resources) >= self.download_settings['batch_size']:
                            break
                cloud_tasks = ResourceExtractor.extract_from_message(msg)
                for t in cloud_tasks:
                    valid_resources.append(t)
                    if len(valid_resources) >= self.download_settings['batch_size']:
                        break

                try:
                    text = getattr(msg, 'message', '') or ''
                    dls_text = ResourceExtractor.parse_bot_deeplinks(text)
                    entity_urls = ResourceExtractor.extract_entity_urls(msg)
                    dls_entities = []
                    for u in entity_urls:
                        try:
                            dls_entities.extend(ResourceExtractor.parse_bot_deeplinks(u))
                        except Exception:
                            pass
                    deeplinks = dls_text + dls_entities
                    for dl in deeplinks:
                        chat_id_s = dl.get('chat_id')
                        msg_id_s = dl.get('message_id')
                        if not chat_id_s or not msg_id_s:
                            continue
                        try:
                            ref_entity = await self.client.get_entity(int(chat_id_s))
                            ref_msg = await self.client.get_messages(ref_entity, ids=int(msg_id_s))
                            ref_text = getattr(ref_msg, 'message', '') or ''
                            ref_links = ResourceExtractor.extract_links(ref_text)
                            ref_entity_urls = ResourceExtractor.extract_entity_urls(ref_msg)
                            for u in ref_entity_urls:
                                for proc in ResourceExtractor.PROCESSORS:
                                    try:
                                        ref_links.extend(proc.find_links(u))
                                    except Exception:
                                        pass
                            for link in ref_links:
                                valid_resources.append({
                                    'kind': 'cloud_link',
                                    'message_id': msg.id,
                                    'provider': link.get('provider', ''),
                                    'url': link.get('url', ''),
                                    'code': link.get('code', ''),
                                    'full_url': ResourceExtractor.build_full_url(link.get('provider', ''), link.get('url', ''), link.get('code', '')),
                                })
                                if len(valid_resources) >= self.download_settings['batch_size']:
                                    break
                        except Exception:
                            pass
                        if len(valid_resources) >= self.download_settings['batch_size']:
                            break

                    for dl in deeplinks:
                        bot_name = dl.get('bot')
                        if not bot_name:
                            continue
                        try:
                            bot_entity = await self.client.get_entity(bot_name)
                            payload_provider = dl.get('provider_raw') or dl.get('provider') or ''
                            payload = f"{dl.get('action','get_link')}_{dl.get('chat_id','')}_{dl.get('message_id','')}"
                            if payload_provider:
                                payload = payload + f"_{payload_provider}"
                            sent_msg = await self.client.send_message(bot_entity, f"/start {payload}")
                            await asyncio.sleep(2)
                            async for reply in self.client.iter_messages(bot_entity, min_id=sent_msg.id, limit=5):
                                rtext = getattr(reply, 'message', '') or ''
                                links = ResourceExtractor.extract_links(rtext)
                                entity_urls2 = ResourceExtractor.extract_entity_urls(reply)
                                for u in entity_urls2:
                                    for proc in ResourceExtractor.PROCESSORS:
                                        try:
                                            links.extend(proc.find_links(u))
                                        except Exception:
                                            pass
                                for link in links:
                                    valid_resources.append({
                                        'kind': 'cloud_link',
                                        'message_id': msg.id,
                                        'provider': link.get('provider', ''),
                                        'url': link.get('url', ''),
                                        'code': link.get('code', ''),
                                        'full_url': ResourceExtractor.build_full_url(link.get('provider', ''), link.get('url', ''), link.get('code', '')),
                                    })
                                    if len(valid_resources) >= self.download_settings['batch_size']:
                                        break
                                if len(valid_resources) >= self.download_settings['batch_size']:
                                    break
                        except Exception:
                            pass
                except Exception:
                    pass

        return valid_resources

class TelegramDownloader:
    def __init__(self):
        logger.info('初始化 TelegramDownloader')
        self.config = ConfigManager.load_config()
        self.client = None
        self.audio_checker = AudioQualityChecker(self.config)
        self.preprocessor = None
        self.download_settings = ConfigManager.get_download_settings(self.config)
        self.progress_tracker = ProgressTracker(self.download_settings['progress_step'] if 'progress_step' in self.download_settings else 10)
        self.log_effective_runtime_config()

    def log_effective_runtime_config(self) -> None:
        safe_phone = re.sub(r'(\d{3})\d+(\d{2})', r'\1***\2', str(self.config.get('phone_number', '')))
        payload = {
            'api_id': self.config.get('api_id'),
            'api_hash': self.config.get('api_hash'),
            'phone_number': safe_phone,
            'media_types': self.config.get('media_types'),
            'audio_quality_check': self.config.get('audio_quality_check'),
            'language_filter': self.config.get('language_filter'),
            'selected_channels': self.config.get('selected_channels', []),
            'download_settings': self.download_settings,
        }
        try:
            logger.info(f"有效运行时配置: {json.dumps(payload, ensure_ascii=False)}")
        except Exception:
            logger.info(f"有效运行时配置: {payload}")

    async def initialize(self) -> None:
        logger.info('开始初始化客户端')
        safe_name = re.sub(r'[^\w\-_.]', '_', self.config['phone_number'])
        session_path = os.path.join(SESSION_DIR, safe_name)
        logger.debug(f'使用会话文件: {session_path}')

        # 准备代理配置
        proxy = ConfigManager.get_proxy_config(self.config)

        self.client = TelegramClient(
            session_path,
            self.config['api_id'],
            self.config['api_hash'],
            connection_retries=5,        # 连接重试次数
            retry_delay=1,              # 重试延迟（秒）
            auto_reconnect=True,        # 自动重连
            request_retries=5,          # 请求重试次数
            timeout=30,                 # 连接超时时间
            proxy=proxy                 # 代理配置
        )
        await self.client.connect()

        if not await self.client.is_user_authorized():
            logger.info('需要登录授权')
            await self._handle_authorization()
        else:
            logger.info('已经授权，无需登录')

        # 初始化预处理器
        self.preprocessor = MessagePreprocessor(self.client, self.config['media_types'], self.config)

    async def _handle_authorization(self) -> None:
        logger.info('开始登录流程')
        await self.client.send_code_request(self.config['phone_number'])
        code = input("请输入收到的验证码: ")
        try:
            await self.client.sign_in(self.config['phone_number'], code)
            logger.info('登录成功')
        except SessionPasswordNeededError:
            logger.info('需要二步验证')
            pwd = input("请输入二步验证密码: ")
            await self.client.sign_in(password=pwd)
            logger.info('二步验证成功')

    async def select_channels(self) -> list:
        logger.info('开始选择频道')
        result = await self.client(GetDialogsRequest(
            offset_date=None,
            offset_id=0,
            offset_peer=InputPeerEmpty(),
            limit=100,
            hash=0
        ))

        channels = []
        for dlg in result.dialogs:
            try:
                entity = await self.client.get_entity(dlg.peer)
                if hasattr(entity, 'title'):
                    channels.append(entity)
            except Exception as e:
                logger.error(f'获取频道实体失败: {e}')

        all_channels = {str(ch.id): ch.title for ch in channels}
        logger.info(f'找到 {len(all_channels)} 个频道')

        for idx, (chid, title) in enumerate(all_channels.items()):
            print(f"[{idx}] {title} ({chid})")

        choices = input("请输入要下载的频道编号（用逗号分隔）: ").split(',')
        selected = []
        id_list = list(all_channels.keys())
        for choice in choices:
            try:
                idx = int(choice.strip())
                selected.append(id_list[idx])
                logger.info(f'选择频道: {all_channels[id_list[idx]]}')
            except Exception as e:
                logger.error(f'无效的选择: {choice}, 错误: {e}')

        self.config['selected_channels'] = selected
        ConfigManager.save_config(self.config)
        return selected

    async def download_media(self, message, channel_title: str) -> bool:
        if not MediaValidator.should_download_media(message, self.config['media_types'], self.config):
            return False

        doc = message.media.document
        size = doc.size or 0
        if not MediaValidator.check_file_size(size, self.config):
            logger.warning(f'跳过大文件: {size/1024/1024:.2f}MB')
            return False

        tmp_path, tmp_name, save_path, safe_name = FileManager.get_filepath(message, channel_title)
        
        # 检查磁盘空间是否足够
        min_disk_space_mb = self.download_settings.get('min_disk_space_mb', 500)
        downloading_dir = self.download_settings.get('downloading_dir', os.path.join(MEDIA_DIR, 'downloading'))
        if not FileManager.check_disk_space(downloading_dir, min_disk_space_mb):
            logger.warning(f'磁盘空间不足 {min_disk_space_mb}MB，暂停下载: {safe_name}')
            await asyncio.sleep(self.download_settings.get('wait_interval_seconds', 300))
            return False
            
        mime = doc.mime_type or ''
        
        # 检查是否需要进行音频质量比较
        if os.path.exists(save_path) and 'audio' in mime:
            if not self.audio_checker.should_replace_audio(save_path, doc, size):
                return False
        elif os.path.exists(save_path):
            logger.info(f'文件已存在，跳过: {save_path}')
            return False

        logger.info(f'开始下载: {safe_name}, 大小: {size/1024/1024:.2f}MB')
        try:
            if DISABLE_TQDM:
                async def progress_callback(current, total):
                    self.progress_tracker.check(safe_name, current, total)
                await self.client.download_media(
                    message,
                    file=tmp_path,
                    progress_callback=progress_callback
                )
            else:
                # 使用tqdm进度条
                with tqdm(total=size, unit='B', unit_scale=True, desc=safe_name, leave=True) as progress_bar:
                    await self.client.download_media(
                        message,
                        file=tmp_path,
                        progress_callback=lambda current, _: progress_bar.update(current - progress_bar.n)
                    )
            
            # 下载完成后，将文件从下载中目录移动到下载完成目录
            os.rename(tmp_path, save_path)
            logger.info(f'下载完成: 从 {tmp_path} 移动到 {save_path}')
            return True
        except Exception as e:
            logger.error(f'下载失败: {save_path}, 错误: {e}')
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
                logger.debug(f'删除临时文件: {tmp_path}')
            return False
        finally:
            # 清理进度跟踪器
            DISABLE_TQDM and self.progress_tracker.clear(safe_name)

    async def handle_cloud_link(self, task: dict, channel_title: str) -> bool:
        try:
            provider = task.get('provider', '')
            url = task.get('url', '')
            code = task.get('code', '')
            full_url = task.get('full_url', url)
            settings = self.config.get('link_submission', {})
            if settings.get('enabled') and settings.get('api_url'):
                payload = {
                    'provider': provider,
                    'src_url': url,
                    'url': full_url,
                    'code': code,
                    'message_id': task.get('message_id'),
                    'channel_title': channel_title
                }
                def _post():
                    return requests.post(settings['api_url'], json=payload, timeout=10)
                resp = await asyncio.to_thread(_post)
                ok = 200 <= resp.status_code < 300
                if ok:
                    logger.info(f'提交云盘任务成功: {channel_title} [{provider}] {full_url}')
                else:
                    logger.error(f'提交云盘任务失败: {resp.status_code} {resp.text}')
                return ok
            else:
                logger.info(f'识别到云盘链接: {channel_title} [{provider}] {full_url} 提取码:{code or "N/A"}')
                return True
        except Exception as e:
            logger.error(f'处理云盘链接失败: {e}')
            return False

    async def _limited_download(self, sem: Semaphore, message, title: str):
        async with sem:
            ok = await self.download_media(message, title)
            return (message.id, ok)

    async def process_channel(self, channel: str) -> None:
        try:
            logger.info(f'处理频道 ID: {channel}')
            entity = await self.client.get_entity(int(channel))
            title = entity.title or channel
            logger.info(f'开始处理频道: {title}')
            retry_count = 0
            retry_delay = self.download_settings['initial_retry_delay']
            sem = Semaphore(self.download_settings['max_concurrent_downloads'])

            while not stop_event.is_set():
                try:
                    tasks = await self.preprocessor.fetch_valid_messages(entity)
                    if not tasks:
                        logger.info(f'频道 {title} 暂无新消息，等待 {self.download_settings["wait_interval_seconds"]} 秒')
                        await asyncio.sleep(self.download_settings['wait_interval_seconds'])
                        continue

                    media_tasks = [t for t in tasks if t.get('kind') == 'telegram_media']
                    link_tasks = [t for t in tasks if t.get('kind') == 'cloud_link']
                    logger.info(f'{title} 资源任务: 媒体 {len(media_tasks)} 条，云盘链接 {len(link_tasks)} 条')
                    media_jobs = [self._limited_download(sem, t['message'], title) for t in media_tasks]
                    media_results = await asyncio.gather(*media_jobs)
                    link_success_ids = []
                    for lt in link_tasks:
                        ok = await self.handle_cloud_link(lt, title)
                        if ok:
                            link_success_ids.append(lt.get('message_id'))

                    channel_id_int = int(channel)
                    success_ids = [mid for (mid, ok) in media_results if ok]
                    success_ids.extend([mid for mid in link_success_ids if mid])
                    if success_ids:
                        new_last = max(success_ids)
                        StateManager.set_last_id(channel_id_int, new_last)
                        logger.info(f'频道 {title} 成功下载推进进度: last_id -> {new_last}')

                    retry_count = 0
                    retry_delay = self.download_settings['initial_retry_delay']

                except ConnectionError as e:
                    if self.download_settings['max_retries'] > 0 and retry_count >= self.download_settings['max_retries']:
                        logger.error(f'频道 {title} 重试次数超过限制 {self.download_settings["max_retries"]} 次，停止重试')
                        break

                    retry_count += 1
                    logger.warning(f'频道 {title} 连接错误，第 {retry_count} 次重试，等待 {retry_delay} 秒: {e}')
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, self.download_settings['max_retry_delay'])

        except Exception as e:
            logger.error(f'处理频道 {channel} 时发生错误: {e}')

    async def run(self) -> None:
        logger.info('启动下载器')
        await self.initialize()

        enabled_channels = self.config.get('selected_channels', [])
        if not enabled_channels:
            logger.info('没有选择频道，开始选择频道')
            enabled_channels = await self.select_channels()

        try:
            tasks = []
            for channel in enabled_channels:
                if stop_event.is_set():
                    break
                task = asyncio.create_task(self.process_channel(channel))
                tasks.append(task)

            logger.info(f'创建了 {len(tasks)} 个下载任务')
            await asyncio.gather(*tasks)
        finally:
            await self.client.disconnect()
            logger.info('客户端已断开连接')

def handle_sigint():
    logger.info('收到中断信号')
    stop_event.set()

async def main():
    logger.info('程序启动')
    parser = argparse.ArgumentParser(description='Telegram 媒体下载器')
    parser.add_argument('-r', '--reconfigure', action='store_true', help='仅进行配置更新，不启动下载')
    parser.add_argument('-c', '--clean', action='store_true', help='启动前清理未完成文件(.part)')
    parser.add_argument('--print-config', action='store_true', help='打印有效运行时配置并退出')
    args = parser.parse_args()
    env_reconfigure = os.getenv('TGDL_RECONFIGURE', '').lower() in ('1', 'true', 'yes')
    env_clean = os.getenv('TGDL_CLEAN_ON_START', '').lower() in ('1', 'true', 'yes')
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, handle_sigint)
    except NotImplementedError:
        # Windows环境下的信号处理
        import threading
        import msvcrt
        def windows_signal_listener():
            while True:
                if msvcrt.kbhit() and msvcrt.getwch() == '\x03':  # Ctrl+C
                    handle_sigint()
                    break
        threading.Thread(target=windows_signal_listener, daemon=True).start()
        logger.debug('启动Windows信号监听器')

    downloader = TelegramDownloader()
    if args.clean or env_clean:
        try:
            FileManager.cleanup_unfinished_files(downloader.download_settings)
        except Exception as e:
            logger.warning(f'启动前清理未完成文件发生错误: {e}')
    if args.print_config:
        return
    if args.reconfigure or env_reconfigure:
        await downloader.initialize()
        await downloader.select_channels()
        await downloader.client.disconnect()
        logger.info('重配置完成，未启动下载')
        return
    await downloader.run()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('程序被用户中断')
        sys.exit(0)
    except Exception as e:
        logger.error(f'程序异常退出: {e}')
        sys.exit(1)
