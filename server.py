"""Веб-сервер для приёма заявок с сайта + Telegram-бот.

Эндпоинты:
  POST /generate  — сайт шлёт сюда form data, мы генерируем меню и шлём клиенту в Telegram
  GET  /health    — health check
  POST /telegram/<token>  — вебхук для бота (если используется)
"""

import os
import sys
import json
import logging
import requests
from flask import Flask, request, jsonify

# гарантируем, что локальные модули импортируются
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from menu_generator import (
    generate_calorie_plan,
    generate_budget_plan,
    format_telegram_message,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ration-bot")

app = Flask(__name__)

# =============== CONFIG (env vars) ===============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")  # токен от @BotFather
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")  # твой Telegram ID для отладки


# =============== Telegram ===============
def send_telegram(chat_id_or_username, text, parse_mode="Markdown"):
    """Отправить сообщение в Telegram."""
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is empty")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    # Если передали @username — резолвим через getChat
    if isinstance(chat_id_or_username, str) and chat_id_or_username.startswith("@"):
        resolved = resolve_username(chat_id_or_username)
        if not resolved:
            log.error(f"Cannot resolve {chat_id_or_username}")
            return False
        chat_id = resolved
    else:
        chat_id = chat_id_or_username
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            log.error(f"Telegram send failed: {r.status_code} {r.text}")
            return False
        return True
    except Exception as e:
        log.error(f"Telegram send error: {e}")
        return False


def resolve_username(username):
    """Получить chat_id по @username (пользователь должен был писать боту раньше)."""
    if not BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChat"
    try:
        r = requests.post(url, json={"chat_id": username}, timeout=10)
        if r.status_code == 200:
            return r.json()["result"]["id"]
    except Exception as e:
        log.error(f"resolve_username error: {e}")
    return None


# =============== Парсинг запросов с сайта ===============
def parse_calorie_request(form):
    family_size = int(form.get("family_size", 1) or 1)
    persons = []
    for i in range(family_size):
        kcal = form.get(f"person_{i}_kcal") or form.get("kcal") or "1500"
        meals = form.get(f"person_{i}_meals") or form.get("meals") or "5"
        persons.append({"kcal": kcal, "meals": meals})
    days = form.get("days", "7")
    budget = form.get("budget_value") or form.get("budget") or ""
    allergens = form.getlist("allergens") if hasattr(form, "getlist") else form.get("allergens", "")
    allergens_other = form.get("allergens_other", "")
    dislike = form.get("dislike", "")
    telegram = form.get("telegram", "")
    return {
        "family_size": family_size,
        "persons": persons,
        "days": int(days) if days else 7,
        "budget": int(budget) if budget else None,
        "allergens": [a for a in (allergens if isinstance(allergens, list) else [allergens]) if a] +
                     ([x.strip() for x in allergens_other.split(",") if x.strip()] if allergens_other else []),
        "dislikes": [x.strip() for x in dislike.split(",") if x.strip()],
        "telegram": telegram,
    }


def parse_budget_request(form):
    meal_types = form.getlist("meal_types") if hasattr(form, "getlist") else form.get("meal_types", [])
    budget = form.get("budget_value") or form.get("budget") or "5000"
    people = form.get("people", "1")
    days_b = form.get("days_b", "7")
    excludes = form.getlist("exclude") if hasattr(form, "getlist") else form.get("exclude", [])
    exclude_other = form.get("exclude_other", "")
    telegram = form.get("telegram_b") or form.get("telegram", "")
    return {
        "meal_types": meal_types if isinstance(meal_types, list) else [meal_types],
        "budget": int(budget),
        "people": people,
        "days_b": int(days_b),
        "excludes": [e for e in (excludes if isinstance(excludes, list) else [excludes]) if e] +
                     ([x.strip() for x in exclude_other.split(",") if x.strip()] if exclude_other else []),
        "telegram": telegram,
    }


# =============== Endpoints ===============
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "bot_configured": bool(BOT_TOKEN)})


@app.route("/generate", methods=["POST"])
def generate():
    form = request.form
    mode = form.get("mode", "calorie")
    log.info(f"Received {mode} request: {dict(form)}")

    if mode == "budget":
        req = parse_budget_request(form)
        result = generate_budget_plan(req)
    else:
        req = parse_calorie_request(form)
        result = generate_calorie_plan(req)

    telegram = req.get("telegram", "")
    if telegram and "error" not in result:
        message = format_telegram_message(result)
        ok = send_telegram(telegram, message)
        result["sent_to_telegram"] = ok
        log.info(f"Sent to {telegram}: {ok}")
    else:
        result["sent_to_telegram"] = False

    return jsonify(result), 200


# =============== Telegram Bot (polling mode для простого тестирования) ===============
@app.route("/bot/poll", methods=["POST"])
def bot_poll():
    """Обработать одно обновление от Telegram (для long polling)."""
    update = request.get_json()
    handle_update(update)
    return jsonify({"ok": True})


def handle_update(update):
    if "message" not in update:
        return
    msg = update["message"]
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "")
    username = msg.get("from", {}).get("username", "")
    log.info(f"Bot got message from {chat_id} (@{username}): {text}")

    if text.startswith("/start"):
        send_telegram(chat_id, (
            "Привет! 👋\n\n"
            "Я помогу собрать меню. Пока я работаю через сайт:\n"
            "👉 https://site-bb0.p.spru.io/\n\n"
            "Заполни форму — меню придёт сюда в Telegram автоматически."
        ))
        return

    if text.startswith("/test"):
        # Тестовая команда: генерируем меню
        result = generate_calorie_plan({
            "family_size": 1,
            "persons": [{"kcal": "1500", "meals": "5"}],
            "days": 2,
            "budget": 2000,
            "allergens": [],
            "dislikes": [],
        })
        send_telegram(chat_id, format_telegram_message(result))
        return


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
