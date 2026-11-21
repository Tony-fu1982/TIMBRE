"""SSE 广播总线：event:job data:{id,status,stage,progress,log_seq}（SPEC §3）。"""
from __future__ import annotations

import asyncio
import json


class EventBus:
    def __init__(self):
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: str, data: dict) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for q in list(self._subs):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # ponytail: 慢消费者丢最旧一条；前端重连后按 log_seq 补齐，不丢事实
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass


bus = EventBus()