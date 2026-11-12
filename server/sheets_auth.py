"""Авторизация Sheets через Google SDK и ограниченный обмен OAuth."""
import re
import time

import httpx

from .sheets_config import ConfigurationError, SCOPE, TOKEN_URI, validate_service_account


class SDKAuthorization:
    def __init__(self, config, info, *, transport=None, allow_live=False):
        self.offline = isinstance(transport, httpx.MockTransport)
        if not self.offline and (transport is not None or not allow_live or not config.enabled):
            raise ConfigurationError('Живая авторизация Sheets выключена.')
        validate_service_account(info, config)
        try:
            from google.oauth2 import service_account

            self._credentials = service_account.Credentials.from_service_account_info(
                info, scopes=[SCOPE], always_use_jwt_access=False,
            )
        except Exception:
            raise ConfigurationError('Не удалось подготовить авторизацию Google SDK.') from None
        self.account = config.service_account
        self._transport = transport

    def token(self):
        """Один обмен для операции Sheets. SDK подписывает assertion сам."""
        attempted = False
        try:
            transport = self._transport or httpx.HTTPTransport(retries=0, trust_env=False)
            with httpx.Client(transport=transport, timeout=5, trust_env=False,
                              follow_redirects=False) as client:
                def request(url, method, body, headers, **kwargs):
                    nonlocal attempted
                    if attempted or url != TOKEN_URI or method != 'POST':
                        raise ConfigurationError('OAuth-запрос отклонён.')
                    attempted = True
                    deadline = time.monotonic() + 15
                    with client.stream(method, url, content=body, headers=headers | {'Accept-Encoding': 'identity'}) as response:
                        if (response.status_code != 200
                                or response.headers.get('content-encoding', 'identity').lower() != 'identity'):
                            raise ConfigurationError('Не удалось получить токен Sheets.')
                        data = bytearray()
                        for chunk in response.iter_bytes():
                            if len(data) + len(chunk) > 65536 or time.monotonic() > deadline:
                                raise ConfigurationError('Ответ OAuth превышает лимит.')
                            data.extend(chunk)
                        return _TokenResponse(bytes(data))

                self._credentials.refresh(request)
            token = self._credentials.token
            if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~+/=-]{1,8192}', token):
                raise ConfigurationError('Некорректный ответ OAuth.')
            return token
        except Exception:
            raise ConfigurationError('Авторизация Google SDK недоступна.') from None


class _TokenResponse:
    status = 200

    def __init__(self, data):
        self.data = data
