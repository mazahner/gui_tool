#
# Copyright (C) 2016  UAVCAN Development Team  <uavcan.org>
#
# This software is distributed under the terms of the MIT License.
#
# Author: Pavel Kirienko <pavel.kirienko@zubax.com>
#

import os
import sys
import queue
import uavcan
import logging
import multiprocessing
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer
from .window import PlotterWindow

logger = logging.getLogger(__name__)

try:
    # noinspection PyUnresolvedReferences
    sys.getwindowsversion()
    RUNNING_ON_WINDOWS = True
except AttributeError:
    RUNNING_ON_WINDOWS = False
    PARENT_PID = os.getppid()


class IPCChannel:
    """
    This class is built as an abstraction over the underlying IPC communication channel.
    """
    QUEUE_DEPTH = 1000000

    def __init__(self):
        # Queue is slower than pipe, but it allows to implement non-blocking sending easier,
        # and the buffer can be arbitrarily large.
        self._q = multiprocessing.Queue(self.QUEUE_DEPTH)

    def send_nonblocking(self, obj):
        try:
            self._q.put_nowait(obj)
        except queue.Full:
            pass

    def receive_nonblocking(self):
        """Returns: (True, object) if successful, (False, None) if no data to read """
        try:
            return True, self._q.get_nowait()
        except queue.Empty:
            return False, None


IPC_COMMAND_STOP = 'stop'


def _process_entry_point(channel):
    logger.info('Plotter process started with PID %r', os.getpid())
    app = QApplication(sys.argv)    # Inheriting args from the parent process

    def exit_if_should():
        if RUNNING_ON_WINDOWS:
            return False
        else:
            return os.getppid() != PARENT_PID       # Parent is dead

    exit_check_timer = QTimer()
    exit_check_timer.setSingleShot(False)
    exit_check_timer.timeout.connect(exit_if_should)
    exit_check_timer.start(2000)

    def get_message():
        received, obj = channel.receive_nonblocking()
        if received:
            if obj == IPC_COMMAND_STOP:
                logger.info('Plotter process has received a stop request, goodbye')
                app.exit(0)
            else:
                return obj

    win = PlotterWindow(get_message)
    win.show()

    logger.info('Plotter process %r initialized successfully, now starting the event loop', os.getpid())
    exit(app.exec_())


def _extract_struct_fields(m):
    if isinstance(m, uavcan.transport.CompoundValue):
        out = {}
        for field_name, field in uavcan.get_fields(m).items():
            if uavcan.is_union(m) and uavcan.get_active_union_field(m) != field_name:
                continue
            val = _extract_struct_fields(field)
            if val is not None:
                out[field_name] = val
        return out
    elif isinstance(m, uavcan.transport.ArrayValue):
        # cannot say I'm breaking the rules
        container = bytes if uavcan.get_uavcan_data_type(m).is_string_like else list
        # if I can glue them back together
        return container(filter(lambda x: x is not None, (_extract_struct_fields(item) for item in m)))
    elif isinstance(m, uavcan.transport.PrimitiveValue):
        return m.value
    elif isinstance(m, (int, float, bool)):
        return m
    elif isinstance(m, uavcan.transport.VoidValue):
        pass
    else:
        raise ValueError(':(')


class MessageTransfer:
    def __init__(self, tr):
        self.source_node_id = tr.source_node_id
        self.ts_mono = tr.ts_monotonic
        self.ts_real = tr.ts_real
        self.data_type_name = uavcan.get_uavcan_data_type(tr.payload).full_name
        self.fields = _extract_struct_fields(tr.payload)


class PlotterManager:
    def __init__(self, node):
        self._node = node
        self._inferiors = []    # process object, channel
        self._hook_handle = None

    def _transfer_hook(self, tr):
        if tr.direction == 'rx' and not tr.service_not_message and len(self._inferiors):
            msg = MessageTransfer(tr)
            for proc, channel in self._inferiors[:]:
                if proc.is_alive():
                    channel.send_nonblocking(msg)
                else:
                    logger.info('Plotter process %r appears to be dead, removing', proc)
                    self._inferiors.remove((proc, channel))

    def spawn_plotter(self):
        channel = IPCChannel()

        if self._hook_handle is None:
            self._hook_handle = self._node.add_transfer_hook(self._transfer_hook)

        proc = multiprocessing.Process(target=_process_entry_point, name='plotter', args=(channel,))
        proc.daemon = True
        proc.start()

        self._inferiors.append((proc, channel))

        logger.info('Spawned new plotter process %r', proc)

    def close(self):
        try:
            self._hook_handle.remove()
        except Exception:
            pass

        for _, channel in self._inferiors:
            channel.send_nonblocking(IPC_COMMAND_STOP)

        for proc, _ in self._inferiors:
            proc.join(1)

        for proc, _ in self._inferiors:
            try:
                proc.terminate()
            except Exception:
                pass
