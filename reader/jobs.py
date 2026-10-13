"""Reader jobs: run a document through the pipeline off the request thread.

* A job id is 128 random bits; a job is visible only to the owner token
  that created it (the browser keeps that token). Unknown or foreign ids
  look the same: not found.
* Stages and timings are recorded as numbered events; a client can
  reconnect and resume from the last event id it saw.
* At most MAX_RUNNING jobs run at once; more wait in the queue.
* Cancel stops the job at the next stage boundary. A job that runs past
  JOB_TIMEOUT_S is stopped the same way and marked failed.
* Results live only in this process's memory for RESULT_TTL_S, so a
  refresh or a reopened report does not re-run the work. The uploaded
  bytes are dropped as soon as the pipeline finishes. A server restart
  loses jobs; the client then says so and offers to run the file again.
* The same owner submitting the same file (same sha256 and ticker) while a
  job is queued, running or done gets that job back instead of a new one.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from . import pipeline
from .document import ReadError

log = logging.getLogger("reader.jobs")

MAX_RUNNING = 2
MAX_JOBS = 60
RESULT_TTL_S = 60 * 60
JOB_TIMEOUT_S = 180


def _owner_key(token: str) -> str:
    return hashlib.sha256(("bmg-reader:" + (token or "")).encode()).hexdigest()


class Job:
    def __init__(self, owner: str, sha: str, title: str, kind: str, ticker: Optional[str]):
        self.id = secrets.token_urlsafe(16)
        self.owner = owner
        self.sha256 = sha
        self.title = title
        self.kind = kind
        self.ticker = ticker
        self.created = time.time()
        self.updated = self.created
        self.started: Optional[float] = None
        self.state = "queued"          # queued, running, done, failed, cancelled
        self.stage = "Upload"
        self.stages = {s: {"state": "pending"} for s in pipeline.STAGES}
        self.stages["Upload"] = {"state": "done", "seconds": 0}
        self.events: list[dict] = []
        self.error: Optional[str] = None
        self.result: Optional[dict] = None
        self.pdf: Optional[bytes] = None
        self.cancel_flag = threading.Event()
        self.lock = threading.Lock()
        self.emit("stage", {"stage": "Upload", "state": "done"})

    def emit(self, typ: str, data: dict):
        with self.lock:
            self.events.append({"id": len(self.events) + 1, "type": typ, "data": data, "t": round(time.time() - self.created, 2)})
            self.updated = time.time()

    def public(self) -> dict:
        r = {"job_id": self.id, "state": self.state, "stage": self.stage, "stages": self.stages, "title": self.title,
             "kind": self.kind, "sha256": self.sha256, "error": self.error, "events": len(self.events),
             "created": self.created, "expires_in": max(0, int(RESULT_TTL_S - (time.time() - self.updated))) if self.state in ("done", "failed", "cancelled") else None}
        if self.result:
            rep = self.result["report"]
            r["summary"] = {"company": rep["company"].get("name"), "ticker": rep["company"].get("ticker"), "coverage": rep["coverage"]["status"],
                            "findings": len(rep["findings"]), "issues": len(rep["coverage"]["issues"]), "validation_passed": rep["validation"]["passed"]}
        return r


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=MAX_RUNNING, thread_name_prefix="reader")

    # ------------------------------------------------------------------
    def _sweep(self):
        now = time.time()
        with self.lock:
            for jid in [j.id for j in self.jobs.values() if j.state in ("done", "failed", "cancelled") and now - j.updated > RESULT_TTL_S]:
                self.jobs.pop(jid, None)
            if len(self.jobs) > MAX_JOBS:
                old = sorted((j for j in self.jobs.values() if j.state in ("done", "failed", "cancelled")), key=lambda j: j.updated)
                for j in old[:len(self.jobs) - MAX_JOBS]:
                    self.jobs.pop(j.id, None)

    def busy(self) -> int:
        return sum(1 for j in self.jobs.values() if j.state in ("queued", "running"))

    def submit(self, owner_token: str, data: bytes, kind: str, title: str, ticker: Optional[str]) -> tuple[Job, bool]:
        self._sweep()
        owner = _owner_key(owner_token)
        sha = hashlib.sha256(data).hexdigest()
        with self.lock:
            for j in self.jobs.values():
                if j.owner == owner and j.sha256 == sha and j.ticker == ticker and j.state in ("queued", "running", "done"):
                    return j, True
            if self.busy() >= MAX_RUNNING * 4:
                raise ReadError(503, "The reader is busy. Try again in a minute.")
            job = Job(owner, sha, title, kind, ticker)
            self.jobs[job.id] = job
        self.pool.submit(self._run, job, data)
        log.info("reader job %s queued kind=%s bytes=%d", job.id[:8], kind, len(data))
        return job, False

    def get(self, owner_token: str, jid: str) -> Optional[Job]:
        self._sweep()
        j = self.jobs.get(jid or "")
        if not j or j.owner != _owner_key(owner_token):
            return None
        return j

    def cancel(self, owner_token: str, jid: str) -> Optional[Job]:
        j = self.get(owner_token, jid)
        if j and j.state in ("queued", "running"):
            j.cancel_flag.set()
            if j.state == "queued":
                j.state = "cancelled"
                j.emit("cancelled", {"message": "Cancelled before it started."})
        return j

    # ------------------------------------------------------------------
    def _run(self, job: Job, data: bytes):
        if job.cancel_flag.is_set():
            return
        job.state = "running"
        job.started = time.time()

        def progress(stage: str, detail: dict):
            job.stage = stage
            job.stages[stage] = {**job.stages.get(stage, {}), **detail}
            job.emit("stage", {"stage": stage, **detail})

        def cancelled() -> bool:
            if job.started and time.time() - job.started > JOB_TIMEOUT_S:
                job.error = f"Stopped: the job ran longer than {JOB_TIMEOUT_S} seconds."
                return True
            return job.cancel_flag.is_set()

        try:
            res = pipeline.run(data, job.kind, job.title, job.ticker, progress=progress, cancelled=cancelled)
            job.result = res
            job.stages["Ready"] = {"state": "done"}
            job.stage = "Ready"
            job.state = "done"
            rep = res["report"]
            job.emit("findings", {"count": len(rep["findings"])})
            job.emit("done", {"coverage": rep["coverage"]["status"], "issues": len(rep["coverage"]["issues"]), "validation_passed": rep["validation"]["passed"]})
            log.info("reader job %s done in %.1fs findings=%d issues=%d validation=%s", job.id[:8], time.time() - job.started,
                     len(rep["findings"]), len(rep["coverage"]["issues"]), rep["validation"]["passed"])
        except pipeline.Cancelled:
            job.state = "failed" if job.error else "cancelled"
            job.stages[job.stage] = {**job.stages.get(job.stage, {}), "state": "stopped"}
            job.emit("failed" if job.error else "cancelled", {"message": job.error or "Cancelled.", "stage": job.stage})
            log.info("reader job %s %s at %s", job.id[:8], job.state, job.stage)
        except ReadError as e:
            job.state, job.error = "failed", e.msg
            job.stages[job.stage] = {**job.stages.get(job.stage, {}), "state": "failed"}
            job.emit("failed", {"message": e.msg, "stage": job.stage})
            log.info("reader job %s failed at %s: %s", job.id[:8], job.stage, e.msg[:120])
        except Exception as e:     # never leave a job hanging
            job.state = "failed"
            job.error = f"Processing failed at the {job.stage} stage ({e.__class__.__name__}). The file was not fully read; no report was produced."
            job.stages[job.stage] = {**job.stages.get(job.stage, {}), "state": "failed"}
            job.emit("failed", {"message": job.error, "stage": job.stage})
            log.exception("reader job %s crashed at %s", job.id[:8], job.stage)
        finally:
            del data

    def pdf(self, job: Job) -> bytes:
        if job.pdf is None:
            from . import pdf as pdfmod
            job.pdf = pdfmod.render(job.result["report"])
        return job.pdf


MANAGER = JobManager()
