# dockerpty: util.py
#
# Copyright 2014 Chris Corbyn <chris@w3style.co.uk>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from contextlib import contextmanager
import termios
import struct
import fcntl
import select
import os
from Queue import Queue
import re
import socket
import tempfile
import threading
import time
import traceback


def set_pty_size(fd, size):
    """
    Resize the PTY at `fd` to (rows, cols) size.
    """

    rows, cols = size
    fcntl.ioctl(
        fd,
        termios.TIOCSWINSZ,
        struct.pack('hhhh', rows, cols, 0, 0)
    )


def wait(fd, timeout=2):
    """
    Wait until data is ready for reading on `fd`.
    """

    return select.select([fd], [], [], timeout)[0]


def printable(text):
    """
    Convert text to only printable characters, as a user would see it.
    """

    ansi = re.compile(r'\x1b\[[^Jm]*[Jm]')
    return ansi.sub('', text).rstrip()


def write(fd, data):
    """
    Write `data` to the PTY at `fd`.
    """

    os.write(fd, data)


def readchar(fd):
    """
    Read a character from the PTY at `fd`, or nothing if no data to read.
    """

    while True:
        ready = wait(fd)
        if len(ready) == 0:
            return ''
        else:
            for s in ready:
                return os.read(s, 1)


def readline(fd):
    """
    Read a line from the PTY at `fd`, or nothing if no data to read.

    The line includes the line ending.
    """

    output = []
    while True:
        char = readchar(fd)
        if char:
            output.append(char)
            if char == "\n":
                return ''.join(output)
        else:
            return ''.join(output)


def read(fd):
    """
    Read all output from the PTY at `fd`, or nothing if no data to read.
    """

    output = []
    while True:
        line = readline(fd)
        if line:
            output.append(line)
        else:
            return "".join(output)


def read_printable(fd):
    """
    Read all output from the PTY at `fd` as a user would see it.

    Warning: This is not exhaustive; it won't render Vim, for example.
    """

    lines = read(fd).splitlines()
    return "\n".join([printable(line) for line in lines]).lstrip("\r\n")


def exit_code(pid, timeout=5):
    """
    Wait up to `timeout` seconds for `pid` to exit and return its exit code.

    Returns -1 if the `pid` does not exit.
    """

    start = time.time()
    while True:
        _, status = os.waitpid(pid, os.WNOHANG)
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        else:
            if (time.time() - start) > timeout:
                return -1


def container_running(client, container, duration=2):
    """
    Predicate to check if a container continues to run after `duration` secs.
    """

    time.sleep(duration)
    config = client.inspect_container(container)
    return config['State']['Running']


@contextmanager
def a_temp_file():
    fle = None
    try:
        fle = tempfile.NamedTemporaryFile(delete=False).name
        yield fle
    finally:
        if fle and os.path.exists(fle):
            os.remove(fle)


@contextmanager
def a_socket(filename, method="bind"):
    sckt = None
    try:
        sckt = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        getattr(sckt, method)(filename)
        if method == 'bind':
            sckt.listen(1)
        yield sckt
    finally:
        if sckt:
            sckt.close()


@contextmanager
def slow_write_thread(message, wait=0.5, chunk_size=5):
    """
    Start a thread that slowly writes the message to a socket.

    Yield an info dictionary
    ``{ 'started': <bool>, 'stopped': <bool>, 'please_stop': <func>
      , 'data': <socket>, 'reader': object(read())
      }``

    This socket can be read via info["reader"].read(<num>)

    Calling ``info['please_stop']`` will set ``stopped`` to True.

    The thread stops when all the message is written
    or if the context manager is exited
    or if ``info['stopped']`` is set to True
    """
    with a_temp_file() as filename:
        os.remove(filename)
        info = {'stopped': False, "started": False, "error": None, "connected": False}
        info["please_stop"] = lambda: dict.__setitem__(info, 'stop', True)

        def writer():
            """Slowly write msg to sckt"""
            try:
                with a_socket(filename, 'bind') as writer:
                    while True:
                        if not info["connected"]:
                            time.sleep(0.1)
                            continue

                        nxt = message.read(chunk_size)
                        if not nxt or info["stopped"]:
                            info["stopped"] = True
                            break

                        writer.send(nxt)
                        info['started'] = True

                        if info["stopped"]:
                            break
                        time.sleep(wait)
            except Exception as e:
                info["error"] = e
                info["traceback"] = traceback.format_exc()
            finally:
                info["stopped"] = True

        threading.Thread(target=writer).start()

        while not info["started"]:
            if info["stopped"]:
                msg = "Something about the write thread failed\n{0}: {1}\n{2}"
                assert False, msg.format(
                    info["error"].__class__.__name__, info["error"], info["traceback"]
                )
            time.sleep(0.5)

        with a_socket(filename, 'connect') as reader:
            read = lambda fake_reader, num: reader.recv(num)
            info["reader"] = type("Reader", (object, ), {"read": read})()
            info["connected"] = True

            try:
                yield info
                if not info["stopped"]:
                    assert False, "The test finished before the writer thread!"
                if info["error"]:
                    assert False, "The write thread failed\n{0}".format(info["error"])
            except AssertionError as error:
                if not info["error"]:
                    raise
                else:
                    msg = "The write thread and the test both failed\n{0}: {1}\n{2}: {3}"
                    assert False, msg.format(
                        info["error"].__class__.__name__, info["error"],
                        error.__class__.__name__, error
                    )
