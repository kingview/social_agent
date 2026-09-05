"""Compatibility imports for material UI components.

Implementation lives in material_ui; callers retain their existing imports.
"""
from .material_ui.controls import button, combo, error
from .material_ui.task_panel import MaterialTaskPanel
from .material_ui.tool_dialog import MaterialToolDialog
from .material_ui.settings_dialog import MaterialSettingsDialog
from .material_ui.library_dialog import MaterialLibraryDialog
from .material_ui.toolbox import MaterialToolbox

__all__ = [
    "MaterialTaskPanel", "MaterialToolDialog", "MaterialSettingsDialog",
    "MaterialLibraryDialog", "MaterialToolbox", "button", "combo", "error",
]
