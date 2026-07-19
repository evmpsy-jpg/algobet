# АлгоБет — архитектура Stage 5.1

Поток данных: `Excel → ParsedMatch → MatchData → SignalDecision → Signal → Telegram`.

- `app/services/excel_parser.py` читает исходный файл.
- `app/services/match_normalizer.py` переводит буквы Excel в предметные поля.
- `app/rules/engine.py` применяет правила и сохраняет трассировку решения.
- `app/signals/builder.py` создаёт независимый объект Signal.
- `app/signals/formatter.py` формирует сообщение по шаблону.
- Telegram-обработчики не содержат бизнес-логики.

Совместимость со Stage 4 сохранена через `app/services/signal_rules.py`.
