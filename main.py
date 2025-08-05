import os
import json
import asyncio
import re
import time
import signal
import sys
import logging
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
                    'wait_interval_seconds': int(os.getenv('TGDL_WAIT_INTERVAL_SECONDS', '300')),
                    'initial_retry_delay': int(os.getenv('TGDL_INITIAL_RETRY_DELAY', '1')),
                    'max_retry_delay': int(os.getenv('TGDL_MAX_RETRY_DELAY', '1800')),
                    'max_retries': int(os.getenv('TGDL_MAX_RETRIES', '0')),
                    'max_concurrent_downloads': int(os.getenv('TGDL_MAX_CONCURRENT_DOWNLOADS', '3')),
                    'batch_size': int(os.getenv('TGDL_BATCH_SIZE', '15')),
                    'progress_step': int(os.getenv('TGDL_PROGRESS_STEP', '10')),
                    'exclude_patterns': os.getenv('TGDL_EXCLUDE_PATTERNS', '').split(',') if os.getenv('TGDL_EXCLUDE_PATTERNS') else []
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
                'wait_interval_seconds': int(os.getenv('TGDL_WAIT_INTERVAL_SECONDS', '300')),
                'initial_retry_delay': int(os.getenv('TGDL_INITIAL_RETRY_DELAY', '1')),
                'max_retry_delay': int(os.getenv('TGDL_MAX_RETRY_DELAY', '1800')),
                'max_retries': int(os.getenv('TGDL_MAX_RETRIES', '0')),
                'max_concurrent_downloads': int(os.getenv('TGDL_MAX_CONCURRENT_DOWNLOADS', '3')),
                'batch_size': int(os.getenv('TGDL_BATCH_SIZE', '15')),
                'progress_step': int(os.getenv('TGDL_PROGRESS_STEP', '10')),
                'exclude_patterns': os.getenv('TGDL_EXCLUDE_PATTERNS', '').split(',') if os.getenv('TGDL_EXCLUDE_PATTERNS') else []
            }
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
            # 如果代理配置为空，尝试从环境变量读取
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
        return {
            'max_file_size_mb': int(os.getenv('TGDL_MAX_FILE_SIZE_MB', str(download_settings.get('max_file_size_mb', 500)))),
            'wait_interval_seconds': int(os.getenv('TGDL_WAIT_INTERVAL_SECONDS', str(download_settings.get('wait_interval_seconds', 300)))),
            'initial_retry_delay': int(os.getenv('TGDL_INITIAL_RETRY_DELAY', str(download_settings.get('initial_retry_delay', 1)))),
            'max_retry_delay': int(os.getenv('TGDL_MAX_RETRY_DELAY', str(download_settings.get('max_retry_delay', 1800)))),
            'max_retries': int(os.getenv('TGDL_MAX_RETRIES', str(download_settings.get('max_retries', 0)))),
            'max_concurrent_downloads': int(os.getenv('TGDL_MAX_CONCURRENT_DOWNLOADS', str(download_settings.get('max_concurrent_downloads', 3)))),
            'batch_size': int(os.getenv('TGDL_BATCH_SIZE', str(download_settings.get('batch_size', 15)))),
            'progress_step': int(os.getenv('TGDL_PROGRESS_STEP', str(download_settings.get('progress_step', 10)))),
            'exclude_patterns': os.getenv('TGDL_EXCLUDE_PATTERNS', '').split(',') if os.getenv('TGDL_EXCLUDE_PATTERNS') else []
        }
class FileManager:
    @staticmethod
    def sanitize_filename(name: str) -> str:
        return re.sub(r'[^\w\-_. ]', '_', name)

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
        save_path = os.path.join(MEDIA_DIR, safe_name)
        tmp_path = os.path.join(MEDIA_DIR, tmp_name) + '.part'
        logger.debug(f'生成文件路径: {save_path}')
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

        for pattern in exclude_patterns:
            if not pattern:  # 跳过空字符串
                continue
            try:
                if re.search(pattern, filename):
                    logger.debug(f'文件名 {filename} 匹配排除模式 {pattern}')
                    return True
            except re.error as e:
                logger.warning(f'排除模式 {pattern} 无效: {e}')
        return False

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
        is_valid = size <= max_size
        logger.debug(f'检查文件大小: {size/1024/1024:.2f}MB, 限制: {download_settings["max_file_size_mb"]}MB, 是否有效: {is_valid}')
        return is_valid

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
        self.seen_ids = set()  # 用于快速查找
        self.seen_ids_queue = deque(maxlen=500)
        self.last_id = 0

    async def fetch_valid_messages(self, entity) -> list:
        """
        尝试获取 batch_size 条满足下载条件的消息（媒体类型 + 文件大小）
        如果已无新消息，可能返回不足 batch_size 条
        """
        valid_messages = []
        exhausted = False

        while len(valid_messages) < self.download_settings['batch_size'] and not exhausted:
            candidate_messages = []
            async for msg in self.client.iter_messages(entity, limit=self.download_settings['batch_size'] * 2, offset_id=self.last_id):
                if msg.id in self.seen_ids:
                    continue
                candidate_messages.append(msg)
                self.seen_ids.add(msg.id)
                self.seen_ids_queue.append(msg.id)
                # 当deque满了，自动移除最旧的ID
                if len(self.seen_ids_queue) == self.seen_ids_queue.maxlen:
                    old_id = self.seen_ids_queue[0]
                    self.seen_ids.discard(old_id)
                self.last_id = msg.id
            if not candidate_messages:
                exhausted = True
                break

            for msg in candidate_messages:
                if MediaValidator.should_download_media(msg, self.media_types, self.config):
                    doc = msg.media.document
                    size = getattr(doc, 'size', 0)
                    if MediaValidator.check_file_size(size, self.config):
                        valid_messages.append(msg)
                        if len(valid_messages) >= self.download_settings['batch_size']:
                            break

        return valid_messages

class TelegramDownloader:
    def __init__(self):
        logger.info('初始化 TelegramDownloader')
        self.config = ConfigManager.load_config()
        self.client = None
        self.audio_checker = AudioQualityChecker(self.config)
        self.preprocessor = None
        self.download_settings = ConfigManager.get_download_settings(self.config)
        self.progress_tracker = ProgressTracker(self.download_settings['progress_step'] if 'progress_step' in self.download_settings else 10)

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

    async def download_media(self, message, channel_title: str) -> None:
        if not MediaValidator.should_download_media(message, self.config['media_types'], self.config):
            return

        doc = message.media.document
        size = doc.size or 0
        if not MediaValidator.check_file_size(size, self.config):
            logger.warning(f'跳过大文件: {size/1024/1024:.2f}MB')
            return

        tmp_path, tmp_name, save_path, safe_name = FileManager.get_filepath(message, channel_title)
        mime = doc.mime_type or ''
        
        # 检查是否需要进行音频质量比较
        if os.path.exists(save_path) and 'audio' in mime:
            if not self.audio_checker.should_replace_audio(save_path, doc, size):
                return
        elif os.path.exists(save_path):
            logger.info(f'文件已存在，跳过: {save_path}')
            return

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
            os.rename(tmp_path, save_path)
            logger.info(f'下载完成: {save_path}')
        except Exception as e:
            logger.error(f'下载失败: {save_path}, 错误: {e}')
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
                logger.debug(f'删除临时文件: {tmp_path}')
        finally:
            # 清理进度跟踪器
            DISABLE_TQDM and self.progress_tracker.clear(safe_name)

    async def _limited_download(self, sem: Semaphore, message, title: str):
        async with sem:
            await self.download_media(message, title)

    async def process_channel(self, channel: str) -> None:
        try:
            entity = await self.client.get_entity(int(channel))
            title = entity.title or channel
            logger.info(f'开始处理频道: {title}')
            retry_count = 0
            retry_delay = self.download_settings['initial_retry_delay']
            sem = Semaphore(self.download_settings['max_concurrent_downloads'])

            while not stop_event.is_set():
                try:
                    messages = await self.preprocessor.fetch_valid_messages(entity)
                    if not messages:
                        logger.info(f'频道 {title} 暂无新消息，等待 {self.download_settings["wait_interval_seconds"]} 秒')
                        await asyncio.sleep(self.download_settings['wait_interval_seconds'])
                        continue

                    logger.info(f'{title} 获取到 {len(messages)} 条有效消息，开始并发下载')
                    tasks = [self._limited_download(sem, msg, title) for msg in messages]
                    await asyncio.gather(*tasks)

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
