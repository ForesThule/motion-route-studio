# Motion Route Studio

Локальная Python-утилита для эмуляции GPS-движения в `Android Emulator` и `iOS Simulator` с картой, кривыми скорости и управлением профилем движения.

Для Android используется `adb emu geo fix`, для iOS — `xcrun simctl location set`.

## Что умеет

- web UI с картой и редактором маршрута
- CLI для сценариев, автоматизации и `dry-run`
- выбор активного `Android Emulator` из `adb devices`
- выбор booted `iOS Simulator` из `xcrun simctl list devices available --json`
- построение маршрута кликами по карте или вручную по координатам
- отдельные кривые старта и остановки
- расчёт маршрута по длительности или по средней скорости
- фиксация скорости на отдельных отрезках
- периодическая модуляция общей скорости по кривой, частоте и амплитуде
- предпросмотр профиля движения и живой лог выполнения

## Интерфейс

Новый интерфейс собран вокруг сценария “маршрут -> устройство -> движение”:

- карта как главный рабочий экран
- sticky-сводка по маршруту, времени и профилю скорости
- отдельный блок устройства и ручного override ID
- продвинутые speed-настройки в раскрываемой секции
- быстрые действия на карте: вписать маршрут, отменить последнюю точку, очистить маршрут

## Поддерживаемые кривые

- `linear`
- `ease-in`
- `ease-out`
- `ease-in-out`
- `smoothstep`
- `smootherstep`
- `sine`

## Требования

- `Python 3.9+`
- `adb` в `PATH` для Android
- `xcrun` / `Xcode Command Line Tools` для iOS
- запущенный `Android Emulator` или booted `iOS Simulator`
- браузер для открытия локального UI по адресу `http://127.0.0.1:<port>`

## Быстрый старт

Показать список кривых:

```bash
python3 android_motion_emulator.py --list-curves
```

Запустить локальный UI:

```bash
python3 android_motion_emulator.py --gui
```

По умолчанию интерфейс поднимется на `http://127.0.0.1:8765`.

Если порт занят:

```bash
python3 android_motion_emulator.py --gui --port 8877
```

## Работа через UI

1. Откройте локальный адрес из терминала.
2. Нажмите `Обновить список` и выберите платформу.
3. Выберите активное устройство или задайте `ID` вручную.
4. Добавьте точки кликами по карте.
5. Выберите режим `По длительности` или `По скорости`.
6. При необходимости откройте продвинутую настройку скорости.
7. Нажмите `Предпросмотр` или `Запустить`.

## CLI-примеры

Проверить маршрут с отдельными кривыми старта и остановки:

```bash
python3 android_motion_emulator.py \
  --point 37.4219999,-122.0840575 \
  --point 37.4225000,-122.0835000 \
  --point 37.4232000,-122.0827000 \
  --duration 90 \
  --interval 1 \
  --start-curve ease-in \
  --stop-curve ease-out \
  --start-share 0.25 \
  --stop-share 0.20 \
  --dry-run
```

Рассчитать маршрут по средней скорости:

```bash
python3 android_motion_emulator.py \
  --point 37.4219999,-122.0840575 \
  --point 37.4225000,-122.0835000 \
  --speed-kmh 24 \
  --interval 1 \
  --start-curve ease-in \
  --stop-curve ease-out \
  --start-share 0.25 \
  --stop-share 0.20 \
  --dry-run
```

Задать скорость на конкретном отрезке:

```bash
python3 android_motion_emulator.py \
  --point 37.4219999,-122.0840575 \
  --point 37.4225000,-122.0835000 \
  --point 37.4232000,-122.0827000 \
  --duration 30 \
  --interval 2 \
  --segment-speed 1:12 \
  --dry-run
```

Добавить периодическую модуляцию скорости:

```bash
python3 android_motion_emulator.py \
  --point 37.4219999,-122.0840575 \
  --point 37.4225000,-122.0835000 \
  --speed-kmh 24 \
  --interval 2 \
  --variation-curve sine \
  --variation-frequency 0.25 \
  --variation-amplitude 20 \
  --dry-run
```

Запустить маршрут в конкретный Android Emulator:

```bash
python3 android_motion_emulator.py \
  --serial emulator-5554 \
  --point 37.4219999,-122.0840575 \
  --point 37.4225000,-122.0835000 \
  --point 37.4232000,-122.0827000 \
  --duration 90 \
  --interval 1 \
  --start-curve smoothstep \
  --stop-curve ease-out \
  --start-share 0.30 \
  --stop-share 0.20
```

Запустить тот же маршрут в iOS Simulator:

```bash
python3 android_motion_emulator.py \
  --platform ios \
  --device-id 80131754-C016-41AB-8B65-824304B91EDD \
  --point 37.4219999,-122.0840575 \
  --point 37.4225000,-122.0835000 \
  --point 37.4232000,-122.0827000 \
  --duration 90 \
  --interval 1 \
  --start-curve smoothstep \
  --stop-curve ease-out \
  --start-share 0.30 \
  --stop-share 0.20
```

## Профиль движения

- `start-curve` управляет разгоном
- `stop-curve` управляет торможением
- `start-share` задаёт долю времени на стартовую фазу
- `stop-share` задаёт долю времени на завершающую фазу
- остальная часть маршрута проходит по базовой скорости

Если включены скорости по сегментам или общая модуляция, итоговая длительность рассчитывается по фактическому speed-profile и может отличаться от базовой `duration`.

## Что есть в UI

- локализуемая карта без API key
- выбор платформы `Android / iOS`
- список активных устройств
- ручной override идентификатора устройства
- список точек маршрута с ручным редактированием координат
- управление порядком точек
- выбор длительности, средней скорости и интервала
- выбор кривой старта и остановки
- скорость на отдельных сегментах
- периодическая модуляция всей траектории
- предпросмотр маршрута и живой лог выполнения

## Ограничения и заметки

- `geo fix` задаёт координаты, а не настоящую GNSS-телеметрию скорости
- реалистичность движения зависит от частоты отправки точек и плотности маршрута
- при нескольких Android-эмуляторах лучше явно указывать `--serial`
- очень маленький `--interval` может заметно нагрузить `adb`
- карта использует тайлы OpenStreetMap, не стоит использовать UI для массовой предзагрузки тайлов
- для iOS отображаются только booted симуляторы
- `altitude` применяется только к Android Emulator

## Файл проекта

- [android_motion_emulator.py](/Users/ve/Documents/New%20project/android_motion_emulator.py)

