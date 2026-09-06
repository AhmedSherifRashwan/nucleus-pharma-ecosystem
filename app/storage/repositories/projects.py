"""Project persistence and Slack thread mapping (PRD s14, s17.2)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import Project


class ProjectRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        *,
        name: str,
        objective: str | None = None,
        context_summary: str | None = None,
        slack_channel_id: str | None = None,
        slack_thread_ts: str | None = None,
    ) -> Project:
        project = Project(
            name=name,
            objective=objective,
            context_summary=context_summary,
            slack_channel_id=slack_channel_id,
            slack_thread_ts=slack_thread_ts,
        )
        self.session.add(project)
        await self.session.flush()
        return project

    async def get_by_name(self, name: str) -> Project | None:
        """Find a project by the name it was ingested or created under."""
        stmt = select(Project).where(Project.name == name)
        return (await self.session.execute(stmt)).scalars().first()

    async def get(self, project_id: uuid.UUID) -> Project | None:
        return await self.session.get(Project, project_id)

    async def get_by_thread(self, slack_channel_id: str, thread_ts: str) -> Project | None:
        """One Slack thread is one project/session (s17.2)."""
        stmt = select(Project).where(
            Project.slack_channel_id == slack_channel_id,
            Project.slack_thread_ts == thread_ts,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_or_create_for_thread(
        self, *, slack_channel_id: str, thread_ts: str, name: str
    ) -> tuple[Project, bool]:
        """Returns (project, created). A follow-up in the same thread reuses it."""
        existing = await self.get_by_thread(slack_channel_id, thread_ts)
        if existing is not None:
            return existing, False
        project = await self.create(
            name=name, slack_channel_id=slack_channel_id, slack_thread_ts=thread_ts
        )
        return project, True

    async def update_context(self, project_id: uuid.UUID, context_summary: str) -> None:
        project = await self.session.get(Project, project_id)
        if project is not None:
            project.context_summary = context_summary
