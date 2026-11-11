"""A detached `run` for the providers that have no in-guest one (external,
nested): the CLI starts itself again in the background and keeps the output
and the exit status in <workdir>/detached/, where `status` and `wait` read
them. The podman provider does the same inside its container, with the same
method names for the state and the output."""
import os
import subprocess
import time

# $1 is the state dir: its pid, then the exit status when the command is done
_WRAP = ('d=$1; shift; echo $$ > "$d/pid"; "$@" > "$d/out.log" 2>&1; '
         'echo $? > "$d/rc"')


class LocalDetached:
    def __init__(self, workdir):
        self.dir = os.path.abspath(os.path.join(workdir, 'detached'))

    def _p(self, name):
        return os.path.join(self.dir, name)

    def start(self, argv):
        if self.detached_state()[0] == 'running':
            raise RuntimeError('a detached run is already in progress '
                               '(see `status`)')
        os.makedirs(self.dir, exist_ok=True)
        for name in ('rc', 'pid', 'out.log'):
            try:
                os.unlink(self._p(name))
            except FileNotFoundError:
                pass
        subprocess.Popen(['/bin/sh', '-c', _WRAP, 'sh', self.dir] + list(argv),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        # the state is read from the wrapper's pid file: wait until it is there
        for _ in range(100):
            if os.path.exists(self._p('pid')):
                break
            time.sleep(0.05)

    def detached_state(self):
        """('none'|'running'|'lost'|'finished', exit code or None)."""
        try:
            with open(self._p('rc')) as f:
                return 'finished', int(f.read().strip())
        except (OSError, ValueError):
            pass
        try:
            with open(self._p('pid')) as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return 'none', None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return 'lost', None
        except PermissionError:
            pass
        return 'running', None

    def detached_tail(self, lines):
        try:
            with open(self._p('out.log'), errors='replace') as f:
                return ''.join(f.readlines()[-lines:])
        except OSError:
            return ''

    def fetch_detached_log(self):
        """Nothing to fetch: the output is already local."""
