"""
配置管理模块 - 集中管理所有应用配置
"""
import os
import logging
from dotenv import load_dotenv
import pytz

load_dotenv()


class Config:
    """应用配置类"""
    
    # Telegram 配置
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    CHAT_ID = os.getenv("CHAT_ID")
    
    # API 配置
    RAPID_API_KEY = os.getenv("RAPID_API_KEY")
    API_BASE_URL = "https://api-football-v1.p.rapidapi.com/v3"
    API_CACHE_TTL = 3600  # 秒
    
    # 足球数据配置
    LEAGUE_ID = os.getenv("LEAGUE_ID", "39")  # 默认英超
    SEASON = int(os.getenv("SEASON", "2024"))
    PREDICTIONS_COUNT = 3  # 每天推送的预测场数
    
    # 时区配置
    TIMEZONE_STR = os.getenv("TIMEZONE", "UTC")
    TIMEZONE = pytz.timezone(TIMEZONE_STR)
    
    # 定时任务配置
    SCHEDULED_HOUR = int(os.getenv("SCHEDULED_HOUR", "8"))
    SCHEDULED_MINUTE = int(os.getenv("SCHEDULED_MINUTE", "0"))
    
    # 日志配置
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE = "bot.log"
    
    # 联赛代码映射
    LEAGUE_NAMES = {
        "39": "英超",
        "140": "西甲",
        "135": "意甲",
        "78": "德甲",
        "61": "法甲",
        "71": "苏超",
        "88": "荷甲",
        "203": "澳超",
        "179": "日超",
        "2": "欧冠",
        "5": "欧联",
    }
    
    @classmethod
    def validate(cls):
        """验证必需的配置"""
        errors = []
        
        if not cls.TELEGRAM_TOKEN:
            errors.append("TELEGRAM_TOKEN 未设置")
        if not cls.CHAT_ID:
            errors.append("CHAT_ID 未设置")
        if not cls.RAPID_API_KEY:
            errors.append("RAPID_API_KEY 未设置")
        
        if errors:
            raise ValueError("配置错误：\n" + "\n".join(errors))
    
    @classmethod
    def get_league_name(cls, league_id):
        """获取联赛名称"""
        return cls.LEAGUE_NAMES.get(str(league_id), "未知联赛")
    
    @classmethod
    def to_dict(cls):
        """返回配置字典（不包含敏感信息）"""
        return {
            "LEAGUE_ID": cls.LEAGUE_ID,
            "LEAGUE_NAME": cls.get_league_name(cls.LEAGUE_ID),
            "SEASON": cls.SEASON,
            "TIMEZONE": cls.TIMEZONE_STR,
            "SCHEDULED_TIME": f"{cls.SCHEDULED_HOUR:02d}:{cls.SCHEDULED_MINUTE:02d}",
            "PREDICTIONS_COUNT": cls.PREDICTIONS_COUNT,
            "LOG_LEVEL": cls.LOG_LEVEL,
        }


def setup_logging():
    """配置日志系统"""
    log_level = getattr(logging, Config.LOG_LEVEL, logging.INFO)
    
    logger = logging.getLogger()
    logger.setLevel(log_level)
    
    # 文件处理器
    file_handler = logging.FileHandler(
        Config.LOG_FILE,
        encoding='utf-8'
    )
    file_handler.setLevel(log_level)
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    
    # 日志格式
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

