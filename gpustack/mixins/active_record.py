import json
import logging
import math
from typing import Any, AsyncGenerator, Optional, Union, overload

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import FlushError
from sqlalchemy.ext.asyncio import AsyncEngine
from gpustack.schemas.common import PaginatedList, Pagination
from gpustack.server.bus import Event, EventType, event_bus


logger = logging.getLogger(__name__)


class ActiveRecordMixin:
    """ActiveRecordMixin provides a set of methods to interact with the database."""

    __config__ = None

    @property
    def primary_key(self):
        """Return the primary key of the object."""

        return self.__mapper__.primary_key_from_instance(self)

    @classmethod
    async def first(cls, session: AsyncSession):
        """Return the first object of the model."""

        statement = select(cls)
        result = await session.exec(statement)
        return result.first()

    @classmethod
    async def one_by_id(cls, session: AsyncSession, id: int):
        """Return the object with the given id. Return None if not found."""

        return await session.get(cls, id)

    @classmethod
    async def first_by_field(cls, session: AsyncSession, field: str, value: Any):
        """Return the first object with the given field and value. Return None if not found."""

        return await cls.first_by_fields(session, {field: value})

    @classmethod
    async def one_by_field(cls, session: AsyncSession, field: str, value: Any):
        """Return the object with the given field and value. Return None if not found."""

        return await cls.one_by_fields(session, {field: value})

    @classmethod
    async def first_by_fields(cls, session: AsyncSession, fields: dict):
        """
        Return the first object with the given fields and values.
        Return None if not found.
        """

        statement = select(cls)
        for key, value in fields.items():
            statement = statement.where(getattr(cls, key) == value)

        result = await session.exec(statement)
        return result.first()

    @classmethod
    async def one_by_fields(cls, session: AsyncSession, fields: dict):
        """Return the object with the given fields and values. Return None if not found."""

        statement = select(cls)
        for key, value in fields.items():
            statement = statement.where(getattr(cls, key) == value)

        result = await session.exec(statement)
        return result.first()

    @classmethod
    async def all_by_field(cls, session: AsyncSession, field: str, value: Any):
        """
        Return all objects with the given field and value.
        Return an empty list if not found.
        """
        statement = select(cls).where(getattr(cls, field) == value)
        result = await session.exec(statement)
        return result.all()

    @classmethod
    async def all_by_fields(cls, session: AsyncSession, fields: dict):
        """
        Return all objects with the given fields and values.
        Return an empty list if not found.
        """

        statement = select(cls)
        for key, value in fields.items():
            statement = statement.where(getattr(cls, key) == value)
        result = await session.exec(statement)
        return result.all()

    @classmethod
    async def paginated_by_query(
        cls, session: AsyncSession, fields: dict, page: int, per_page: int
    ) -> PaginatedList[SQLModel]:
        """
        Return a paginated list of objects match the given fields and values.
        Return an empty list if not found.
        """

        statement = select(cls)
        for key, value in fields.items():
            statement = statement.where(col(getattr(cls, key)) == value)

        if page is not None and per_page is not None:
            statement = statement.offset((page - 1) * per_page).limit(per_page)
        result = await session.exec(statement)
        items = result.all()

        statement = select(func.count(cls.id))
        for key, value in fields.items():
            statement = statement.where(col(getattr(cls, key)) == value)

        result = await session.exec(statement)
        count = result.one()
        total_page = math.ceil(count / per_page)
        pagination = Pagination(
            page=page,
            perPage=per_page,
            total=count,
            totalPage=total_page,
        )

        return PaginatedList[cls](items=items, pagination=pagination)

    @classmethod
    def convert_without_saving(
        cls, source: Union[dict, SQLModel], update: Optional[dict] = None
    ) -> SQLModel:
        """
        Convert the source to the model without saving to the database.
        Return None if failed.
        """

        if isinstance(source, SQLModel):
            obj = cls.from_orm(source, update=update)
        elif isinstance(source, dict):
            obj = cls.parse_obj(source, update=update)
        return obj

    @classmethod
    async def create(
        cls,
        session: AsyncSession,
        source: Union[dict, SQLModel],
        update: Optional[dict] = None,
    ) -> Optional[SQLModel]:
        """Create and save a new record for the model."""

        obj = cls.convert_without_saving(source, update)
        if obj is None:
            return None

        await obj.save(session)
        await cls._publish_event(EventType.CREATED, obj)
        return obj

    @classmethod
    async def create_or_update(
        cls,
        session: AsyncSession,
        source: Union[dict, SQLModel],
        update: Optional[dict] = None,
    ) -> Optional[SQLModel]:
        """Create or update a record for the model."""

        obj = cls.convert_without_saving(source, update)
        if obj is None:
            return None
        pk = cls.__mapper__.primary_key_from_instance(obj)
        if pk[0] is not None:
            existing = await session.get(cls, pk)
            if existing is None:
                return None
            else:
                await existing.update(session, obj)
                return existing
        else:
            return await cls.create(session, obj)

    @classmethod
    async def count(cls, session: AsyncSession) -> int:
        """Return the number of records in the model."""

        return len(await cls.all(session))

    async def refresh(self, session: AsyncSession):
        """Refresh the object from the database."""

        await session.refresh(self)

    async def save(self, session: AsyncSession):
        """Save the object to the database. Raise exception if failed."""

        session.add(self)
        try:
            await session.commit()
            await session.refresh(self)
        except (IntegrityError, OperationalError, FlushError) as e:
            await session.rollback()
            raise e

    async def update(
        self, session: AsyncSession, source: Union[dict, SQLModel, None] = None
    ):
        """Update the object with the source and save to the database."""

        if isinstance(source, SQLModel):
            source = source.model_dump(exclude_unset=True)
        elif source is None:
            source = {}

        for key, value in source.items():
            setattr(self, key, value)
        await self.save(session)
        await self._publish_event(EventType.UPDATED, self)

    async def delete(self, session: AsyncSession):
        """Delete the object from the database."""

        await self._handle_cascade_delete(session)

        await session.delete(self)
        await session.commit()
        await self._publish_event(EventType.DELETED, self)

    async def _handle_cascade_delete(self, session: AsyncSession):
        """Handle cascading deletes for all defined relationships."""
        for rel in self.__mapper__.relationships:
            if rel.cascade.delete:
                # Load the related objects
                await session.refresh(self)
                related_objects = getattr(self, rel.key)

                # Delete each related object
                if isinstance(related_objects, list):
                    for related_object in related_objects:
                        await related_object.delete(session)
                elif related_objects:
                    await related_objects.delete(session)

    @classmethod
    async def all(cls, session: AsyncSession):
        """Return all objects of the model."""

        result = await session.exec(select(cls))
        return result.all()

    @classmethod
    async def delete_all(cls, session: AsyncSession):
        """Delete all objects of the model."""

        for obj in await cls.all(session):
            await obj.delete(session)
            await cls._publish_event(EventType.DELETED, obj)

    @classmethod
    async def _publish_event(cls, event_type: str, data: Any):
        try:
            await event_bus.publish(
                cls.__name__.lower(), Event(type=event_type, data=data)
            )
        except Exception as e:
            logger.error(f"Failed to publish event: {e}")

    @overload
    @classmethod
    async def subscribe(
        cls, session_or_engine: AsyncEngine
    ) -> AsyncGenerator[Event, None]: ...

    @overload
    @classmethod
    async def subscribe(
        cls, session_or_engine: AsyncEngine
    ) -> AsyncGenerator[Event, None]: ...

    @classmethod
    async def subscribe(
        cls, session_or_engine: Union[AsyncSession, AsyncEngine]
    ) -> AsyncGenerator[Event, None]:
        if isinstance(session_or_engine, AsyncSession):
            items = await cls.all(session_or_engine)
            for item in items:
                yield Event(type=EventType.CREATED, data=item)
        elif isinstance(session_or_engine, AsyncEngine):
            async with AsyncSession(session_or_engine) as session:
                items = await cls.all(session)
                for item in items:
                    yield Event(type=EventType.CREATED, data=item)
        else:
            raise ValueError("Invalid session or engine.")

        subscriber = event_bus.subscribe(cls.__name__.lower())

        try:
            while True:
                event = await subscriber.receive()
                yield event
        finally:
            event_bus.unsubscribe(cls.__name__.lower(), subscriber)

    @classmethod
    async def streaming(
        cls, session: AsyncSession, fields: Optional[dict] = None
    ) -> AsyncGenerator[str, None]:
        async for event in cls.subscribe(session):
            skip_event = False
            for key, value in (fields or {}).items():
                if getattr(event.data, key) != value:
                    skip_event = True
                    break
            if skip_event:
                continue

            yield json.dumps(jsonable_encoder(event), separators=(",", ":")) + "\n\n"
