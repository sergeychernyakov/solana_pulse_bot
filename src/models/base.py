# src/models/base.py

"""
Base model class with validation and utility methods.

This module defines a base model using Pydantic's `BaseModel`, providing
common functionality such as validation, error handling, and metadata retrieval
for fields. It is intended to be inherited by other models in the project.
"""

import re
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, ValidationError


class Base(BaseModel):
    """
    Base class for models with validation and metadata utilities.

    This class extends Pydantic's `BaseModel` to provide:
    - Validation with error reporting.
    - Utility method for retrieving field names and their metadata.
    """

    _validation_errors: Optional[Dict[str, str]] = None
    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True, extra="ignore")

    def is_valid(self) -> bool:
        """Checks if the model is valid by running validation."""
        try:
            # Use model_validate to re-validate the data
            self.__class__.model_validate(self.model_dump())
            self._validation_errors = None
            return True
        except ValidationError as e:
            self._validation_errors = {err["loc"][0]: err["msg"] for err in e.errors()}
            return False

    def get_validation_errors(self) -> Optional[Dict[str, str]]:
        """Returns a dictionary of fields with validation error messages if any."""
        return self._validation_errors

    @classmethod
    def validate_phone_number(cls, v: Any, field_name: str) -> str:
        """
        Validates UK phone numbers.

        Args:
            v (Any): The phone number to validate.
            field_name (str): The name of the field for error messages.

        Returns:
            str: The validated phone number.

        Raises:
            ValueError: If the phone number is invalid.
        """
        if v is None:
            return v
        v = str(v)
        if re.fullmatch(r"^\d{9,10}$", v) and not v.startswith("0"):
            v = "0" + v

        # Validate for UK mobile format only
        if not re.fullmatch(r"^0[37]\d{9}$", v):
            raise ValueError(f"{field_name} must be in UK local format (07XXXXXXXXX) or (03XXXXXXXX).")

        if v in ["07123456789", "03123456789"]:
            raise ValueError(f"{field_name} cannot be a dummy phone number.")
        return v

    @classmethod
    def validate_email_address(cls, v: str, field_name: str) -> str:
        """
        Validates email addresses to ensure they are not from disposable email providers.

        Args:
            v (str): The email address to validate.
            field_name (str): The name of the field for error messages.

        Returns:
            str: The validated email address.

        Raises:
            ValueError: If the email is from a disposable domain.
        """
        disposable_domains = ["mailinator.com", "tempmail.com", "10minutemail.com"]
        domain = v.split("@")[1]
        if domain in disposable_domains:
            raise ValueError(f"{field_name} cannot be a disposable email address.")
        return v

    @classmethod
    def validate_zipcode(cls, v: Any) -> str:
        """
        Validates UK postal codes using a regex pattern.

        Args:
            v (Any): The postal code to validate.

        Returns:
            str: The validated postal code.

        Raises:
            ValueError: If the postal code is invalid.
        """
        if v is None:
            return v
        if not re.fullmatch(r"[A-Z]{1,2}\d[A-Z\d]? \d[A-Z]{2}", v, re.IGNORECASE):
            raise ValueError("PostalCode must be a valid UK postal code (e.g., SW1A 1AA).")
        return v
