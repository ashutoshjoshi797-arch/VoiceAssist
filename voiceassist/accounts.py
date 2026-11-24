"""Account errors shared by the account code. Accounts themselves live in the VoiceAssist cloud (see cloud.py)."""
from __future__ import annotations


class AccountError(Exception):
    """User-facing account error. `field` names the form field it relates to (or '')."""

    def __init__(self, message: str, field: str = "", status: int = 400):
        super().__init__(message)
        self.field = field
        self.status = status
