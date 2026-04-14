# src/config/__init__.py

"""
Configuration Module

This module initializes the application's configuration settings based on the environment.
It utilizes environment variables to determine whether to load development or production settings.
"""

import os

from dotenv import load_dotenv

from src.config.settings import Config, DevelopmentConfig, ProductionConfig

load_dotenv()


def get_config() -> Config:
    """
    Retrieves the configuration settings based on the environment.

    :return: An instance of Config (DevelopmentConfig or ProductionConfig).
    """
    env = os.getenv("APP_ENV", "development").lower()  # Use 'development' as the default environment
    if env == "production":
        return ProductionConfig()
    return DevelopmentConfig()


# Usage example:
config: Config = get_config()
