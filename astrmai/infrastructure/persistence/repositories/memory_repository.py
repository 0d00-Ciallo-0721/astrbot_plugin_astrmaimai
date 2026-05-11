from __future__ import annotations


class MemoryRepository:
    def __init__(self, db_service):
        self.db = db_service

    def save_jargon(self, jargon):
        return self.db.save_jargon(jargon)

    async def save_jargon_async(self, jargon):
        return await self.db.save_jargon_async(jargon)

    def get_jargons(self, group_id: str, limit: int = 20, only_confirmed: bool = True):
        return self.db.get_jargons(group_id, limit=limit, only_confirmed=only_confirmed)

    async def get_jargons_async(self, group_id: str, limit: int = 20, only_confirmed: bool = True):
        return await self.db.get_jargons_async(group_id, limit=limit, only_confirmed=only_confirmed)

    def get_jargon(self, group_id: str, word: str):
        return self.db.get_jargon(group_id, word)

    async def load_jargon_list(self, group_id: str, limit: int = 20):
        return await self.db.load_jargon_list(group_id, limit=limit)

    def update_nodes(self, nodes):
        return self.db.update_nodes(nodes)

    async def update_nodes_async(self, nodes):
        return await self.db.update_nodes_async(nodes)

    def search_nodes(self, query: str, limit: int = 3, include_description: bool = True):
        return self.db.search_nodes(query, limit=limit, include_description=include_description)

    async def search_nodes_async(self, query: str, limit: int = 3, include_description: bool = True):
        return await self.db.search_nodes_async(query, limit=limit, include_description=include_description)

    def save_event(self, event):
        return self.db.save_event(event)

    async def save_event_async(self, event):
        return await self.db.save_event_async(event)

    def save_retrieval_trace(self, trace):
        return self.db.save_retrieval_trace(trace)

    async def save_retrieval_trace_async(self, trace):
        return await self.db.save_retrieval_trace_async(trace)

    def get_recent_retrieval_traces(self, chat_id: str, limit: int = 10):
        return self.db.get_recent_retrieval_traces(chat_id, limit=limit)

    async def get_recent_retrieval_traces_async(self, chat_id: str, limit: int = 10):
        return await self.db.get_recent_retrieval_traces_async(chat_id, limit=limit)
