"""
An extension to the standard supervisor RPC interface which subscribes
to internal supervisor events and dispatches them to 0RPC.

disadvantages: it depends on supervisor internal supervisor.events.subscribe
               interface so its usage is quite risky.
advantages: it avoids creating an eventlistener process just to forward events.

The python environment where supervisor runs must have multivisor installed
"""

import os
import queue
import logging
import functools
import threading

from gevent import spawn, hub, sleep
from gevent.queue import Queue
from zerorpc import stream, Server, LostRemote

from supervisor.http import NOT_DONE_YET
from supervisor.rpcinterface import SupervisorNamespaceRPCInterface
from supervisor.events import subscribe, Event, getEventNameByType
# unsubscribe only appears in supervisor > 3.3.4
try:
    from supervisor.events import unsubscribe
except:
    unsubscribe = lambda x, y: None

from .util import sanitize_url


DEFAULT_BIND = 'tcp://*:9002'


def sync(klass):
    def wrap_func(meth):
        @functools.wraps(meth)
        def wrapper(*args, **kwargs):
            args[0]._log.debug('0RPC: called {}'.format(meth.__name__))
            result = meth(*args, **kwargs)
            if callable(result):
                r = NOT_DONE_YET
                while r is NOT_DONE_YET:
                    sleep(0.1)
                    r = result()
                result = r
            return result
        return wrapper

    for name in dir(klass):
        if name.startswith('_') or name == 'event_stream':
            continue
        meth = getattr(klass, name)
        if not callable(meth):
            continue
        setattr(klass, name, wrap_func(meth))
    return klass


@sync
class MultivisorNamespaceRPCInterface(SupervisorNamespaceRPCInterface):

    def __init__(self, supervisord, bind):
        SupervisorNamespaceRPCInterface.__init__(self, supervisord)
        self._bind = bind
        self._channel = queue.Queue()
        self._event_channels = set()
        self._server = None
        self._watcher = None
        self._shutting_down = False
        self._log = logging.getLogger('MVRPC')

    def _start(self):
        subscribe(Event, self._handle_event)

    def _shutdown(self):
        unsubscribe(Event, self._handle_event)
        self._shutting_down = True

    def _process_event(self, event):
        if self._shutting_down:
            return
        event_name = getEventNameByType(event.__class__)
        if event_name == 'SUPERVISOR_STATE_CHANGE_STOPPING':
            self._log.warn('noticed that supervisor is dying')
            self._shutdown()
        elif event_name.startswith('TICK'):
            return
        if not self._event_channels:
            # if no client is listening avoid building the event
            return
        try:
            # old supervisor version
            payload_str = event.payload()
        except AttributeError:
            payload_str = str(event)
        payload = dict((x.split(':') for x in payload_str.split()))
        if event_name.startswith('PROCESS_STATE'):
            pname = "{}:{}".format(payload['groupname'], payload['processname'])
            payload['process'] = self.getProcessInfo(pname)
        # broadcast the event to clients
        server = self.supervisord.options.identifier
        new_event = dict(pool='multivisor', server=server,
                         eventname=event_name, payload=payload)
        for channel in self._event_channels:
            channel.put(new_event)

    # called on 0RPC server thread
    def _dispatch_event(self):
        while not self._channel.empty():
            event = self._channel.get()
            self._process_event(event)

    # called on main thread
    def _handle_event(self, event):
        if self._server is None:
            reply = start_rpc_server(self, self._bind)
            self._server, self._watcher = reply
        self._channel.put(event)
        self._watcher.send()

    @stream
    def event_stream(self):
        self._log.info('client connected to stream')
        channel = Queue()
        self._event_channels.add(channel)
        try:
            yield 'First event to trigger connection. Please ignore me!'
            for event in channel:
                yield event
        except LostRemote as e:
            self._log.info('remote end of stream disconnected')
        finally:
            self._event_channels.remove(channel)


def start_rpc_server(multivisor, bind):
    future_server = queue.Queue(1)
    th = threading.Thread(target=run_rpc_server, name='RPCServer',
                          args=(multivisor, bind, future_server))
    th.daemon = True
    th.start()
    return future_server.get()


def run_rpc_server(multivisor, bind, future_server):
    multivisor._log.info('0RPC: spawn server on {}...'.format(os.getpid()))
    watcher = hub.get_hub().loop.async()
    watcher.start(lambda: spawn(multivisor._dispatch_event))
    server = Server(multivisor)
    server.bind(bind)
    future_server.put((server, watcher))
    multivisor._log.info('0RPC: server running!')
    server.run()


def make_rpc_interface(supervisord, bind=DEFAULT_BIND):
    # Uncomment following lines to configure python standard logging
    #log_level = logging.INFO
    #log_fmt = '%(threadName)-8s %(levelname)s %(asctime)-15s %(name)s: %(message)s'
    #logging.basicConfig(level=log_level, format=log_fmt)

    url = sanitize_url(bind, protocol='tcp', host='*', port=9002)
    multivisor = MultivisorNamespaceRPCInterface(supervisord, url['url'])
    multivisor._start()
    return multivisor
