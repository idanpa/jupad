import logging
from contextlib import contextmanager
from watchdog.observers import Observer

logger = logging.getLogger(__name__)
logger_handler = logging.StreamHandler()
logger_handler.setFormatter(logging.Formatter('%(relativeCreated)d: %(message)s'))
logger.addHandler(logger_handler)

class PausableObserver(Observer):
    @contextmanager
    def pause(self):
        orig_put = self.event_queue.put
        self.event_queue.put = lambda x : None
        try:
            yield
        finally:
            self.event_queue.put = orig_put
