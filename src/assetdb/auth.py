"""Bearer-token authentication with four roles.

Tokens are random 32-byte strings shown once at creation; only their SHA-256
is stored, so a database leak does not leak usable credentials.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ApiToken, Role

# role -> roles it implies
_IMPLIES = {
    Role.ADMIN: {Role.ADMIN, Role.OPERATOR, Role.VIEWER},
    Role.OPERATOR: {Role.OPERATOR, Role.VIEWER},
    Role.VIEWER: {Role.VIEWER},
    Role.ENROLL: {Role.ENROLL},  # deliberately cannot read the inventory
}


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role

    def has(self, role: Role) -> bool:
        return role in _IMPLIES[self.role]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(session: Session, name: str, role: Role, token: str | None = None) -> str:
    token = token or secrets.token_urlsafe(32)
    session.add(ApiToken(name=name, role=role, token_hash=hash_token(token)))
    session.commit()
    return token


def authenticate(session: Session, token: str) -> Principal | None:
    if not token:
        return None
    row = session.scalar(
        select(ApiToken).where(
            ApiToken.token_hash == hash_token(token), ApiToken.revoked.is_(False)
        )
    )
    return Principal(row.name, row.role) if row else None
