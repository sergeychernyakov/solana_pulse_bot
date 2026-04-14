# src/models/__init__.py

"""
This module imports all models and enums for easy access.

The purpose of this module is to gather all models and enums 
from the `src.models` package in one place. This simplifies 
the usage of models and enums throughout the application by 
allowing imports from a single module.
"""

from src.models.base import Base

__all__ = ["Base"]
