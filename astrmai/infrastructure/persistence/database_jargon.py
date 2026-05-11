import time
from typing import Dict, List, Optional

from sqlmodel import desc, select

from .orm_models import Jargon


class JargonPersistenceMixin:
    def save_jargon(self, jargon: Jargon):
        def _sync(session):
            statement = select(Jargon).where(
                Jargon.group_id == jargon.group_id,
                Jargon.content == jargon.content,
            )
            existing = session.exec(statement).first()
            if existing:
                existing.count += 1
                existing.updated_at = time.time()
                if jargon.meaning:
                    existing.meaning = jargon.meaning
                    existing.is_complete = jargon.is_complete
                    existing.is_jargon = jargon.is_jargon
                session.add(existing)
                target = existing
            else:
                session.add(jargon)
                target = jargon
            session.commit()
            session.refresh(target)
            jargon.is_jargon = target.is_jargon
            jargon.is_complete = target.is_complete
            jargon.content = target.content
            jargon.meaning = target.meaning
            return self._clone_model(target)

        return self._run_with_session(_sync)

    def get_jargons(self, group_id: str, limit: int = 20, only_confirmed: bool = True) -> List[Jargon]:
        with self.get_session() as session:
            statement = select(Jargon).where(Jargon.group_id == group_id)
            if only_confirmed:
                statement = statement.where(Jargon.is_jargon == True)
            statement = statement.order_by(desc(Jargon.updated_at)).limit(limit)
            results = session.exec(statement).all()
            return [Jargon.model_validate(item.model_dump()) for item in results]

    def get_recent_jargons(self, group_id: str, hours: int = 24) -> List[Jargon]:
        with self.get_session() as session:
            cutoff_time = time.time() - (hours * 3600)
            statement = select(Jargon).where(
                Jargon.group_id == group_id,
                Jargon.is_jargon == True,
                Jargon.updated_at >= cutoff_time,
            ).order_by(desc(Jargon.updated_at))
            results = session.exec(statement).all()
            return [Jargon.model_validate(item.model_dump()) for item in results]

    def get_jargon(self, group_id: str, word: str) -> Optional[str]:
        if not group_id or not word:
            return None
        with self.get_session() as session:
            statement = select(Jargon).where(
                Jargon.group_id == group_id,
                Jargon.content == word,
            ).order_by(desc(Jargon.updated_at))
            result = session.exec(statement).first()
            if result and result.meaning:
                return result.meaning
        return None

    def search_jargons(self, keyword: str, limit: int = 3) -> List[Jargon]:
        if not keyword:
            return []
        keyword_lower = keyword.lower()
        with self.get_session() as session:
            statement = select(Jargon).order_by(desc(Jargon.updated_at))
            results = session.exec(statement).all()
            matches = []
            for item in results:
                content = (item.content or "").lower()
                meaning = (item.meaning or "").lower()
                if keyword_lower in content or keyword_lower in meaning:
                    matches.append(Jargon.model_validate(item.model_dump()))
                if len(matches) >= limit:
                    break
            return matches

    async def save_jargon_async(self, jargon: Jargon):
        return await self._run_blocking(self.save_jargon, jargon, with_lock=True)

    async def get_recent_jargons_async(self, group_id: str, hours: int = 24):
        return await self._run_blocking(self.get_recent_jargons, group_id, hours)

    async def get_jargons_async(self, group_id: str, limit: int = 20, only_confirmed: bool = True):
        return await self._run_blocking(self.get_jargons, group_id, limit, only_confirmed)

    async def load_jargon_list(self, group_id: str, limit: int = 20) -> List[Dict[str, str]]:
        items = await self.get_jargons_async(group_id, limit=limit, only_confirmed=True)
        return [
            {"text": item.content, "meaning": item.meaning, "situation": ""}
            for item in items
            if item.content and item.meaning
        ]