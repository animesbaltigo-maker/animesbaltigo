"""
services/memory.py — Memória curta por conversa (in-process, sem banco)

Guarda os últimos N turnos por chat_id, com TTL e eviction LRU.
Thread-safe. Sem dependências externas além da stdlib.

Uso nos handlers:
    from services.memory import conversation_memory

    history = conversation_memory.get_history(chat_id)
    reply   = generate_anime_reply(user_text, history=history)
    conversation_memory.add_turn(chat_id, user_text, reply)
"""

import threading
import time
from collections import deque, OrderedDict
from dataclasses import dataclass, field

# ─── Configuração ────────────────────────────────────────────────────────────

MAX_TURNS_PER_CHAT   = 6        # últimos 6 turnos (12 msgs) por chat
MAX_CHATS_IN_MEMORY  = 2_000    # evita leak de RAM em bots com muitos grupos
TTL_SECONDS          = 60 * 30  # 30 min sem atividade → esquece o contexto


# ─── Estrutura interna ────────────────────────────────────────────────────────

@dataclass
class _ChatSession:
    turns: deque
    last_active: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def is_expired(self) -> bool:
        return (time.monotonic() - self.last_active) > TTL_SECONDS


# ─── Manager ─────────────────────────────────────────────────────────────────

class ConversationMemory:
    """Memória de conversas multi-turn, thread-safe, com LRU e TTL."""

    def __init__(self) -> None:
        self._sessions: OrderedDict[int | str, _ChatSession] = OrderedDict()
        self._lock = threading.Lock()

    def get_history(self, chat_id: int | str) -> list[dict]:
        """Retorna histórico no formato [{"role": ..., "content": ...}]."""
        with self._lock:
            session = self._sessions.get(chat_id)
            if session is None or session.is_expired():
                return []
            session.touch()
            self._sessions.move_to_end(chat_id)
            return list(session.turns)

    def add_turn(
        self,
        chat_id: int | str,
        user_text: str,
        assistant_reply: str,
    ) -> None:
        """Registra um turno completo (user + assistant)."""
        with self._lock:
            self._evict_if_needed()

            if chat_id not in self._sessions:
                self._sessions[chat_id] = _ChatSession(
                    turns=deque(maxlen=MAX_TURNS_PER_CHAT * 2)
                )

            session = self._sessions[chat_id]
            session.turns.append({"role": "user",      "content": user_text})
            session.turns.append({"role": "assistant", "content": assistant_reply})
            session.touch()
            self._sessions.move_to_end(chat_id)

    def clear(self, chat_id: int | str) -> None:
        """Limpa o histórico de um chat. Útil para /esquecer."""
        with self._lock:
            self._sessions.pop(chat_id, None)

    def _evict_if_needed(self) -> None:
        # Primeiro remove expirados
        expired = [cid for cid, s in self._sessions.items() if s.is_expired()]
        for cid in expired:
            del self._sessions[cid]
        # Depois LRU se ainda cheio
        while len(self._sessions) >= MAX_CHATS_IN_MEMORY:
            self._sessions.popitem(last=False)


# Instância global — importe isso onde precisar
conversation_memory = ConversationMemory()
