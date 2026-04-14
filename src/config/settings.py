# src/config/settings.py

"""
Settings Module

This module defines configuration classes for different environments
(Development and Production) using dataclasses. It loads environment
variables from a `.env` file and sets various application settings.
"""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    """Base configuration class."""

    DEBUG: bool = False  # pylint: disable=invalid-name
    APP_ENV: str = os.getenv("APP_ENV", "development")

    # Database Configuration
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./tmp/tasks.db")
    DATABASE_ECHO: bool = False  # Log SQL queries

    # API Configuration
    API_V1_PREFIX: str = os.getenv("API_V1_PREFIX", "/api/v1")
    CORS_ORIGINS: list[str] = field(default_factory=lambda: ["*"])

    # Server Configuration
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))


@dataclass
class DevelopmentConfig(Config):
    """Development configuration."""

    DEBUG: bool = True
    DATABASE_ECHO: bool = True  # Show SQL in development
    RELOAD: bool = True


@dataclass
class ProductionConfig(Config):
    """Production configuration."""

    DEBUG: bool = False  # pylint: disable=invalid-name
    DATABASE_ECHO: bool = False
    RELOAD: bool = False
    CORS_ORIGINS: list[str] = field(
        default_factory=lambda: os.getenv("CORS_ORIGINS", "").split(",") if os.getenv("CORS_ORIGINS") else []
    )


# Get config instance based on environment
def get_config() -> Config:
    """
    Get configuration instance based on APP_ENV.

    Returns:
        Config: Configuration instance (Development or Production)
    """
    env = os.getenv("APP_ENV", "development").lower()
    if env == "production":
        return ProductionConfig()
    return DevelopmentConfig()


config = get_config()
