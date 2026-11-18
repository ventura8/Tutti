import tempfile
import os
print(tempfile.gettempdir())
os.environ['TMPDIR'] = '/tmp/test'
print(tempfile.gettempdir())
