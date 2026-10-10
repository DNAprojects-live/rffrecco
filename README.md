# Recommendation Bot

Discord-бот для системы рекомендаций (Roblox + Bloxlink).

## Команды
- `/recommendation user [roblox]` — рекомендовать игрока (только роль Player). 1/3, 2/3 -> при 3/3 заявка уходит в админ-канал с кнопками Approve / Deny.
- `/info [user] [roblox]` — скин, ник, ID Roblox, роли на сервере, статус рекомендаций.

- `/links` — ссылки из `.env` (`LINK_1=Название|https://...`).

Игроку приходят ЛС: когда его порекомендовали, когда набралось 3/3 и когда заявку приняли или отклонили.

## Запуск
1. Python 3.10+
2. Скопируй `.env.example` в `.env` и заполни значения
3. В Developer Portal включи Server Members Intent
4. `pip install -r requirements.txt`
5. `python bot.py`

Роль бота должна стоять выше роли Player (иначе не сможет её выдавать).
Данные хранятся в `reco.db` (создаётся сам).
