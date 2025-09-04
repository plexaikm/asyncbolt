"""Bolt Protocol for Asyncio"""
import asyncio
import logging

from enum import IntEnum

from asyncbolt import buffer, messaging, parser
from asyncbolt.exception import HandshakeError, ProtocolError, ServerFailedError, ServerIgnoredError

logger = logging.getLogger(__name__)
log_debug = logger.debug
log_info = logger.info
log_warning = logger.warning
log_error = logger.error


class HandshakeProtocolState(IntEnum):
    PROTOCOL_UNINITIALIZED = 0
    PROTOCOL_VERSION_NEGOTIATE = 1
    PROTOCOL_CAPABILITIES_NEGOTIATE = 2
    PROTOCOL_DONE = 3


class ServerProtocolState(IntEnum):
    PROTOCOL_UNINITIALIZED = 0
    PROTOCOL_READY = 1
    PROTOCOL_RUNNING = 2
    PROTOCOL_FAILED = 3
    PROTOCOL_CLOSING = 4
    PROTOCOL_CLOSED = 5


class BoltProtocol(asyncio.Protocol):

    PROTOCOL_VERSION,  = messaging.unpack_v(messaging.MANIFEST_V1)
    FINAL_VERSION, = messaging.unpack_v(messaging.V50)
    # PROTOCOL_VERSION, = messaging.unpack_v(messaging.V3)

    def __init__(self, loop):
        self.loop = loop
        self.read_buffer = buffer.ChunkedReadBuffer()
        self.write_buffer = buffer.ChunkedWriteBuffer(8192)
        self.transport = None
        self.transport_write = None
        self.parser = parser.BoltParser(self)

    def flush(self):
        for message in self.write_buffer.flush():
            log_debug("Writing message to transport\n'{}'\n".format(message))
            self.transport_write(message)

    def connection_lost(self, exc):
        raise NotImplementedError

    def connection_made(self, transport):
        self.transport = transport
        self.transport_write = self.transport.write

    def data_received(self, data):
        # log_debug('Data received {}'.format(data.hex()))
        self.parser.feed_data(data)
        log_debug('Data received:\n{}\n'.format(data))
        self.handle_incoming()

    def handle_incoming(self):
        raise NotImplementedError

    def on_chunk(self, chunk):
        self.read_buffer.feed_data(chunk)

    def on_message_complete(self):
        self.read_buffer.feed_eof()


class BoltServerProtocol(BoltProtocol):
    """Implement the Bolt protocol for server"""

    def __init__(self, loop, *, server=None):
        super().__init__(loop)
        self.server = server
        self.handshake_state = HandshakeProtocolState.PROTOCOL_UNINITIALIZED
        self.state = ServerProtocolState.PROTOCOL_UNINITIALIZED

    def close(self):
        self.state = ServerProtocolState.PROTOCOL_CLOSED
        self.transport = None

    # Asyncio protocol methods
    def connection_lost(self, exc):
        log_debug('Connection lost {}'.format(exc))
        self.close()

    def data_received(self, data):
        if not self.handshake_state == HandshakeProtocolState.PROTOCOL_DONE:
            self.check_protocol(data)
        else:
            super().data_received(data)

    # Bolt handshake/initialization methods
    def check_protocol(self, data):
        """Verify requested protocol against supported versions"""
        # TODO handle future versions
        try:
            data_view = memoryview(data)
            if self.handshake_state == HandshakeProtocolState.PROTOCOL_UNINITIALIZED:
                # log_debug('Handshake - client data received {}'.format(data_view.hex()))
                magic = data_view[:4]
                if not magic == messaging.MAGIC:
                    raise ProtocolError('Incorrect magic byte sequence')
                v1, v2, v3, v4 = messaging.unpack_4v(data_view[4:])
                # Only support MANIFEST V1 for and version 5.0
                if not v1 == self.PROTOCOL_VERSION:
                    raise ProtocolError('Invalid protocol version')
                # log_debug('Handshake - server Using protocol v5.0')
                self.transport_write(messaging.V50)
                # log_debug('Handshake - server Using protocol Manifest v1 for v5.0')
                # self.transport_write(messaging.MANIFEST_V1)
                # self.transport_write(messaging.MANIFEST_LEN_1)
                # self.transport_write(messaging.MANIFEST_RANGE_V50_V50)
                # self.transport_write(messaging.MANIFEST_NO_CAPABILITIES)
                # log_debug('Handshake - server capabilities sent {}'.format(messaging.MANIFEST_NO_CAPABILITIES.hex()))
                # self.handshake_state = HandshakeProtocolState.PROTOCOL_VERSION_NEGOTIATE
            # elif self.handshake_state == HandshakeProtocolState.PROTOCOL_VERSION_NEGOTIATE:
            #     log_debug('Handshake - client version received {}'.format(data_view.hex()))
            #     vc, = messaging.unpack_v(data_view)
            #     if not vc == self.FINAL_VERSION:
            #         raise ProtocolError('Unsupported version requested')
            #     self.handshake_state = HandshakeProtocolState.PROTOCOL_CAPABILITIES_NEGOTIATE
            # elif self.handshake_state == HandshakeProtocolState.PROTOCOL_CAPABILITIES_NEGOTIATE:
            #     log_debug('Handshake - client capabilities received {}'.format(data_view.hex()))
            #     if data != messaging.MANIFEST_NO_CAPABILITIES:
            #         raise ProtocolError('Unsupported capabilities requested')
                self.handshake_state = HandshakeProtocolState.PROTOCOL_DONE
        except Exception as e:
            raise HandshakeError from e

    def get_server_metadata(self):
        """Inheriting server protocol should implement this method"""

    def on_hello(self, extra):
        """Inheriting server protocol should implement this method"""

    # Hooks for custom behavior in inheriting classes
    def on_ack_failure(self):
        """Called when server receives ACK_FAILURE message"""

    def on_discard(self, extra):
        """Called when server receives DISCARD message"""

    def on_pull(self, extra):
        """Required! Send completed tasks to client!"""
        raise NotImplementedError

    def on_run(self, statement, parameters, extra):
        """Required! Run task received from client"""
        raise NotImplementedError

    def on_begin(self, extra):
        """Inheriting server protocol should implement this method"""

    def on_commit(self) -> dict:
        """Inheriting server protocol should implement this method"""

    def on_rollback(self):
        """Inheriting server protocol should implement this method"""

    def on_reset(self):
        """Called when server receives ACK_FAILURE message"""

    def handle_incoming(self):
        while self.read_buffer.ready:
            data = messaging.deserialize_message(self.read_buffer)
            if self.state == ServerProtocolState.PROTOCOL_READY:
                if data.signature == messaging.Message.RUN:
                    self.state = ServerProtocolState.PROTOCOL_RUNNING
                    self.on_run(data.statement, data.parameters, data.extra)
                elif data.signature == messaging.Message.BEGIN:
                    self.on_begin(data.extra)
                    self.success({})
                    self.flush()
                elif data.signature == messaging.Message.COMMIT:
                    extra = self.on_commit()
                    self.success(extra)
                    self.flush()
                elif data.signature == messaging.Message.ROLLBACK:
                    self.on_rollback()
                    self.success({})
                    self.flush()
                elif data.signature == messaging.Message.RESET:
                    self.reset()
                else:
                    self.state = ServerProtocolState.PROTOCOL_FAILED
                    self.failure({})
                    self.flush()
            elif self.state == ServerProtocolState.PROTOCOL_RUNNING:
                if data.signature == messaging.Message.PULL:
                    # Client ready to consume stream
                    self.on_pull(data.extra)
                    self.state = ServerProtocolState.PROTOCOL_READY
                elif data.signature == messaging.Message.DISCARD:
                    self.on_discard(data.extra)
                    self.write_buffer = buffer.ChunkedWriteBuffer(8192)
                    self.state = ServerProtocolState.PROTOCOL_READY
                else:
                    self.state = ServerProtocolState.PROTOCOL_FAILED
                    self.failure({})
                    self.flush()
            elif self.state == ServerProtocolState.PROTOCOL_FAILED:
                if data.signature == messaging.Message.ACK_FAILURE:
                    self.on_ack_failure()
                    self.state = ServerProtocolState.PROTOCOL_READY
                    self.success({})
                    self.flush()
                elif data.signature == messaging.Message.RESET:
                    self.reset()
                else:
                    self.ignored({})
                    self.flush()
            elif self.state == ServerProtocolState.PROTOCOL_UNINITIALIZED:
                if data.signature == messaging.Message.HELLO:
                    self.on_hello(data.extra)
                    self.state = ServerProtocolState.PROTOCOL_READY
                    metadata = self.get_server_metadata()
                    self.success(metadata)
                    self.flush()
                else:
                    self.state = ServerProtocolState.PROTOCOL_FAILED
                    self.failure({})
                    self.flush()
            else:
                self.state = ServerProtocolState.PROTOCOL_FAILED
                self.failure({})
                self.flush()

    def reset(self):
        self.on_reset()
        # Reset the server session state, clear buffers
        self.write_buffer = buffer.ChunkedWriteBuffer(8192)
        while self.read_buffer.queue:
            self.read_buffer.queue_popleft()
            self.ignored({})
        self.state = ServerProtocolState.PROTOCOL_READY
        self.success({})
        self.flush()

    # Bolt message packing methods
    def record(self, fields):
        messaging.serialize_message(messaging.Message.RECORD, buf=self.write_buffer, params=(fields,))

    def success(self, metadata):
        messaging.serialize_message(messaging.Message.SUCCESS, buf=self.write_buffer, params=(metadata,))

    def failure(self, metadata):
        messaging.serialize_message(messaging.Message.FAILURE, buf=self.write_buffer, params=(metadata,))

    def ignored(self, metadata):
        messaging.serialize_message(messaging.Message.IGNORED, buf=self.write_buffer, params=(metadata,))


class BoltClientProtocol(BoltProtocol):
    """Implement the Bolt protocol for client"""

    def __init__(self, loop, **kwargs):
        super().__init__(loop)
        self.waiter = asyncio.Future(loop=self.loop)
        self.handshake_waiter = asyncio.Future(loop=self.loop)

    # Asyncio protocol methods
    def connection_lost(self, exc):
        self.loop.stop()

    def connection_made(self, transport):
        log_debug('Connection made to: {}'.format(transport.get_extra_info('peername')))
        super().connection_made(transport)
        self.do_handshake()

    def data_received(self, data):
        """
        Called when client receives data. This method handles the connection handshake.
        All session specific information (initialization, etc.), is handled by the ClientSession object,
        which provides the main client API for asyncbolt.
        """
        if not self.handshake_waiter.done():
            try:
                # Server responds with 32 bit protocol version
                # TODO: Make this work for more than V1
                v, = messaging.unpack_v(data)
                assert v == self.PROTOCOL_VERSION
                log_info('Using Bolt protocol version {}\n'.format(data))
            except Exception as e:
                log_error(e)
                self.handshake_waiter.set_exception(HandshakeError('Could not agree on protocol version'))
            else:
                self.handshake_waiter.set_result(True)
        else:
            super().data_received(data)

    def close(self):
        """Close client socket"""
        self.transport.close()

    # Bolt handshake/initialization methods
    def do_handshake(self):
        # TODO: Add support for future versions
        version_info = messaging.V1 + messaging.NULL_V + messaging.NULL_V + messaging.NULL_V
        log_debug('Sending handshake with version info: {}'.format(version_info))
        self.transport_write(messaging.MAGIC + version_info)

    def get_init_params(self):
        """
        Inheriting client protocol must should implement this method. Must return a tuple containing
        required BOLT INIT message params clientName and authToken,
        :returns tuple: tuple of (clientName: str, authToken: dict)
        """
        return 'AsyncBolt/1.0', {"scheme": "none"}

    # Bolt communication logic methods
    def handle_incoming(self):
        if self.read_buffer.ready:
            # Protocol received a complete message
            if not self.waiter.done():
                self.waiter.set_result(True)

    async def read(self):
        """Main API. Read a result from the incoming results stream"""
        # Await waiter
        if not self.read_buffer.ready:

            await self.waiter
            self.reset_waiter()
        data = messaging.deserialize_message(self.read_buffer)
        if data[0] == messaging.Message.RECORD:
            return data
        if data[0] == messaging.Message.SUCCESS:
            return data
        if data[0] == messaging.Message.RECORD:
            return data
        if data[0] == messaging.Message.FAILURE:
            raise ServerFailedError("{}".format(data.metadata))
        if data[0] == messaging.Message.IGNORED:
            raise ServerIgnoredError()

    def reset_waiter(self):
        self.waiter = asyncio.Future(loop=self.loop)

    # Bolt message packing methods
    def hello(self, client_name, metadata):
        messaging.serialize_message(messaging.Message.HELLO, buf=self.write_buffer, params=(client_name, metadata))

    def run(self, statement, parameters):
        messaging.serialize_message(messaging.Message.RUN, buf=self.write_buffer, params=(statement, parameters))

    def discard(self, extra):
        messaging.serialize_message(messaging.Message.DISCARD, buf=self.write_buffer, params=(extra,))

    def pull(self, extra):
        messaging.serialize_message(messaging.Message.PULL, buf=self.write_buffer, params=(extra,))

    def ack_failure(self):
        messaging.serialize_message(messaging.Message.ACK_FAILURE, buf=self.write_buffer)

    def reset(self):
        messaging.serialize_message(messaging.Message.RESET, buf=self.write_buffer)
