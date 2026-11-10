"""Офлайн-авторизация Sheets через официальный Google SDK."""
import re

import httpx

from .sheets_config import ConfigurationError, SCOPE, TOKEN_URI, validate_service_account


class SDKAuthorization:
    def __init__(self, config, info, *, transport):
        if not isinstance(transport, httpx.MockTransport):
            raise ConfigurationError('Для проверки SDK требуется подставной OAuth-транспорт.')
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
            with httpx.Client(transport=self._transport, timeout=5, trust_env=False,
                              follow_redirects=False) as client:
                def request(url, method, body, headers, **kwargs):
                    nonlocal attempted
                    if attempted or url != TOKEN_URI or method != 'POST':
                        raise ConfigurationError('OAuth-запрос отклонён.')
                    attempted = True
                    response = client.post(url, content=body, headers=headers)
                    if response.status_code != 200 or len(response.content) > 65536:
                        raise ConfigurationError('Не удалось получить токен Sheets.')
                    return _TokenResponse(response.content)

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
