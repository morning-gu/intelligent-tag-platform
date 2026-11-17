from contextlib import contextmanager
from time import monotonic


@contextmanager
def record_elapsed(timings: dict[str, float], stage: str):
    started = monotonic()
    try:
        yield
    finally:
        timings[stage] = timings.get(stage, 0.0) + (monotonic() - started)
