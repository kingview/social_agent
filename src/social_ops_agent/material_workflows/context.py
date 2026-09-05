"""Shared boundaries for workflow handlers, without UI or repository SQL."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..diagnostics import current_context, diagnostic_context
from ..material_library import MaterialLibrary
from ..material_limits import model_slot
from ..material_settings import MaterialSettings
from .inputs import sidecar_metadata

if TYPE_CHECKING:
    from ..material_service import MaterialService


@dataclass
class WorkflowContext:
    service: MaterialService
    parameters: dict
    job_id: str

    def __post_init__(self):
        self.settings = MaterialSettings.model_validate(self.parameters['settings'])
        self.options = self.parameters['options']
        self._library = None
        self._invoker = None

    @property
    def library(self):
        if self._library is None:
            self._library = MaterialLibrary(Path(self.settings.library_root))
        return self._library

    def check_control(self):
        self.service.jobs.check_control(self.job_id)

    def call(self, name, arguments):
        self.check_control()
        if self._invoker is None:
            self._invoker = self.service.invoker(self.settings)
        execution_id = (
            current_context().get('execution_id')
            or self.parameters.get('execution_id')
            or self.job_id
        )
        with diagnostic_context(task_id=self.job_id, execution_id=execution_id):
            return asyncio.run(self._invoker.call(name, arguments))

    @contextmanager
    def model_slot(self):
        with model_slot(
            self.service.state_root / 'material-model-slots',
            self.settings.model_concurrency,
            check_control=self.check_control,
        ):
            yield

    def source(self, item):
        self.check_control()
        resource = (
            self.library.get(str(item).removeprefix('resource:'))
            if str(item).startswith('resource:') else None
        )
        original = Path(resource['path'] if resource else item).resolve(strict=True)
        metadata = (
            json.loads(resource['metadata_json'])
            if resource else sidecar_metadata(original)
        )
        if self.options.get('theme'):
            metadata['theme'] = self.options['theme']
        return resource, original, metadata

    def stage(self, original):
        self.check_control()
        return self.service.stage(original)
