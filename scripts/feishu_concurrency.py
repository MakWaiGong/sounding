"""Local-machine coordination for cooperating Feishu publishers.

All clones for a user must use the same state directory. SOUNDING_FEISHU_STATE_DIR
may override the default ~/.sounding/feishu (mainly for isolated installations/tests).
Lock files are never deleted: OS locks, not file existence, establish ownership.
"""
import contextlib
import hashlib
import json
import os
import tempfile
import time
import uuid
from urllib.parse import unquote, urlsplit

MAX_WORKERS = 8
DEFAULT_WORKERS = 3


class BusyError(RuntimeError):
    pass


def worker_count(cfg, key):
    value = cfg.get(key, DEFAULT_WORKERS)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_WORKERS:
        raise ValueError(f"feishu.{key} must be an integer between 1 and {MAX_WORKERS}")
    return value


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def state_root():
    return os.path.abspath(os.path.expanduser(os.environ.get(
        "SOUNDING_FEISHU_STATE_DIR", os.path.join("~", ".sounding", "feishu"))))


def destination_dir(ws, cfg):
    base = cfg.get("api_base") or "https://open.feishu.cn"
    if not isinstance(base, str):
        raise ValueError("feishu.api_base must identify an API host")
    parsed = urlsplit(base)
    if not parsed.hostname:
        raise ValueError("feishu.api_base must identify an API host")
    # Match FeishuClient: it connects to hostname only and chooses HTTPS by scheme.
    scheme = "https" if (parsed.scheme or "https") == "https" else "http"
    dest = [scheme, parsed.hostname, ws["app_token"], ws["tables"]["papers"]]
    return os.path.join(state_root(), digest(json.dumps(dest, ensure_ascii=False, separators=(",", ":"))))


def alias_keys(citekey, doi):
    key = str(citekey or "").strip().casefold()
    if not key:
        raise ValueError("citekey is required for publication locking")
    aliases = ["citekey:" + key]
    if doi:
        normalized = unquote(str(doi).strip()).casefold()
        for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix):].strip()
                break
        if not normalized:
            raise ValueError("invalid DOI for publication locking")
        aliases.append("doi:" + normalized)
    return sorted(digest(value) for value in aliases)


class FileLock:
    def __init__(self, path):
        self.path = path
        self.file = None

    def acquire(self, blocking=True):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.file = os.fdopen(fd, "r+b", buffering=0)
        try:
            # Both supported lock APIs lock beyond EOF; no pre-lock write races another owner.
            while True:
                try:
                    self.file.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except (BlockingIOError, PermissionError):
                    if not blocking:
                        self.file.close()
                        self.file = None
                        return False
                    time.sleep(0.025)
                except OSError as exc:
                    # Windows CRT reports lock contention as EACCES/EDEADLK.
                    import errno
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not blocking:
                        self.file.close()
                        self.file = None
                        return False
                    time.sleep(0.025)
        except BaseException:
            self.file.close()
            self.file = None
            raise

    def release(self):
        if self.file is not None:
            try:
                self.file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            finally:
                self.file.close()
                self.file = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


def atomic_json(path, value):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            directory_fd = os.open(os.path.dirname(path), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_registry(path):
    if not os.path.exists(path):
        return {"version": 1, "aliases": {}, "papers": {}, "limit": None}
    with open(path, encoding="utf-8") as f:
        value = json.load(f)
    if (not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(value.get("aliases"), dict) or not isinstance(value.get("papers"), dict)):
        raise RuntimeError("Feishu shared recovery registry is invalid; repair it before publishing")
    return value


class Publication:
    def __init__(self, ws, cfg, citekey, doi, limit):
        self.directory = destination_dir(ws, cfg)
        self.aliases = alias_keys(citekey, doi)
        self.limit = limit
        self.registry = os.path.join(self.directory, "registry.json")
        self.registry_lock = os.path.join(self.directory, "registry.lock")
        self.locks = []
        self.paper_id = None

    def _take(self, name, error):
        lock = FileLock(os.path.join(self.directory, name))
        if not lock.acquire(blocking=False):
            raise BusyError(error)
        self.locks.append(lock)
        return lock

    def __enter__(self):
        try:
            for alias in self.aliases:
                self._take(os.path.join("aliases", alias + ".lock"), "paper busy: another publication is in progress")
            with FileLock(self.registry_lock):
                registry = read_registry(self.registry)
                ids = {registry["aliases"][a] for a in self.aliases if a in registry["aliases"]}
                if len(ids) > 1:
                    raise RuntimeError("conflicting paper aliases in shared recovery registry; repair before publishing")
                self.paper_id = next(iter(ids)) if ids else uuid.uuid4().hex
                if not isinstance(self.paper_id, str) or not self.paper_id.isalnum():
                    raise RuntimeError("invalid shared paper identity; repair the recovery registry")
                self._take(os.path.join("papers", self.paper_id + ".lock"),
                           "paper busy: another publication is in progress")
                active = False
                for slot in range(MAX_WORKERS):
                    probe = FileLock(os.path.join(self.directory, "slots", str(slot) + ".lock"))
                    if probe.acquire(blocking=False):
                        probe.release()
                    else:
                        active = True
                if active and registry.get("limit") != self.limit:
                    raise BusyError("destination busy: active publishers use a different feishu.paper_concurrency")
                got_slot = False
                for slot in range(self.limit):
                    lock = FileLock(os.path.join(self.directory, "slots", str(slot) + ".lock"))
                    if lock.acquire(blocking=False):
                        self.locks.append(lock)
                        got_slot = True
                        break
                if not got_slot:
                    raise BusyError("destination busy: feishu.paper_concurrency limit reached")
                registry["limit"] = self.limit
                registry["aliases"].update({alias: self.paper_id for alias in self.aliases})
                registry["papers"].setdefault(self.paper_id, {})
                atomic_json(self.registry, registry)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        for lock in reversed(self.locks):
            lock.release()
        self.locks.clear()

    def get_state(self):
        with FileLock(self.registry_lock):
            state = read_registry(self.registry)["papers"].get(self.paper_id)
            if not isinstance(state, dict):
                raise RuntimeError("invalid shared paper state; repair the recovery registry")
            return dict(state)

    def set_state(self, state):
        with FileLock(self.registry_lock):
            registry = read_registry(self.registry)
            registry["papers"][self.paper_id] = dict(state)
            atomic_json(self.registry, registry)

    def repair_message(self, kind):
        if kind == "document":
            steps = ("If found, set phase to pending and doc_id to its verified ID; "
                     "only if confirmed absent, replace the paper state with {}.")
        else:
            steps = ("If found, set phase to record_known and record_id to its verified ID; "
                     "only if confirmed absent, set phase to pending and remove record_id. "
                     "Keep the existing doc_id and make_public values.")
        return (f"{kind} creation outcome is unknown; publication is blocked to prevent duplicates. "
                f"Inspect Feishu and verify the outcome. Back up and repair the shared registry "
                f"{self.registry}, papers entry {self.paper_id}. {steps} Then retry.")

    @property
    def resume_path(self):
        return os.path.join(self.directory, "images", self.paper_id + ".json")


def index_lock_path(path):
    return os.path.realpath(os.path.abspath(path)) + ".lock"


@contextlib.contextmanager
def locked_index(path):
    with FileLock(index_lock_path(path)):
        yield
