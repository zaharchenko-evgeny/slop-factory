"""Serialize background snapshot refreshes and publish their progress."""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone


_PHASES = ('pending', 'listing', 'prefetch', 'events', 'tickets', 'complete')


def _now():
    return datetime.now(timezone.utc).isoformat()


class RefreshController:
    """Run at most one tracker refresh at a time for a server instance."""

    def __init__(self, store):
        self.store = store
        self.sources = tuple(store.sources)
        self._lock = threading.Lock()
        self._queue = deque()
        self._jobs = {False: None, True: None}
        self._snapshots = {False: None, True: None}
        self._snapshot_run_ids = {False: None, True: None}
        self._next_id = 1
        self._worker = None
        self._closed = False

    def _source_statuses(self):
        return [{'id': str(source.id), 'name': str(source.name), 'phase': 'pending',
                 'completedTickets': 0, 'totalTickets': None, 'errorCount': 0}
                for source in self.sources]

    def _idle_status(self, older):
        return {'id': 0, 'state': 'idle', 'older': older,
                'startedAt': None, 'finishedAt': None, 'error': None,
                'sources': self._source_statuses()}

    @staticmethod
    def _copy_status(status):
        return {**status, 'sources': [dict(source) for source in status['sources']]}

    def _status_locked(self, older):
        job = self._jobs[older]
        return self._copy_status(job['status']) if job else self._idle_status(older)

    def snapshot(self, older=False):
        """Return the cached snapshot, its producing run id, and current status."""
        with self._lock:
            return (self._snapshots[older], self._snapshot_run_ids[older],
                    self._status_locked(older))

    def status(self, older=False):
        """Return status only; this never reads from a tracker."""
        with self._lock:
            return {'snapshotRunId': self._snapshot_run_ids[older],
                    'refresh': self._status_locked(older)}

    def request(self, older=False):
        """Start or join a refresh for a scope, without waiting for tracker I/O."""
        with self._lock:
            if self._closed:
                return self._status_locked(older)
            current = self._jobs[older]
            if current and current['status']['state'] in ('queued', 'running'):
                return self._copy_status(current['status'])

            job_id = self._next_id
            self._next_id += 1
            job = {'id': job_id, 'older': older,
                   'status': {'id': job_id, 'state': 'queued', 'older': older,
                              'startedAt': None, 'finishedAt': None, 'error': None,
                              'sources': self._source_statuses()}}
            self._jobs[older] = job
            self._queue.append(job)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run,
                                                name='factory-ui-refresh', daemon=True)
                self._worker.start()
            return self._copy_status(job['status'])

    def _progress(self, job, source, phase, *, completed_tickets=None,
                  total_tickets=None, error_count=None):
        with self._lock:
            if self._jobs[job['older']] is not job:
                return
            current = job['status']
            by_id = next((item for item in current['sources']
                          if item['id'] == str(source.id)), None)
            if by_id is None:
                return
            if phase not in _PHASES:
                return
            if _PHASES.index(phase) >= _PHASES.index(by_id['phase']):
                by_id['phase'] = phase
            if completed_tickets is not None:
                by_id['completedTickets'] = max(by_id['completedTickets'], completed_tickets)
            if total_tickets is not None:
                by_id['totalTickets'] = total_tickets
            if error_count is not None:
                by_id['errorCount'] = error_count

    def _finish(self, job, *, snapshot=None, error=None):
        with self._lock:
            status = job['status']
            status['finishedAt'] = _now()
            if error is not None:
                status['state'] = 'failed'
                status['error'] = error
            else:
                status['state'] = 'complete'
                self._snapshots[job['older']] = snapshot
                self._snapshot_run_ids[job['older']] = job['id']

    def _run(self):
        while True:
            with self._lock:
                if not self._queue:
                    self._worker = None
                    return
                job = self._queue.popleft()
                job['status']['state'] = 'running'
                job['status']['startedAt'] = _now()

            try:
                snapshot = self.store.snapshot(
                    older=job['older'],
                    progress=lambda source, phase, **details:
                        self._progress(job, source, phase, **details))
            except Exception as exc:
                # Never expose exception messages: they can contain credentials or response data.
                error = f'Refresh failed ({type(exc).__name__}).'
                self._finish(job, error=error)
            else:
                self._finish(job, snapshot=snapshot)

    def close(self, timeout=2.0):
        """Stop accepting work and allow the daemon worker to finish its current call."""
        with self._lock:
            self._closed = True
            worker = self._worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout)
