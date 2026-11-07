from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
import re
import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: ROOT / 'data')
    prefix: str = ''
    retention: int = 86400
    receipt_ttl: int = 3600
    minute_limit: int = 5
    hour_limit: int = 30
    storage_limit: int = 100 * MIB
    record_limit: int = 200
    cleanup_interval: int = 60
    clock: object = time.time

    def __post_init__(self):
        self.data_dir = self.data_dir.resolve()
        if self.data_dir.is_relative_to((ROOT / 'public').resolve()):
            raise ValueError('Private data must be outside public')
        if not re.fullmatch(r'(?:/[a-zA-Z0-9_-]+)*', self.prefix):
            raise ValueError('Invalid prefix')


class Failure(Exception):
    def __init__(self, status, message, fields=None, retry_after=None):
        self.status = status
        self.message = message
        self.fields = fields or {}
        self.retry_after = retry_after


class Lead(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    sofa: Literal['Прямой', 'Угловой', 'Модульный']
    size: Literal['Два места', 'Три места', 'Четыре и больше', 'Не знаю']
    material: Literal['Ткань / рогожка', 'Велюр / бархат', 'Кожа / экокожа', 'Не знаю']
    description: str = Field(min_length=1, max_length=1000)
    channel: Literal['email', 'telegram']
    contact: str = Field(min_length=1, max_length=80)

    @model_validator(mode='after')
    def synthetic_only(self):
        pattern = (r'[a-z0-9._+\-]+@example\.test' if self.channel == 'email'
                   else r'@demo_[a-z0-9_]{1,30}')
        if not re.fullmatch(pattern, self.contact, re.IGNORECASE):
            raise ValueError('Synthetic contacts only')
        if any(ord(c) < 32 and c not in '\n\t' for c in self.description):
            raise ValueError('Control characters are not allowed')
        return self


def sheets_row(submission_id, lead):
    """Исторический 7-польный helper, не контракт B2. B2: sheets.row_for_submission, 9 полей/UTC."""
    return [submission_id, lead.sofa, lead.size, lead.material, lead.description, lead.channel, lead.contact]


def csv_cell(value):
    """Защита формульных префиксов, включая пробелы/управляющие символы перед ними."""
    value = str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value
