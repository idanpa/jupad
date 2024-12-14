import ast
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

class FileRemodifiedError(ValueError): pass

class AssignmentsGetter(ast.NodeTransformer, list):
    def __init__(self):
        super().__init__()

    def visit_Assign(self, node):
        self.append(node.target)
        return self.generic_visit(node)

    def visit_AugAssign(self, node):
        self.append(node.target)
        return self.generic_visit(node)

    def visit_Delete(self, node):
        return self.generic_visit(node)

class ReplaceAssignment(ast.NodeTransformer):
    def __init__(self, ):
        super().__init__()

    def visit_Assign(self, node):
        self.append(node.target)
        return self.generic_visit(node)
