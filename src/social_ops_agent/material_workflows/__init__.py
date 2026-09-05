"""Independent item workflows behind the MaterialService compatibility facade."""

from .analysis import analyze
from .context import WorkflowContext
from .discovery import discover
from .download import download
from .intake import import_material

HANDLERS = {
    'discover': discover,
    'download': download,
    'import': import_material,
    'analyze': analyze,
}

__all__ = ['HANDLERS', 'WorkflowContext']
