# src/models/enums.py

"""
Enumerations for various commission types, and other related categories.
"""

from enum import Enum


class YesNoEnum(str, Enum):
    """
    Enumeration for Yes/No answers.
    """

    YES = "YES"
    NO = "NO"


class PriorityEnum(str, Enum):
    """
    Task priority levels.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
