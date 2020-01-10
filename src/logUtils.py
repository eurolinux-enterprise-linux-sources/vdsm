import logging
import sys
from StringIO import StringIO

class SimpleLogAdapter(logging.LoggerAdapter):
    # Because of how python implements the fact that warning
    # and warn are the same. I need to reimplement it here. :(
    def warn(self, *args, **kwargs):
        return self.warning(*args, **kwargs)

    def process(self, msg, kwargs):
        result = StringIO()
        for key, value in self.extra.iteritems():
            result.write(key)
            result.write("=`")
            result.write(value)
            result.write("`")
        result.write("::")
        result.write(msg)
        result.seek(0)
        return (result.read(), {})

class TracebackRepeatFilter(logging.Filter):
    """
    Makes sure a traceback is logged only once for each exception.
    """
    def filter(self, record):
        if not record.exc_info:
            return 1

        info = sys.exc_info()
        ex = info[1]
        if ex is None:
            return 1

        if hasattr(ex, "_logged") and ex._logged:
            record.exc_info = False
            ex._logged = True

        return 1

