import os
import sys
import logging
from pathlib import Path

# Log directory: use local ./logs on Windows so /data/logs is not required
LOG_DIR = str(Path(__file__).resolve().parent / "logs") if os.name == "nt" else "/data/logs"


def _safe_stderr():
    """Use UTF-8 for stderr so non-ASCII log messages don't crash on Windows (cp1252)."""
    if hasattr(sys.stderr, "buffer"):
        return __import__("io").TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    return sys.stderr

def setup_logger(name="sandbox", file_name=None):
    """设置并返回配置好的日志记录器
    
    Args:
        name (str): 日志记录器名称，默认为"sandbox"
        file_name (str): 日志文件名，如果为None则使用name.log
        
    Returns:
        logging.Logger: 配置好的日志记录器
    """
    global LOG_DIR
    logger = logging.getLogger(name)
    
    # 如果记录器已经有处理器，直接返回
    if logger.handlers:
        return logger
    
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except Exception:
        LOG_DIR = str(Path(__file__).resolve().parent)
        os.makedirs(LOG_DIR, exist_ok=True)

    if file_name is None:
        file_name = f"{name}.log"
    log_file = os.path.join(LOG_DIR, file_name)
    logger.setLevel(logging.INFO)

    # FileHandler with UTF-8 so log file can hold any message
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.INFO)

    # 创建控制台处理器 (UTF-8 on Windows to avoid UnicodeEncodeError)
    console_handler = logging.StreamHandler(_safe_stderr())
    console_handler.setLevel(logging.INFO)

    # 创建格式化器
    formatter = logging.Formatter('%(asctime)s %(threadName)s %(filename)s:%(funcName)s:%(lineno)d %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    # 将处理器添加到记录器
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

# 创建默认的日志记录器
logger = setup_logger("sandbox")
logger.info("日志系统初始化完成")
