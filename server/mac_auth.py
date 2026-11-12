"""Одноразовый OAuth на Mac, подпись RS256 системным OpenSSL через pipe."""
import base64
import json
import math
import os
import re
import subprocess
import time

import httpx

from .sheets_config import ConfigurationError, SCOPE, TOKEN_URI, validate_service_account

OPENSSL = '/usr/bin/openssl'


def base64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


class MacAuthorization:
    def __init__(self, config, info, *, transport=None, allow_live=False, clock=time.time):
        self.offline = isinstance(transport, httpx.MockTransport)
        if not self.offline and (transport is not None or not allow_live or not config.enabled):
            raise ConfigurationError('Живая авторизация Mac выключена.')
        validate_service_account(info, config)
        pem = info['private_key'].encode('utf-8')
        key_id = info.get('private_key_id', '')
        if (not 1 <= len(pem) <= 4096 or not isinstance(key_id, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', key_id)
                or not pem.startswith((b'-----BEGIN PRIVATE KEY-----', b'-----BEGIN RSA PRIVATE KEY-----'))):
            raise ConfigurationError('Ключ Mac-демонстрации имеет неподдерживаемый формат.')
        try:
            check = subprocess.run([OPENSSL, 'rsa', '-check', '-noout', '-modulus', '-passin', 'pass:'],
                                   input=pem, capture_output=True, timeout=5, check=True,
                                   env={'OPENSSL_CONF': os.devnull})
            modulus, = re.findall(rb'Modulus=([0-9A-Fa-f]+)', check.stdout)
            if not 2048 <= int(modulus, 16).bit_length() <= 4096:
                raise ValueError()
        except Exception:
            raise ConfigurationError('Ключ RSA Mac не прошёл локальную проверку.') from None
        self.account = config.service_account
        self._pem = pem
        self._key_id = key_id
        self._transport = transport
        self._clock = clock

    def __repr__(self):
        return f'MacAuthorization(offline={self.offline})'

    def _assertion(self):
        stamp = self._clock()
        if not math.isfinite(stamp) or stamp <= 0:
            raise ConfigurationError('Неверное время Mac-демонстрации.')
        header = {'alg': 'RS256', 'typ': 'JWT', 'kid': self._key_id}
        claims = {'iss': self.account, 'scope': SCOPE, 'aud': TOKEN_URI,
                  'iat': int(stamp), 'exp': int(stamp) + 300}
        body = '.'.join(base64url(json.dumps(value, separators=(',', ':')).encode('utf-8'))
                        for value in (header, claims)).encode('ascii')
        read_fd, write_fd = os.pipe()
        try:
            # Ключ передаётся в pipe, не в argv или временный файл. Объём ограничен PIPE_BUF.
            if os.write(write_fd, self._pem) != len(self._pem):
                raise ConfigurationError('Не удалось передать ключ подписи.')
            os.close(write_fd)
            write_fd = None
            signed = subprocess.run([OPENSSL, 'dgst', '-sha256', '-sign', f'/dev/fd/{read_fd}'],
                                    input=body, capture_output=True, pass_fds=(read_fd,),
                                    timeout=5, check=True, env={'OPENSSL_CONF': os.devnull})
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
        if not 256 <= len(signed.stdout) <= 512:
            raise ConfigurationError('Неподдерживаемый размер подписи RSA.')
        return body.decode('ascii') + '.' + base64url(signed.stdout)

    def token(self):
        try:
            assertion = self._assertion()
            transport = self._transport or httpx.HTTPTransport(retries=0, trust_env=False)
            with httpx.Client(transport=transport, timeout=5, trust_env=False, follow_redirects=False) as client:
                deadline = time.monotonic() + 15
                with client.stream('POST', TOKEN_URI,
                                   data={'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': assertion},
                                   headers={'Accept-Encoding': 'identity', 'Accept': 'application/json'}) as response:
                    if (response.status_code != 200
                            or response.headers.get('content-encoding', 'identity').lower() != 'identity'):
                        raise ValueError()
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if len(raw) + len(chunk) > 65536 or time.monotonic() > deadline:
                            raise ValueError()
                        raw.extend(chunk)
            data = json.loads(raw)
            token = data.get('access_token') if isinstance(data, dict) else None
            if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9._~+/=-]{1,8192}', token)
                    or not isinstance(data.get('token_type'), str) or data['token_type'].lower() != 'bearer'
                    or type(data.get('expires_in')) is not int or not 60 <= data['expires_in'] <= 3600):
                raise ValueError()
            return token
        except Exception:
            raise ConfigurationError('Авторизация Mac недоступна. Значения скрыты.') from None
