# src/models/database_base.py

"""
SQLAlchemy declarative base for database models.

This module provides the base class for all SQLAlchemy ORM models.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    Base class for all SQLAlchemy models.

    All database models should inherit from this class.
    Provides common functionality and metadata for database tables.
    """

    pass
