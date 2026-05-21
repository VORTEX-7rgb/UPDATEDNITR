"""User persistence repository using SQLAlchemy async sessions."""

import logging
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.db.crypto import encrypt_password

logger = logging.getLogger(__name__)


class UserRepository:
    """Manages database persistence for the User model."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_user(self, telegram_id: int, roll_number: str, raw_password: str) -> User:
        """Create and store a new user, transparently encrypting their password."""
        logger.info("Creating new user database record for Roll: %s", roll_number)
        
        encrypted_pass = encrypt_password(raw_password)
        user = User(
            telegram_id=telegram_id,
            roll_number=roll_number,
            encrypted_password=encrypted_pass,
        )
        
        self.session.add(user)
        await self.session.flush()  # Assigns primary key 'id' to user in active transaction
        return user

    async def get_by_telegram_id(self, telegram_id: int) -> Optional[User]:
        """Retrieve a user by their unique Telegram user ID."""
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_credentials(self, user_id: int, roll_number: str, raw_password: str) -> None:
        """Update a user's roll number and encrypted password."""
        logger.info("Updating credentials in database for User ID: %s", user_id)
        
        stmt = select(User).where(User.id == user_id)
        result = await self.session.execute(stmt)
        user = result.scalar_one_or_none()
        
        if not user:
            raise ValueError(f"User with ID {user_id} not found.")
            
        encrypted_pass = encrypt_password(raw_password)
        user.roll_number = roll_number
        user.encrypted_password = encrypted_pass
        
        await self.session.flush()

    async def delete_user(self, user_id: int) -> None:
        """Safely delete a user and cascading children objects from persistent storage."""
        logger.info("Deleting user record from database for User ID: %s", user_id)
        
        stmt = select(User).where(User.id == user_id)
        result = await self.session.execute(stmt)
        user = result.scalar_one_or_none()
        
        if user:
            await self.session.delete(user)
            await self.session.flush()

