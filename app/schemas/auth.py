"""Effective caller identity and UI capabilities."""

from typing import Literal

from pydantic import BaseModel


class AuthCapabilities(BaseModel):
    read: bool
    write: bool
    admin: bool


class AuthContextOut(BaseModel):
    prefix: str
    role: Literal["read", "write", "admin"]
    source: str
    is_anonymous: bool
    auth_enabled: bool
    read_access: Literal["public", "key"]
    capabilities: AuthCapabilities