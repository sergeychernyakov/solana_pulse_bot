# src/helpers/logger.py

"""
Logger utility module.

This module provides a utility function for setting up and retrieving a logger 
with specific configurations such as log level and handlers.
"""

import logging
from logging.handlers import RotatingFileHandler

from src.config import config

logging.getLogger("faker.factory").setLevel(logging.ERROR)


def get_logger(name: str) -> logging.Logger:
    """
    Returns a logger instance with a specified configuration.

    This function sets up a logger with both file and stream handlers. It ensures
    that multiple handlers are not added to the same logger to prevent duplicate logs.
    The file handler uses log rotation to manage log file sizes.

    Args:
        name (str): The name of the logger.

    Returns:
        logging.Logger: Configured logger instance.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        # Prevent adding multiple handlers to the same logger
        level = logging.DEBUG if config.DEBUG else logging.INFO
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

        # Create rotating file handler
        # maxBytes: 10MB per file, backupCount: 5 backup files
        file_handler = RotatingFileHandler(f"tmp/logs/{name}.log", maxBytes=10 * 1024 * 1024, backupCount=5)  # 10 MB
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)

        # Create stream handler
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(level)
        stream_handler.setFormatter(formatter)

        # Configure logger
        logger.setLevel(level)
        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)

    return logger
