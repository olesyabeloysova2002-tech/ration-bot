"""Генератор меню и списка продуктов.

Принимает запрос с параметрами (режим, калораж, бюджет, аллергены и т.д.),
возвращает структуру с меню на каждый день и агрегированным списком покупок.
"""

import random
from collections import defaultdict
from dish_db import DISHES, get_by_meal_type, filter_safe, get_ingredient_price


# Соответствие числа приёмов пищи в день и их слотов
MEAL_SLOTS = {
    3: ["завтрак", "обед", "ужин"],
    4: ["завтрак", "обед", "перекус", "ужин"],
    5: ["завтрак", "перекус", "обед", "перекус", "ужин"],
}


def _score_dish(dish, target_kcal, recently_used_ids, cost_weight=0.0):
    """Оценка блюда: чем ближе к целевой калорийности и разнообразнее — тем лучше."""
    diff = abs(dish["kcal"] - target_kcal)
    kcal_score = -diff / max(target_kcal, 1)  # нормализуем
    variety_score = 0
    if dish["id"] in recently_used_ids:
        variety_score = -2.0  # сильный штраф за повтор
    cost_score = -dish["cost"] / 100 * cost_weight
    return kcal_score + variety_score + cost_score


SLOT_WEIGHTS = {
    "завтрак": 0.85,   # 1800 → 510 на завтрак
    "обед":    1.30,   # 1800 → 780 на обед
    "ужин":    0.95,   # 1800 → 570 на ужин
    "перекус": 0.20,   # 1800 → 120 на перекус
}


def _generate_day_menu(person_kcal, meals_per_day, allergens, dislikes, recently_used, exclude_ids=None):
    """Сгенерировать меню на один день для одного человека."""
    slots = MEAL_SLOTS.get(meals_per_day, MEAL_SLOTS[3])
    total_weight = sum(SLOT_WEIGHTS.get(s, 1.0) for s in slots)
    norm = person_kcal / total_weight if total_weight > 0 else person_kcal / len(slots)

    day_menu = []
    for slot in slots:
        candidates = filter_safe(
            get_by_meal_type(slot),
            allergens=allergens,
            dislikes=dislikes,
            exclude_ids=exclude_ids,
        )
        if not candidates:
            return None
        target = norm * SLOT_WEIGHTS.get(slot, 1.0)
        best = max(candidates, key=lambda d: _score_dish(d, target, recently_used))
        day_menu.append({"slot": slot, "dish": best})
        recently_used.add(best["id"])

    total = sum(m["dish"]["kcal"] for m in day_menu)
    if total < person_kcal * 0.92:
        snacks = filter_safe(
            get_by_meal_type("перекус"),
            allergens=allergens,
            dislikes=dislikes,
            exclude_ids=exclude_ids,
        )
        if snacks and "перекус" not in slots:
            best_snack = max(snacks, key=lambda d: d["kcal"])
            day_menu.append({"slot": "перекус", "dish": best_snack})
            recently_used.add(best_snack["id"])
    return day_menu


def _collect_dish_ids(plan):
    """Собрать все id блюд из плана."""
    out = []
    for entry in plan:
        for m in entry.get("meals", []):
            d = m.get("dish") or {}
            if d.get("id"):
                out.append(d["id"])
    return out


def generate_calorie_plan(req):
    """Собрать план для режима 'с калоражем'.

    req: dict с полями:
      family_size, persons (list of {kcal, meals}), days, budget, allergens, dislikes
    """
    family_size = int(req.get("family_size", 1))
    persons = req.get("persons", [])
    if not persons:
        persons = [{"kcal": 1500, "meals": 5}]
    days = int(req.get("days", 7))
    _budget = req.get("budget")
    try:
        budget = int(_budget) if _budget else None
    except (TypeError, ValueError):
        budget = None
    allergens = req.get("allergens", [])
    dislikes = req.get("dislikes", [])
    exclude_ids = set(req.get("exclude_ids") or [])

    plan = []
    recently_used = set()
    total_cost = 0
    for person_idx in range(family_size):
        person = persons[person_idx] if person_idx < len(persons) else persons[-1]
        for day in range(1, days + 1):
            day_menu = _generate_day_menu(
                person_kcal=int(person["kcal"]),
                meals_per_day=int(person["meals"]),
                allergens=allergens,
                dislikes=dislikes,
                recently_used=recently_used,
                exclude_ids=exclude_ids,
            )
            if day_menu is None:
                return {"error": f"Не удалось собрать меню: слишком жёсткие ограничения (аллергены/нелюбимое). Попробуй убрать часть ограничений."}
            plan.append({"person": person_idx + 1, "day": day, "meals": day_menu})

    shopping = aggregate_shopping(plan)
    summary = compute_summary(plan, shopping, budget)

    return {"plan": plan, "shopping": shopping, "summary": summary, "mode": "calorie"}


def generate_budget_plan(req):
    """Собрать план для режима 'по бюджету' (без калоража)."""
    budget = int(req.get("budget", 5000))
    people = req.get("people", "1")
    days = int(req.get("days_b", 7))
    meal_types = req.get("meal_types", ["обед", "ужин"])
    excludes = req.get("excludes", [])
    other = req.get("exclude_other", "")
    exclude_ids = set(req.get("exclude_ids") or [])

    people_n = int(people) if people != "5+" else 5

    plan = []
    recently_used = set()
    slots_per_day = meal_types if len(meal_types) >= 1 else ["обед", "ужин"]
    last_meal_per_slot = {}
    for day in range(1, days + 1):
        day_menu = []
        for slot in slots_per_day:
            candidates = filter_safe(
                get_by_meal_type(slot),
                excludes=excludes,
                exclude_ids=exclude_ids,
            )
            if not candidates:
                return {"error": f"Нет блюд типа '{slot}' без исключений."}
            # Бюджетный режим: оптимизируем по цене и разнообразию
            # variety penalty: сильно штрафуем недавние блюда
            def key(d):
                cost = d["cost"]
                if d["id"] in recently_used:
                    cost += 200  # сильный штраф
                if last_meal_per_slot.get(slot) == d["id"]:
                    cost += 500  # не ставим то же самое, что вчера
                return cost
            best = min(candidates, key=key)
            day_menu.append({"slot": slot, "dish": best, "servings": people_n})
            recently_used.add(best["id"])
            last_meal_per_slot[slot] = best["id"]
        plan.append({"person": "all", "day": day, "meals": day_menu})

    shopping = aggregate_shopping_budget(plan)
    total_cost = sum(s["estimated_cost"] for s in shopping)
    summary = {
        "total_cost": total_cost,
        "budget": budget,
        "fits_budget": total_cost <= budget,
        "people": people,
        "days": days,
        "meals_per_day": len(slots_per_day),
    }
    return {"plan": plan, "shopping": shopping, "summary": summary, "mode": "budget"}


def aggregate_shopping(plan):
    """Суммируем ингредиенты по всем приёмам. Используем реальные цены из прайса."""
    agg = defaultdict(lambda: {"amount": 0, "unit": "г", "cost": 0.0, "missing_price": False})
    person_count = max(p["person"] for p in plan) if plan else 1
    for entry in plan:
        for m in entry["meals"]:
            dish = m["dish"]
            for ing in dish["ingredients"]:
                key = ing["name"]
                agg[key]["amount"] += ing["amount"] * person_count
                agg[key]["unit"] = ing["unit"]
                price = get_ingredient_price(key, ing["amount"] * person_count, ing["unit"])
                if price is None:
                    agg[key]["missing_price"] = True
                    agg[key]["cost"] += dish["cost"] * person_count / max(len(dish["ingredients"]), 1)
                else:
                    agg[key]["cost"] += price

    result = []
    for name, info in sorted(agg.items(), key=lambda x: -x[1]["cost"]):
        result.append({
            "name": name,
            "total_amount": round(info["amount"], 1),
            "unit": info["unit"],
            "estimated_cost": round(info["cost"]),
            "price_estimated": info["missing_price"],
        })
    return result


def aggregate_shopping_budget(plan):
    """Аналогично, но с учётом servings (порции на всех)."""
    agg = defaultdict(lambda: {"amount": 0, "unit": "г", "cost": 0.0, "missing_price": False})
    for entry in plan:
        for m in entry["meals"]:
            dish = m["dish"]
            servings = m.get("servings", 1)
            for ing in dish["ingredients"]:
                key = ing["name"]
                agg[key]["amount"] += ing["amount"] * servings
                agg[key]["unit"] = ing["unit"]
                price = get_ingredient_price(key, ing["amount"] * servings, ing["unit"])
                if price is None:
                    agg[key]["missing_price"] = True
                    agg[key]["cost"] += dish["cost"] * servings / max(len(dish["ingredients"]), 1)
                else:
                    agg[key]["cost"] += price

    result = []
    for name, info in sorted(agg.items(), key=lambda x: -x[1]["cost"]):
        result.append({
            "name": name,
            "total_amount": round(info["amount"], 1),
            "unit": info["unit"],
            "estimated_cost": round(info["cost"]),
            "price_estimated": info["missing_price"],
        })
    return result


def compute_summary(plan, shopping, budget=None):
    if not plan:
        return {}
    person_count = max(p["person"] for p in plan)
    days = max(p["day"] for p in plan)
    total_kcal_per_day = 0
    total_b = 0
    total_j = 0
    total_u = 0
    # Берём первый день как образец
    first_day = next(p for p in plan if p["person"] == 1 and p["day"] == 1)
    for m in first_day["meals"]:
        d = m["dish"]
        total_kcal_per_day += d["kcal"]
        total_b += d["protein"]
        total_j += d["fat"]
        total_u += d["carbs"]
    total_cost = sum(s["estimated_cost"] for s in shopping)
    return {
        "people": person_count,
        "days": days,
        "kcal_per_day_per_person": total_kcal_per_day,
        "protein_per_day": total_b,
        "fat_per_day": total_j,
        "carbs_per_day": total_u,
        "total_cost": total_cost,
        "budget": budget,
        "fits_budget": (budget is None) or (total_cost <= budget),
    }


def format_telegram_message(result):
    """Собрать красивое текстовое сообщение для отправки в Telegram."""
    if "error" in result:
        return f"⚠️ {result['error']}"

    lines = []
    if result["mode"] == "calorie":
        s = result["summary"]
        lines.append("🍽 *Ваш индивидуальный рацион готов!*")
        lines.append("")
        lines.append(f"👥 Семья: {s['people']} чел. · 📅 {s['days']} дней")
        lines.append(f"🔥 На человека в день: *{s['kcal_per_day_per_person']} ккал*")
        lines.append(f"Б{s['protein_per_day']} · Ж{s['fat_per_day']} · У{s['carbs_per_day']}")
        if s.get("budget"):
            ok = "✅ укладывается" if s["fits_budget"] else "⚠️ чуть выше"
            lines.append(f"💰 Бюджет: {s['budget']} ₽ · итог ≈ {s['total_cost']} ₽ ({ok})")
        lines.append("")
    else:
        s = result["summary"]
        lines.append("🍽 *Меню на неделю по бюджету готово!*")
        lines.append("")
        lines.append(f"👥 Семья: {s['people']} чел. · 📅 {s['days']} дней · {s['meals_per_day']} приёма/день")
        ok = "✅ укладывается" if s["fits_budget"] else "⚠️ чуть выше"
        lines.append(f"💰 Бюджет: {s['budget']} ₽ · итог ≈ {s['total_cost']} ₽ ({ok})")
        lines.append("")

    # Меню по дням
    lines.append("📋 *Меню*")
    lines.append("")
    current_person = None
    current_day = None
    for entry in result["plan"]:
        if entry["person"] != current_person:
            current_person = entry["person"]
            lines.append(f"👤 *Человек {current_person}*")
            current_day = None
        if entry["day"] != current_day:
            current_day = entry["day"]
            lines.append("")
            lines.append(f"*День {current_day}*")
        for m in entry["meals"]:
            d = m["dish"]
            slot_emoji = {"завтрак": "🌅", "обед": "🍲", "ужин": "🌙", "перекус": "🍎"}[m["slot"]]
            lines.append(f"  {slot_emoji} {m['slot'].capitalize()}: *{d['name']}* — {d['kcal']} ккал · Б{d['protein']} Ж{d['fat']} У{d['carbs']}")

    # Список покупок
    lines.append("")
    lines.append("🛒 *Список продуктов*")
    lines.append("_(цены примерные, для Азова — Магнит / Пятёрочка / Ашан)_")
    lines.append("")
    for s in result["shopping"]:
        lines.append(f"• {s['name']} — {format_amount(s['total_amount'], s['unit'])} (~{s['estimated_cost']} ₽)")

    lines.append("")
    lines.append("_Готовь с удовольствием! Если что-то не подходит — напиши @Olesya2042 🙌_")
    return "\n".join(lines)


def format_amount(amount, unit):
    """Красиво отформатировать количество."""
    if unit == "г" and amount >= 1000:
        return f"{amount/1000:.1f} кг"
    if unit == "мл" and amount >= 1000:
        return f"{amount/1000:.1f} л"
    return f"{amount:g} {unit}"


if __name__ == "__main__":
    # Тест
    test_req = {
        "family_size": 1,
        "persons": [{"kcal": "1500", "meals": 5}],
        "days": 3,
        "budget": 3000,
        "allergens": [],
        "dislikes": [],
    }
    result = generate_calorie_plan(test_req)
    print(format_telegram_message(result))
