"""Versioned local material-workflow settings; existing LLM settings stay intact."""
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .state_io import write_json
from .material_dimensions import SCORE_DIMENSIONS,FILTER_DIMENSIONS


class StrategyRule(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    weights: dict[str, float] = Field(default_factory=lambda: {'quality': 1})
    required: dict[str, list[str]] = Field(default_factory=dict)
    preferred: dict[str, list[str]] = Field(default_factory=dict)
    minimum_score: float = Field(default=60, ge=0, le=100)

    @field_validator('required', 'preferred')
    @classmethod
    def valid_dimensions(cls, value):
        if set(value)-FILTER_DIMENSIONS:
            raise ValueError('筛选或偏好维度不受支持')
        return value

    @field_validator('weights')
    @classmethod
    def valid_weights(cls, value):
        if not value or set(value) - SCORE_DIMENSIONS or any(not 0 <= v <= 100 for v in value.values()) or not sum(value.values()):
            raise ValueError('评分权重必须是受支持维度的非负数，且总和大于 0')
        return value


class MaterialSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schema_version: int = 1
    library_root: str
    download_root: str
    local_model: str = 'qwen3.5:9b'
    local_base_url: str = 'http://127.0.0.1:11434/v1'
    max_concurrency: int = Field(default=2, ge=1, le=8)
    model_concurrency: int = Field(default=1, ge=1, le=4)
    themes: list[str] = Field(default_factory=lambda: ['科技', '美女', 'Web3', '萌宠', '成人'])
    analysis_dimensions: list[str] = Field(default_factory=lambda: ['主题', '场景', '风格', '时效性', '语言', '画面质量'])
    tag_rules: str = ''
    strategies: list[StrategyRule] = Field(default_factory=list)

    @field_validator('local_base_url')
    @classmethod
    def local_only(cls, value):
        url = urlsplit(value)
        if url.scheme not in {'http', 'https'} or url.hostname not in {'localhost', '127.0.0.1', '::1'} or url.username or url.password:
            raise ValueError('一期素材分析使用本机模型地址；现有远端模型设置不受影响')
        return value.rstrip('/')

    @classmethod
    def load(cls, state_root: Path, output_root: Path):
        path = state_root / 'material-settings.json'
        if path.exists():
            return cls.model_validate_json(path.read_text())
        return cls(library_root=str(output_root / '素材库'), download_root=str(output_root / '素材下载'))

    def save(self, state_root: Path):
        for raw in (self.library_root, self.download_root):
            path = Path(raw).expanduser()
            if not path.is_absolute():
                raise ValueError('目录必须是绝对路径')
            path.mkdir(parents=True, exist_ok=True)
        write_json(state_root / 'material-settings.json', self.model_dump(mode='json'))
