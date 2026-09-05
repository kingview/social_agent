"""Deliver background service results to widgets on the GUI thread."""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot


class _WorkerSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(object)


class _Worker(QRunnable):
    def __init__(self, operation, error_reporter=None):
        super().__init__()
        self.operation = operation
        self.error_reporter = error_reporter
        self.signals = _WorkerSignals()

    def run(self):
        try:
            result = self.operation()
        except Exception as exc:
            if self.error_reporter is not None:
                try:
                    self.error_reporter(exc)
                except Exception:
                    logging.getLogger(__name__).exception('Background service exception reporting failed')
            self.signals.failed.emit(exc)
        else:
            self.signals.succeeded.emit(result)


class BackgroundCall(QObject):
    """One operation at a time; destroying a view safely disconnects its slots.

    Only service data enters the worker. The operation must not reference widgets.
    The pool owns its runnable until completion, independently of the view lifetime.
    """

    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self, parent=None, *, error_reporter=None):
        super().__init__(parent)
        self.busy = False
        self._worker = None
        self.error_reporter = error_reporter

    def start(self, operation):
        if self.busy:
            return False
        self.busy = True
        self._worker = _Worker(operation, self.error_reporter)
        self._worker.signals.succeeded.connect(self._succeeded, Qt.ConnectionType.QueuedConnection)
        self._worker.signals.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(self._worker)
        return True

    @Slot(object)
    def _succeeded(self, result):
        self.busy = False
        self._worker = None
        self.succeeded.emit(result)

    @Slot(object)
    def _failed(self, exc):
        self.busy = False
        self._worker = None
        self.failed.emit(exc)
