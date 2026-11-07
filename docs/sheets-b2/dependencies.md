# Google Sheets B2: предложение пакетов до установки

**Пересмотр 07.11.2026:** предложение ниже **отозвано** после независимого ревью: Python-RSA архивирована
и не сопровождается; wheel-совместимость не означает пригодность этого набора.
Прежние proposed-*.txt сохранены как история, **не выполнять их установку**.
Актуальное сравнение Linux/сборки Intel Mac, рекомендация и состав 33 пакетов —
[../sheets-deps-08/comparison.md](../sheets-deps-08/comparison.md). Принятый офлайн-код не менялся.

Ниже — историческое предложение от 06.11.2026, не текущая рекомендация.

06.11.2026. **Ничего не установлено.** Текущие 24 версии/lock не менялись.
Предлагается официальный Google auth SDK service account → официальный Requests Request для OAuth →
существующий httpx 0.28.1 для REST Sheets. Самописных JWT/OAuth/криптографии нет.

## Ограничение и решение для ревью

Текущий [google-auth 2.62.1](https://pypi.org/pypi/google-auth/2.62.1/json) требует cryptography.
У [cryptography 50.0.2](https://pypi.org/pypi/cryptography/50.0.2/json) не найден совместимый macOS Intel wheel,
а последний найденный с подходящими тегами [48.0.1](https://pypi.org/pypi/cryptography/48.0.1/json)
имеет опубликованные advisories. Этот набор к установке не предлагается. Компиляция/Rust/Homebrew/смена Python/OS
не выполнялись и не включаются в нынешнее предложение.

**Условный кандидат для согласования — google-auth 2.43.0 с его официальным RSA signer.**
Это более ранний SDK, не последняя версия и не заявление о текущей поддержке/отсутствии всех уязвимостей.
Его версия и риск использования старой ветки должны быть отдельно приняты координатором/владельцем до установки.
При непринятии этого ограничения установка останавливается: автоматической замены на cryptography 48 или сборку нет.
Проверка PyPI vulnerability list не является аудитом безопасности SDK/RSA.

## Точные прямые и транзитивные версии кандидата

Прямые новые зависимости — google-auth и requests, без extras. Оба нужны серверной CLI авторизации;
Sheets API SDK/Drive/gspread/discovery/httplib2 не нужны. Остальные шесть — обязательные транзитивные.

| Новый пакет | Версия | Назначение / метаданные |
|---|---|---|
| google-auth | 2.43.0 | Официальный service account SDK: ключ/подпись/обмен OAuth внутри библиотеки · [PyPI](https://pypi.org/pypi/google-auth/2.43.0/json) |
| requests | 2.34.2 | Готовый транспорт google.auth.transport.requests.Request только для OAuth · [PyPI](https://pypi.org/pypi/requests/2.34.2/json) |
| cachetools | 6.2.2 | Требование SDK cachetools>=2,<7 · [PyPI](https://pypi.org/pypi/cachetools/6.2.2/json) |
| pyasn1-modules | 0.4.2 | Структуры ASN.1 для SDK · [PyPI](https://pypi.org/pypi/pyasn1-modules/0.4.2/json) |
| pyasn1 | 0.6.5 | Обязателен pyasn1-modules>=0.6.1,<0.7 и RSA · [PyPI](https://pypi.org/pypi/pyasn1/0.6.5/json) |
| rsa | 4.9.1 | Готовая реализация подписи, используемая SDK; код D4 RSA не реализует · [PyPI](https://pypi.org/pypi/rsa/4.9.1/json) |
| charset-normalizer | 3.5.2 | Обязателен Requests>=2,<4 · [PyPI](https://pypi.org/pypi/charset-normalizer/3.5.2/json) |
| urllib3 | 2.8.0 | Транспорт Requests, допустим >=1.26,<3 · [PyPI](https://pypi.org/pypi/urllib3/2.8.0/json) |

Из уже установленных переиспользуются certifi 2026.10.23 (Requests>=2023.5.7), idna 3.20 (>=2.5,<4),
httpx 0.28.1 и его существующие зависимости. **24 прежних + 8 новых = 32**, обновлений/удалений прежних не требуется
по проверенным метаданным. Bootstrap pip/setuptools не входят в эти 32.

Полный кандидат — [proposed-environment.txt](proposed-environment.txt), новые восемь — proposed-additions.txt.
dependency-metadata.json фиксирует requires_python/requires_dist, активные marker-рёбра, совместимые wheel filenames/SHA-256.
Проверены CPython 3.11.8, macOS 15.8.2 x86_64: условия Python и версии каждого активного требования,
наличие хотя бы одного незабракованного wheel с пересечением packaging.tags.sys_tags.
Опциональные extras не выбраны, скрытых дополнительных пакетов из них не включено.

Это полный **метаданный кандидат**, не установленный совместно проверенный lock. Pip resolver/install/download/dry-run
не запускались, wheel не скачивались/не импортировались. Совместные импорты, подпись/refresh и runtime-поведение SDK
до отдельной установки не проверены. Веб/PyPI использовались только для публичных docs/JSON, без OAuth/Sheets/Telegram API.

После отдельного одобрения именно этого набора и его ограничений возможна установка в существующий D4 `.venv`:

```sh
cd /Users/alex/myit/demo/Landing/d4-cleaning
.venv/bin/python -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: -c requirements-lock-macos-py311.txt -r docs/sheets-b2/proposed-environment.txt
.venv/bin/python -m pip check
```

**Команда не выполнялась, разрешения сейчас нет.** Если wheel/resolve не совпадут, остановиться;
не выбирать другую версию/компиляцию/extras. Старый lock сохранять как историю, новый фактический lock формировать
только после успешной разрешённой установки и проверок в отдельном срезе.

## Маршрут авторизации и запрет скрытого повторения

Предлагаемый следующий код (ещё не реализован): явный JSON → строгая проверка type/client_email/token_uri →
`service_account.Credentials.from_service_account_info` с единственным scope spreadsheets, без subject/delegation,
ADC/default()/impersonation, поиска переменных окружения/gcloud и self-signed JWT как обхода token endpoint.
Подпись и OAuth — только SDK. [Контракт service account](https://google-auth.readthedocs.io/en/latest/reference/google.oauth2.service_account.html).

`google.auth.transport.requests.Request` на отдельной Session с trust_env=False, без netrc/proxy/mTLS,
фиксированным POST https://oauth2.googleapis.com/token, allow_redirects=False, явными connect/read timeout и
HTTPAdapter max_retries=0. Минимальная обвязка готового Request ограничит разрешённый endpoint/метод,
один фактический запрос на refresh и размер ответа; это транспортный guard, не собственный OAuth.
[Официальный Requests Request](https://github.com/googleapis/google-auth-library-python/blob/v2.43.0/google/auth/transport/requests.py).

**Один max_retries=0 недостаточен:** SDK имеет собственный token-endpoint retry/backoff.
Нужен отдельный лимит фактических Request-вызовов (второй блокируется), а не подмена внутреннего JWT-кода.
SDK может сделать backoff перед заблокированным повтором; конечный временной бюджет refresh также предстоит проверить.
[Исходник SDK token endpoint](https://github.com/googleapis/google-auth-library-python/blob/v2.43.0/google/oauth2/_client.py).

Не использовать AuthorizedSession для Sheets: он умеет refresh/retry после 401. Явный refresh выполняется
до preflight/append, готовый короткоживущий токен копируется в один HTTPX request; сам HTTPX retries=0/redirects=False.
После 401 никакого неявного refresh и повторения append. Если токен истёк перед append — остановка до записи;
если ошибка после начала записи — unknown и только отдельная read-only сверка.
[AuthorizedSession и max_refresh_attempts](https://google-auth.readthedocs.io/en/latest/reference/google.auth.transport.requests.html).

Весь срок refresh/HTTP защищается от стандартного debug logging; исключения SDK/HTTP не печатать/сохранять целиком.
Токен, private_key, assertion/JWT, serialized/URL/base64 варианты могут оказаться в сообщении исключения.
Существующий B2 проверяет этот контракт искусственным provider; **реальный SDK этим не проверен**.

## Обязательная приёмка после установки, до настоящих ресурсов

1. Импортировать согласованные версии в D4 venv, pip check, сравнить фактический набор 32 с предложением,
   повторить весь D4-набор; при несовпадениях остановиться и согласовать их, не переписывать старый lock.
2. На искусственном ключе/временных данных проверить настоящий service_account SDK с подменой готового Request,
   без сокетов. Подтвердить формат ключа/подпись именно SDK, scope/token_uri/account; чужой JSON и URL отклоняются до SDK.
3. Посчитать фактические OAuth HTTP-вызовы при 200/401/429/5xx/timeout: не более одного refresh-запроса,
   конечные таймауты и отсутствие скрытых повторов HTTPAdapter/SDK. Не использовать реальные credentials.
4. В Sheets-контракте отдельно доказать: 401/истечение не приводят к refresh+повторному append,
   сбой чтения после append оставляет unknown; reconcile никогда не вызывает POST.
5. Проверить настоящие SDK исключения/логи/stdout/stderr/traceback на уникальных искусственных private_key,
   bearer, JWT/assertion и кодированных вариантах. Маскирование фиктивного provider не выдавать за эту проверку.
6. Только затем отдельные решения о ресурсе/правах, чтении конкретного ключа и одном live Sheets-тесте.
   Telegram повторно не отправлять. Внешнее хранение/удаление строки — отдельное согласование.
