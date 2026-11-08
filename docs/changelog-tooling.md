# Общий parser CHANGELOG (FIN-457)

`common/scripts/changelog.py` сохраняет формат и команды сервисного parser: `check`,
`latest`, `notes vX.Y.Z`. Это перенос механики, а не новая release/version policy.

```sh
python3 <common>/scripts/changelog.py --root <consumer-root> check
python3 <common>/scripts/changelog.py --root <consumer-root> latest
python3 <common>/scripts/changelog.py --root <consumer-root> notes v0.2.0
```

`--root` обязателен и должен быть абсолютным путём: читается ровно `<consumer-root>/CHANGELOG.md`, независимо от cwd
или расположения parser в pinned cache go-ci. API `main(argv)` сохраняет argv[0];
`parse`, `problems`, `latest`, `notes` и `version_key` остаются чистыми функциями.
Коды CLI сохраняются: 0 — успех, 1 — ошибка check/отсутствующая notes секция, 2 — неверная
команда или аргументы. При неверных аргументах (включая отсутствующий, пустой или
относительный `--root`) argparse завершает `main(argv)` через `SystemExit(2)`.
Требуются Python 3.11+, стандартная библиотека; pip parser не нужен.

В каждом из шести сервисных потребителей `scripts/changelog.py` можно сохранить тонким
CLI wrapper с прежними командами. Он определяет consumer root по своему пути, вызывает
существующий неизменённый `scripts/common_ci.py`, передаёт root общему parser и отключает
запись bytecode при исполнении verified source cache. Поведение local/CI следует одному
полному SHA из существующего manifest; rollback возвращает согласованные pins.

Общие `scripts/test_changelog.py` заменяются `common/tests/test_changelog.py` в source.
Формат, malformed headings, порядок/дубликаты/пустые notes и независимость consumer root
проверяются executable tests с process timeouts. Публикация GitHub Release и reusable
workflow подключаются отдельной итерацией; parser не создаёт теги или releases.
