---
name: cloak-profile-registration
description: |
  Применяй, когда оператор просит зарегистрировать или первично настроить
  одну авторизованную учётную запись через отдельный Cloak-профиль: с
  переданным proxy или явно выбранным proxy pool, humanized browser-вводом
  и понятным ручным шагом для CAPTCHA/MFA.
metadata:
  hermes:
    category: browser
    tags: [cloak, profile, registration, proxy, browser, humanize, captcha]
    related_skills: [cloak-proxy-pool]
---

# Регистрация через Cloak-профиль

Используй этот skill для одной учётной записи, когда оператор имеет право
автоматизировать регистрацию на целевом сервисе. Новый профиль сохраняет
сессию и назначенный proxy отдельно от остальных задач.

## Границы

- Работай только с URL и данными учётной записи, которые дал оператор через
  разрешённый канал. Не придумывай личности, адреса, номера телефонов,
  почтовые ящики или платёжные данные.
- Один запуск — один новый профиль и одна регистрация. Не превращай этот
  workflow в массовый или параллельный поток.
- Не показывай в ответах пароль, токены CAPTCHA, CDP URL или учётные данные
  proxy. Не проси вставлять API-ключи CAPTCHA в чат.
- Если для задачи не хватает обязательных данных или разрешения оператора,
  задай один точный вопрос до создания профиля.

## Выбор proxy

1. Если оператор передал конкретный proxy, передай его только как
   `proxy` в `cloak_create_profile`.
2. Если оператор явно попросил proxy из общего пула, передай только
   `use_pool: true`. `cloak_create_profile` сам атомарно резервирует proxy
   для имени профиля.
3. Если оператор не попросил proxy и не дал адрес, не включай pool молча.
4. Не вызывай заранее `cloak_proxy_pool(action="next")` или `pool.py next`
   перед `cloak_create_profile(use_pool=true)`: это создаст лишнюю
   резервацию.

При `proxy_unavailable` или ошибке создания остановись и сообщи компактную
причину. Не подменяй proxy и не переходи на локальный браузер молча.

## Основной workflow

### 1. Создай новый профиль

Имя должно быть уникальным и понятным, например `reg-<короткий-id-задачи>`.
Создавай профиль явным вызовом, а не `cloak_set_active`, чтобы не
переиспользовать профиль с совпадающим именем.

~~~text
cloak_create_profile(
  name="reg-<task-id>",
  use_pool=false,
  humanize=true,
  human_preset="default",
  headless=false,
  geoip=true,
  tags=[{"tag": "registration"}]
)
~~~

При явном proxy добавь `proxy=...` и оставь `use_pool=false`. Только при явной просьбе взять proxy из пула замени `use_pool=false` на `use_pool=true`. Сохрани
возвращённый `launch_next_with_profile_id`.

### 2. Запусти именно созданный профиль

~~~text
cloak_launch(profile=<launch_next_with_profile_id>)
~~~

Не включай `allow_profile_switch`. Если запуск не подтвердил `active: true`,
не начинай браузерные действия.

### 3. Пусть модель ведёт браузер через UI

1. Сначала сними `browser_snapshot` или `browser_screenshot`.
2. Открой целевой URL через `browser_navigate`.
3. После каждого значимого изменения снова наблюдай страницу.
4. Для действий используй `browser_click`, `browser_type`, `browser_fill`,
   `browser_hover`, `browser_press`, `browser_scroll` и `browser_drag`.
   В Cloak они уже проходят через humanized input overrides.
5. Для email, имени пользователя и пароля всегда передавай CSS selector и
   `verify=true` у `browser_type`/`browser_fill`. Не передавай `@e…` ref:
   Cloak намеренно остановит такой ввод вместо перехода в обычный браузер.
6. Если текстовый ввод вернул `humanized_selector_required` или ошибку CDP,
   не используй native browser, `browser_eval` или прямой DOM-fill. Снова
   наблюдай форму, выбери стабильный CSS selector и повтори humanized-ввод.

Модель должна читать интерфейс и кликать по нему сама. Не добавляй
page-specific скрипты, прямую инъекцию токенов или обходные сценарии вместо
обычного UI-потока.

## CAPTCHA и ручные проверки

Вызывай `cloak_detect_captcha` только когда challenge виден или страница не
пускает дальше. Если `kind` не равен `null`, передай `kind`, `site_key`, `extra` и `url=<detected page_url>` в `cloak_solve_captcha`.

Поддержанные runtime-провайдеры на сегодня:

- CapSolver: `CAPSOLVER_API_KEY`;
- 2Captcha: `TWO_CAPTCHA_API_KEY` или `TWOCAPTCHA_API_KEY`;
- выбор: `CAPTCHA_PROVIDER=auto`, `capsolver` или `2captcha`.

`cloak_solve_captcha` возвращает токен либо
`MANUAL_INTERVENTION_REQUIRED`. Возвращённый токен не завершает CAPTCHA: generic tool не применяет его к странице. Не пытайся применять токен через JavaScript или page-specific скрипт. Используй только штатный, видимый UI-путь сервиса. Если такого пути нет, а также для
email-кода, SMS, MFA, документов или любого ручного подтверждения, останови
поток без циклов и передай оператору причину и VNC/страницу. В Kanban-контексте
вызови `kanban_block(reason=...)`.

Не обещай поддержку provider'ов, которые только перечислены в шаблоне
окружения: для generic router сейчас подключены только CapSolver и 2Captcha.

## Завершение

- При успехе сохрани профиль и его proxy-привязку: не вызывай `cloak_stop`
  автоматически, чтобы не потерять готовую сессию или освободить proxy из
  пула.
- При явной просьбе закрыть или остановить профиль используй
  `cloak_stop(profile=...)`; он освобождает pool reservation.
- В результате сообщи только: статус, имя/ID профиля, был ли задан proxy,
  и нужен ли ручной следующий шаг. Не раскрывай секреты.
