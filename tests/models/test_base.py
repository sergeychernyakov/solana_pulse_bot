# tests/models/test_base.py

"""
Tests for shared Pydantic base model validators.

These tests cover validation helpers that are used by higher-level models.
"""

import pytest
from pydantic import field_validator

from src.models.base import Base


class ContactModel(Base):
    """Small concrete model for exercising Base validation helpers."""

    phone: str | None = None

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        """Validate phone values through the shared helper."""
        return cls.validate_phone_number(value, "phone")


def test_base_is_valid_returns_true_for_valid_model() -> None:
    """Test that valid model data passes re-validation."""
    contact = ContactModel(phone="7123456788")

    assert contact.is_valid() is True
    assert contact.get_validation_errors() is None
    assert contact.phone == "07123456788"


def test_base_is_valid_stores_validation_errors() -> None:
    """Test that invalid constructed data is converted to field errors."""
    contact = ContactModel.model_construct(phone="123")

    assert contact.is_valid() is False
    assert contact.get_validation_errors() == {
        "phone": "Value error, phone must be in UK local format (07XXXXXXXXX) or (03XXXXXXXX)."
    }


def test_validate_phone_number_accepts_none_and_valid_numbers() -> None:
    """Test phone helper accepts optional and normalized UK numbers."""
    assert ContactModel.validate_phone_number(None, "phone") is None
    assert ContactModel.validate_phone_number("07123456788", "phone") == "07123456788"
    assert ContactModel.validate_phone_number("3123456788", "phone") == "03123456788"


def test_validate_phone_number_rejects_dummy_numbers() -> None:
    """Test phone helper rejects configured dummy values."""
    with pytest.raises(ValueError, match="dummy phone number"):
        ContactModel.validate_phone_number("07123456789", "phone")


def test_validate_email_address_rejects_disposable_domain() -> None:
    """Test email helper rejects disposable email providers."""
    assert ContactModel.validate_email_address("user@example.com", "email") == "user@example.com"

    with pytest.raises(ValueError, match="disposable email address"):
        ContactModel.validate_email_address("user@mailinator.com", "email")


def test_validate_zipcode_accepts_none_and_valid_postcode() -> None:
    """Test postcode helper accepts optional and valid UK postcode values."""
    assert ContactModel.validate_zipcode(None) is None
    assert ContactModel.validate_zipcode("SW1A 1AA") == "SW1A 1AA"


def test_validate_zipcode_rejects_invalid_postcode() -> None:
    """Test postcode helper rejects malformed UK postcode values."""
    with pytest.raises(ValueError, match="valid UK postal code"):
        ContactModel.validate_zipcode("not-a-postcode")
