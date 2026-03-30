import asyncio
import html
import re
import secrets
import time
from urllib.parse import quote_plus

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from services.metrics import log_event, mark_user_seen
from utils.gatekeeper import ensure_channel_membership

RESULTS_PER_PAGE = 8

SEARCH_COOLDOWN = 1.5
SEARCH_INFLIGHT_TTL = 12.0
SEARCH_TIMEOUT = 20.0

SEARCH_BANNER_URL = "https://photo.chelpbot.me/AgACAgEAAxkBaL-UMWnDPUdoNCaz4ZUFvzeOHSVXh0oRAALTC2sbdnEYRrjsVpeCeT08AQADAgADeQADOgQ/photo.jpg"
MINIAPP_URL = "https://jerusalem-editorials-screensavers-for.trycloudflare.com/miniapp/index.html"

_SEARCH_USER_LOCKS = {}
_SEARCH_INFLIGHT = {}


def _now() -> float:
    return time.monotonic()


def _normalize_query(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _search_last_key(user_id: int) -> str:
    return f"search_last:{user_id}"


def _search_last_query_key(user_id: int) -> str:
    return f"search_last_query:{user_id}"


def _search_inflight_key(user_id: int, query: str) -> str:
    return f"{user_id}:{query.lower()}"


def _is_search_cooldown(context: ContextTypes.DEFAULT_TYPE, user_id: int, query: str) -> bool:
    now = _now()

    last_ts = context.user_data.get(_search_last_key(user_id), 0.0)
    last_query = context.user_data.get(_search_last_query_key(user_id), "")

    if query and last_query == query and (now - last_ts) < SEARCH_COOLDOWN:
        return True

    context.user_data[_search_last_key(user_id)] = now
    context.user_data[_search_last_query_key(user_id)] = query
    return False


def _is_inflight(user_id: int, query: str) -> bool:
    key = _search_inflight_key(user_id, query)
    item = _SEARCH_INFLIGHT.get(key)
    if not item:
        return False

    if _now() - item > SEARCH_INFLIGHT_TTL:
        _SEARCH_INFLIGHT.pop(key, None)
        return False

    return True


def _set_inflight(user_id: int, query: str):
    _SEARCH_INFLIGHT[_search_inflight_key(user_id, query)] = _now()


def _clear_inflight(user_id: int, query: str):
    _SEARCH_INFLIGHT.pop(_search_inflight_key(user_id, query), None)


def _user_lock(user_id: int) -> asyncio.Lock:
    lock = _SEARCH_USER_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _SEARCH_USER_LOCKS[user_id] = lock
    return lock


def _clean_button_title(title: str) -> str:
    title = (title or "").strip()

    title = re.sub(r"\b\d+\.\d+\b", "", title)
    title = re.sub(r"\bA(?:10|12|14|16|18|L)\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\bLIVRE\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\bN/?A\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\(\s*\)", "", title)
    title = re.sub(r"\s{2,}", " ", title).strip(" -–|•")

    return title or "Sem título"


def _build_miniapp_search_url(query: str) -> str:
    base = MINIAPP_URL.rstrip("/")
    return f"{base}?search={quote_plus(query)}&page=1"


def _build_search_button_title(item: dict) -> str:
    title = _clean_button_title(item.get("title") or "Sem título")

    if item.get("is_dubbed"):
        title = f"{title} [DUBLADO]"
    else:
        title = f"{title} [LEGENDADO]"

    return title


def _build_search_text(query: str, page: int, total: int) -> str:
    total_pages = max(1, ((total - 1) // RESULTS_PER_PAGE) + 1)
    safe_query = html.escape((query or "").strip())

    return (
        f"🔎 <b>Resultado da busca</b>\n\n"
        f"━━━━━━━━━━━━━━\n\n"
        f"🎬 <b>Pesquisa:</b> {safe_query}\n"
        f"📚 <b>Resultados:</b> {total}\n"
        f"📄 <b>Página:</b> {page}/{total_pages}\n\n"
        f"Toque em uma obra abaixo para abrir os detalhes."
    )


def _store_search_session(context: ContextTypes.DEFAULT_TYPE, query: str, results: list) -> str:
    token = secrets.token_hex(4)
    context.user_data[f"search_session:{token}"] = {
        "query": query,
        "results": results,
        "created_at": _now(),
    }
    return token


def _build_results_keyboard(results: list, page: int, total: int, token: str) -> InlineKeyboardMarkup:
    rows = []

    start = (page - 1) * RESULTS_PER_PAGE
    end = start + RESULTS_PER_PAGE
    page_items = results[start:end]

    for idx, item in enumerate(page_items, start=start + 1):
        title = _build_search_button_title(item)

        if len(title) > 42:
            title = title[:39].rstrip() + "..."

        rows.append([
            InlineKeyboardButton(
                f"🎬 {idx}. {title}",
                callback_data=f"sa|{token}|{idx - 1}",
            )
        ])

    nav = []

    if page > 1:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Anterior",
                callback_data=f"sp|{token}|{page - 1}",
            )
        )

    if end < total:
        nav.append(
            InlineKeyboardButton(
                "Próxima ➡️",
                callback_data=f"sp|{token}|{page + 1}",
            )
        )

    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(rows)


async def _safe_delete_message(msg):
    if not msg:
        return
    try:
        await msg.delete()
    except TelegramError:
        pass
    except Exception:
        pass


async def _safe_edit_loading(msg, text: str):
    if not msg:
        return False
    try:
        await msg.edit_text(text, parse_mode="HTML")
        return True
    except TelegramError:
        return False
    except Exception:
        return False


async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_channel_membership(update, context):
        return

    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message

    if user:
        user_name = user.username or user.first_name or ""
        seen_result = mark_user_seen(user.id, user_name)
        if hasattr(seen_result, "__await__"):
            await seen_result

    if not chat or chat.type != "private":
        await message.reply_text(
            "🔒 <b>Esse comando só funciona no privado.</b>\n\n"
            "Me chama no PV e envie:\n"
            "<code>/buscar nome do anime</code>",
            parse_mode="HTML",
        )
        return

    if not context.args:
        await message.reply_text(
            "🔎 <b>Como buscar um anime</b>\n\n"
            "Envie o comando no formato:\n"
            "<code>/buscar nome do anime</code>\n\n"
            "📌 <b>Exemplos</b>\n"
            "• <code>/buscar naruto</code>\n"
            "• <code>/buscar one piece</code>\n"
            "• <code>/buscar solo leveling</code>",
            parse_mode="HTML",
        )
        return

    raw_query = " ".join(context.args)
    query = _normalize_query(raw_query)

    if not query:
        await message.reply_text(
            "⚠️ <b>Digite um nome para pesquisar.</b>\n\n"
            "Exemplo:\n"
            "<code>/buscar solo leveling</code>",
            parse_mode="HTML",
        )
        return

    if len(query) < 2:
        await message.reply_text(
            "⚠️ <b>Digite pelo menos 2 caracteres para buscar.</b>",
            parse_mode="HTML",
        )
        return

    if len(query) > 80:
        query = query[:80].rstrip()

    if not user:
        await message.reply_text(
            "❌ Não consegui identificar seu usuário agora.",
            parse_mode="HTML",
        )
        return

    if _is_search_cooldown(context, user.id, query):
        await message.reply_text(
            "⏳ <b>Aguarde um instante antes de repetir essa busca.</b>",
            parse_mode="HTML",
        )
        return

    if _is_inflight(user.id, query):
        await message.reply_text(
            "⏳ <b>Essa busca já está sendo processada.</b>",
            parse_mode="HTML",
        )
        return

    lock = _user_lock(user.id)

    async with lock:
        if _is_inflight(user.id, query):
            await message.reply_text(
                "⏳ <b>Essa busca já está sendo processada.</b>",
                parse_mode="HTML",
            )
            return

        _set_inflight(user.id, query)

        try:
            log_event(
                event_type="search",
                user_id=user.id,
                username=(user.username or user.first_name or ""),
                query_text=query,
                extra="miniapp_handoff",
            )

            miniapp_url = _build_miniapp_search_url(query)
            text = (
                "🔎 <b>Busca pronta no MiniApp</b>\n\n"
                f"🎬 <b>Pesquisa:</b> {html.escape(query)}\n\n"
                "Toque no botão abaixo para abrir os resultados direto no MiniApp."
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔎 Abrir busca no MiniApp",
                        web_app=WebAppInfo(url=miniapp_url),
                    )
                ]
            ])

            sent = False

            try:
                await message.reply_photo(
                    photo=SEARCH_BANNER_URL,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                sent = True
            except Exception as e:
                print("ERRO AO ENVIAR BANNER DA BUSCA MINIAPP:", repr(e))

            if not sent:
                await message.reply_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )

        except asyncio.TimeoutError:
            print(f"ERRO NA BUSCA: Timeout query={query!r}")

            await message.reply_text(
                "⏳ <b>A busca demorou demais.</b>\n\n"
                "Tente novamente em instantes.",
                parse_mode="HTML",
            )

        except Exception as e:
            print("ERRO NA BUSCA:", repr(e))

            await message.reply_text(
                "❌ <b>Erro ao buscar os animes.</b>\n\n"
                "Tente novamente em instantes.",
                parse_mode="HTML",
            )
        finally:
            _clear_inflight(user.id, query)
