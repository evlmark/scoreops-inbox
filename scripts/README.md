# Ночная выгрузка чатов (Snowflake → ScoreOPS)

Автоматический ежедневный импорт чатов PyME из Snowflake в Real Inbox.

## Как это работает

```
launchd (05:00 по Мехико, локально на Маке)
  └─ ~/.scoreops/pull_chats.py
       ├─ поднимает plata-mcp (stdio MCP) → Superset.exportSql(env=dwh, db=1)  → CSV  (SSO из Keychain)
       ├─ POST /admin/import-csv         → conversations (дедуп по TASK_ID)
       └─ POST /admin/process-conversations (цикл) → перевод + оценка по базе знаний
```

Утром чаты за прошлый день уже импортированы, оценены и видны в дашборде (Real Inbox).

## Почему это локально, а не в облаке

Snowflake доступен только через `plata-mcp` — а он требует **твою SSO-сессию (Keychain) + VPN**.
Railway (облако) до Snowflake достучаться не может. Поэтому выгрузка живёт на Маке.

## Установка

См. шапку `com.scoreops.chatpull.plist.template`. Кратко:
исполняемая копия скрипта кладётся в `~/.scoreops/` (НЕ в `~/Downloads` — там macOS TCC
запрещает launchd читать файл), плист — в `~/Library/LaunchAgents/`.

После изменения `pull_chats.py` не забудь обновить копию:
```
cp scripts/pull_chats.py ~/.scoreops/pull_chats.py
```

## Запуск вручную

```
python3 ~/.scoreops/pull_chats.py                 # вчерашний день
python3 ~/.scoreops/pull_chats.py --date 2026-05-31
python3 ~/.scoreops/pull_chats.py --from 2026-05-29 --to 2026-06-01   # [from, to)
python3 ~/.scoreops/pull_chats.py --date 2026-05-31 --no-process      # только импорт
```

Лог: `~/.plata-mcp/scoreops-pull/pull.log`

## Ограничения / на что смотреть

- **VPN** должен быть поднят в 05:00 (иначе Superset/Snowflake недоступны).
- **Мак должен не спать.** launchd запустит пропущенную задачу при пробуждении, но не разбудит сам.
  При желании — будить Мак: `sudo pmset repeat wake MTWRFSU 00:58:00`.
- **SSO истекает.** plata-mcp сам обновляет сессию, пока жив refresh-токен. Когда он умрёт —
  ночной прогон упадёт с ошибкой экспорта и покажет macOS-уведомление; нужно один раз
  выполнить `plata-mcp login`.
- **Звонки (CALL_TEXT) пока не выгружаются** — join со `whisperx_cs_dataset` слишком тяжёлый
  (не укладывается в таймаут), и текущий импортёр их всё равно не парсит. Берём только чаты.
- Дедуп — по `TASK_ID` (PK). Повторная выгрузка тех же дат не создаёт дублей.
```
