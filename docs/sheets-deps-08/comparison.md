# Пересмотр Google-зависимостей: Linux и Intel Mac

07.11.2026 · `d4-tihiy-dom-sheets-deps-08`. Только предложение среды/пакетов для согласования.
Офлайн-код B2-07 принят координатором; здесь он не изменяется. Установки, сборки, создание среды,
перенос реестра, чтение секретов и Google/Telegram вызовы не выполнялись.

## Рекомендация и исправление прошлого предложения

**Рекомендую актуальный Google auth SDK с готовыми wheel в отдельно согласованной Linux x86_64-среде.**
Linux пока не выбран владельцем, не подготовлен и не разрешён к созданию. Сборку cryptography для Intel Mac
не рекомендую как основной путь: она не возвращает поддержку платформы, удалённую upstream.

Предложение google-auth 2.43.0 + rsa 4.9.1 **отозвано**, его прежние proposed-*.txt не являются инструкцией установки.
Python-RSA официально архивирована и не получает обновлений. В прошлой передаче я ограничился общей оговоркой
о старой ветке SDK и пропустил этот существенный факт. Отсутствие advisories не заменяет сопровождение.
[Сообщение сопровождающего на PyPI](https://pypi.org/project/rsa/4.9.1/).

С cryptography 49.0.0 удалена поддержка macOS x86_64, а не только публикация wheel.
Собственный успешный бинарник не станет поддерживаемой upstream конфигурацией.
[Changelog 49.0.0](https://cryptography.io/en/50.0.2/changelog/#v49-0-0).

| Критерий | Linux x86_64, готовые wheel — рекомендуется | Intel Mac, собственная сборка — эксперимент |
|---|---|---|
| Google auth / cryptography | 2.62.1 / 50.0.2 | Те же версии |
| Поддержка cryptography | Debian 12 x86_64 входит в проверяемые upstream платформы | x86_64 macOS удалена из поддержки с 49 |
| Wheel для полного Python-набора | 33/33 по метаданным | 32/33; cryptography надо собрать |
| Новая инфраструктура | Согласовать Linux-хост/VM и способ изоляции | Отдельный builder и тестовый venv на Mac |
| Инструменты сборки расширений | Не нужны при принятии всех wheel | Apple CLT/SDK, C compiler/linker, Rust/Cargo, OpenSSL, make/Perl |
| Сопровождение бинарников | Обновлять согласованные релизы/wheel и тестировать | Дополнительно хранить и пересобирать свой wheel, native stack и provenance |
| Главная непроверенная часть | Установка/реальный SDK и переносимость всего D4 | Сам факт сборки и работоспособность на неподдерживаемой платформе |

Основание матрицы — [платформы и установка cryptography 50.0.2](https://cryptography.io/en/50.0.2/installation/),
changelog и собственная проверка JSON PyPI. Матрица — инженерная рекомендация, не результат запуска двух сред.

## Точный Python-набор для обоих вариантов

Повторно проверены последние стабильные релизы на PyPI: **google-auth 2.62.1**, **cryptography 50.0.2**.
Для CPython 3.11 у SDK обязательна cryptography; RSA/cachetools в новый набор не входят.
[Google auth JSON](https://pypi.org/pypi/google-auth/2.62.1/json),
[cryptography JSON](https://pypi.org/pypi/cryptography/50.0.2/json).

| Добавляемый пакет | Версия | Роль |
|---|---|---|
| google-auth | 2.62.1 | Прямая: официальный SDK service account, подпись и OAuth внутри SDK |
| requests | 2.34.2 | Прямая: готовый google.auth.transport.requests.Request для OAuth |
| cryptography | 50.0.2 | Обязательная зависимость Google auth для CPython 3.11 |
| pyasn1-modules | 0.4.2 | Обязательная зависимость Google auth |
| pyasn1 | 0.6.5 | Зависимость pyasn1-modules |
| cffi | 2.1.1 | Зависимость cryptography на CPython |
| pycparser | 3.12 | Зависимость CFFI |
| charset-normalizer | 3.5.2 | Зависимость Requests |
| urllib3 | 2.8.0 | Зависимость Requests |

Полный состав: **прежние 24 точные версии + эти 9 = 33 пакета**, без замены прежних.
Requests переиспользует certifi 2026.10.23 и idna 3.20; REST Sheets использует уже имеющийся httpx 0.28.1.
Extras, Google API client/gspread, Drive, RSA, cachetools, Redis/Celery не добавляются.

- [runtime-33.txt](runtime-33.txt) — все 33 версии, включая прежние транзитивные, одинаковый Python-набор двух вариантов.
- [additions-9.txt](additions-9.txt) — только девять добавлений.
- [metadata.json](metadata.json) — источники каждого из 33 пакетов, requires_python/requires_dist, активные рёбра
  зависимостей отдельно для Linux/Mac, подходящие wheel filenames/SHA-256 и sdist hash.

Это полный **метаданный кандидат, не установленный lock**. Проверены активные условия/версии и теги wheel,
не запускались pip resolver/dry-run/install, импорты нового SDK, скачивание wheel/sdist и выполнение их кода.
Отсутствие advisories в ответе PyPI не является аудитом безопасности.
Исторический requirements-lock-macos-py311.txt остаётся прежним и не превращается в Linux-lock переименованием.

## Вариант A: подходящая Linux-среда

Предлагаемый конкретный профиль: **Debian 12 Bookworm, glibc 2.36, x86_64, CPython 3.11.18**.
Версия glibc подтверждена [метаданными libc6 Bookworm](https://packages.debian.org/bookworm/libc6);
конкретная security-ревизия пакета будущего образа ещё не зафиксирована.
Вариант готовой основы — официальный `python:3.11.18-slim-bookworm`, а не сборка Python на Mac.
Официальный [Dockerfile Python](https://github.com/docker-library/python/blob/master/3.11/slim-bookworm/Dockerfile)
указывает Bookworm и 3.11.18. Его чтение не подтверждает скачивание/наличие конкретного image manifest.
Это пример основы, не требование установить Docker Desktop и не разрешение контейнера/VM.

Проверка metadata.json моделирует CPython 3.11, обычный cp311 ABI и manylinux x86_64 с glibc до 2.36;
py3-none-any и совместимые abi3 также учтены. Все 33 версии имеют подходящий wheel.
Это не проверка загрузчика ELF, реального CPU, glibc, библиотек и SQLite будущей среды.

Необходимые действия **после отдельного решения владельца**:

1. Выбрать существующий согласованный Linux-хост либо отдельно разрешить локальную VM/контейнерную среду.
   Наличие такого хоста/движка не проверялось. Зафиксировать OS/архитектуру, точный образ по digest и Python patch;
   tag/master Dockerfile изменяемы и не являются воспроизводимым образом. Ничего не создавать/переносить сейчас.
2. Создать отдельный Linux venv/образ и установить runtime-33 только из wheel, с отказом от сборки.
   Rust, clang, OpenSSL headers и libffi-dev для wheel-пути не нужны. Bootstrap pip/setuptools базового образа
   учитывать отдельно от 33 пакетов, проверить/зафиксировать после выбора digest, не обновлять Mac venv.
3. Для **полного D4**, а не только SDK, обеспечить `/bin/ps` (пакет [procps 2:4.0.2-3 Bookworm](https://packages.debian.org/bookworm/procps)), CA certificates и Node для существующих
   JS/Node VM-регрессий. На Mac принят Node 22.23.4; Linux-дистрибутив Node здесь не проверялся.
   Точные версии OS-пакетов/Node-артефакта и их хэши входят в следующий план среды после выбора основы;
   это предложение пока не является lock всего OS-образа. На python-slim наличие ps/Node не предполагается.
4. Проверить pip check, импорты/фактический OpenSSL, настоящий SDK на искусственном ключе с запретом сокетов,
   счётчики OAuth/HTTP-повторов и маскирование исключений. Затем весь принятый D4-набор: фото/RSS/CPU,
   SQLite/restore, fcntl/flock/конкуренция, CLI и квитанции. 236 тестов ранее приняты на Mac, на Linux не запускались.
5. Если среда принята — сохранить новый фактический Linux-lock и образ/digest в новом срезе.
   Принятый код и Mac-lock не переписывать автоматически. Любая необходимая правка переносимости — отдельная кодовая задача.

Будущая команда установки **в уже отдельно согласованной Linux-среде**, не на текущем Mac:

```sh
python -m pip --isolated install --index-url https://pypi.org/simple --only-binary=:all: -r docs/sheets-deps-08/runtime-33.txt
python -m pip check
```

Команды не выполнялись. Переносить только проверенный исходный срез и искусственные fixtures;
не копировать реальные .env, data/backups или credentials ради проверки зависимостей.
Linux-SDK-проверка не разрешает работать отдельным отправителем с копией рабочего Mac-реестра:
единый владелец реестра/lock и будущая среда всего D4 требуют отдельного решения. Общий SQLite через сетевой диск не предлагается.

## Вариант B: сборка cryptography 50.0.2 на Intel Mac

Это **неподдерживаемая upstream платформа и непроверенный эксперимент**, не равноценный рекомендуемому Linux-пути.
Готовая документация сборки macOS не отменяет явного удаления x86_64 в changelog. Даже успешная сборка оставит
на владельце поддержку этого бинарника/ABI и последующих обновлений. Патчить cryptography ради обхода отказа платформы
не предлагается: при невозможности сборки остановиться.

Runtime — тот же runtime-33.txt, но cryptography 50.0.2 должна поступить из собственного проверенного wheel.
Остальные 32 версии имеют Mac Intel wheel по текущим тегам CPython 3.11.8/macOS 15.8.2.

Отдельная Python-среда **только сборщика** (не существующий D4 venv), четыре точных пакета:

```text
maturin==1.14.1
setuptools==84.2.1
cffi==2.1.1
pycparser==3.12
```

[mac-build-4.txt](mac-build-4.txt), [mac-build-metadata.json](mac-build-metadata.json).
Версии удовлетворяют PEP 517 build-system cryptography и своим активным требованиям на CPython 3.11.
Maturin/setuptools — дополнительные инструменты сборщика, не часть runtime-33; CFFI/pycparser уже есть в runtime.
В upstream CI pycparser закреплён иначе; наш 3.12 выбран из runtime по совместимым метаданным, совместная сборка не проверена.
CI-пин setuptools 82.0.1 также не перенесён: PyPI сообщает advisories; для builder предложен текущий 84.2.1,
прошедший проверку требований/тегов, не сборку. Причина замены/ID advisories зафиксированы в build-metadata.
Нужны также bootstrap pip с PEP 517/wheel-командой; текущий Mac pip 24.0 не менялся и не объявляется проверенным builder.
[pyproject 50.0.2](https://github.com/pyca/cryptography/blob/50.0.2/pyproject.toml),
[upstream build requirements](https://github.com/pyca/cryptography/blob/50.0.2/.github/requirements/build-requirements.txt).

Native-инструменты и необходимые действия, также только после отдельного разрешения:

1. Проверить/согласовать Apple Command Line Tools с clang/linker, macOS SDK и Python headers для имеющегося
   CPython 3.11.8 x86_64. CLT/SDK здесь не инвентаризировались, точный выпуск не выбран и не установлен.
   Не запускать автоматическую установку Xcode/Homebrew и не заменять системный Python.
2. Согласовать Rust/Cargo для x86_64-apple-darwin. Upstream MSRV — **Rust 1.83.0**; это нижняя граница,
   не утверждение о текущем безопасном/toolchain pin. Конкретный выпуск Rust/его хэш фиксируется до разрешённой сборки.
   Использовать Cargo.lock исходного тега без cargo update. В mac-build-metadata.json записан полный список
   lock-пакетов/версий/checksum и SHA-256 исходных pyproject/Cargo файлов; это не проверка скачивания crates.
   [Cargo workspace/MSRV](https://github.com/pyca/cryptography/blob/50.0.2/Cargo.toml).
3. Для конкретного native-кандидата предлагается **OpenSSL 3.5.9 LTS**, отдельный приватный prefix,
   x86_64 target, make/Perl >=5.10 и необходимые core-модули. Text::Template имеет bundled fallback в исходнике OpenSSL,
   установка из CPAN не предполагается без отдельного решения ([NOTES-PERL](https://github.com/openssl/openssl/blob/openssl-3.5.9/NOTES-PERL.md)).
   Эта поддерживаемая ветка перечислена на официальной
   [странице OpenSSL](https://openssl-library.org/source/); сборка/тесты на этом Mac не проверены.
   До скачивания зафиксировать SHA-256/подпись источника и native-инструменты; исходник не скачивался.
   Библиотеки OpenSSL/LibreSSL из базовой macOS для cryptography не поддерживаются.
   [Инструкция macOS](https://cryptography.io/en/50.0.2/installation/#building-cryptography-on-macos),
   [предпосылки сборки OpenSSL](https://github.com/openssl/openssl/blob/openssl-3.5.9/INSTALL.md).
4. После отдельного разрешения на исходники/сборку получить только зафиксированный sdist 50.0.2,
   build-wheel пакеты и Cargo crates с проверкой хэшей. Сначала собрать/проверить OpenSSL в отдельном prefix,
   затем собственный cryptography wheel с OPENSSL_DIR/OPENSSL_STATIC, x86_64 target и согласованным deployment target.
   Не менять global libraries, PATH профили и существующий D4 venv. Не применять неограниченную PEP 517 auto-install:
   сборщик заранее комплектуется mac-build-4, сборка выполняется с --no-build-isolation/--no-deps.
5. Записать версии компилятора/SDK/Rust/OpenSSL, полный build log без секретов, native linkage, wheel tag/hash.
   Проверить wheel в третьем отдельном тестовом venv, cryptography/SDK подпись и полный D4-набор без сокетов.
   Никакой установки полученного wheel в принятый D4 venv до независимой проверки.

Ориентир будущей wheel-команды, где BUILD_PYTHON/APPROVED_WHEELHOUSE обозначают ещё не созданные согласованные пути:

```sh
BUILD_PYTHON -m pip wheel --no-deps --no-build-isolation --no-binary=cryptography --no-cache-dir --wheel-dir APPROVED_WHEELHOUSE cryptography==50.0.2
```

Это не готовый проверенный build recipe: CLT/SDK/Rust pin, native hashes/параметры линковки требуют фиксации,
а успех на удалённой из поддержки платформе неизвестен. Команда не выполнялась; Rust/OpenSSL/Cargo/CPAN зависимости
не устанавливались и не загружались. Linux-путь снимает необходимость этого собственного native build.

## Дальнейшие отдельные решения

Сейчас достаточно выбрать направление среды — рекомендуется Linux. Выбор не разрешает создание среды или установку.
Следующая разрешённая итерация: конкретный профиль/digest, установки и настоящие SDK/OAuth **офлайн** проверки
на искусственных значениях. Подпись/refresh только SDK; готовый Request с фиксированным token endpoint,
лимитом фактических refresh-запросов и без скрытого replay append после 401. Сокеты запрещены в этой приёмке.

Лишь затем отдельно согласовывать Google-ресурс/права, чтение конкретной авторизации и одну RAW-строку+чтение.
Telegram не повторять. Приёмка офлайн B2 не означает приёмку SDK/Linux/Google live/публичного D4.
Редактура слова append в клиентской квитанции из протокола ревью отложена до следующей разрешённой **кодовой** итерации;
в этой сверке продуктовый текст не менялся.
