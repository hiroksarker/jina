import argparse
import asyncio
from asyncio import Future
from typing import AsyncGenerator, Dict

from ....helper import typename, get_or_reuse_loop
from ....logging.logger import JinaLogger
from ....types.message import Message

__all__ = ['PrefetchCaller']

if False:
    from ...zmq import AsyncZmqlet


class PrefetchCaller:
    """An async zmq request sender to be used in the Gateway"""

    def __init__(self, args: argparse.Namespace, zmqlet: 'AsyncZmqlet'):
        """
        :param args: args from CLI
        :param zmqlet: zeromq object
        """
        self.args = args
        self.zmqlet = zmqlet
        self.name = args.name or self.__class__.__name__

        self.logger = JinaLogger(self.name, **vars(args))
        self._message_buffer: Dict[str, Future[Message]] = dict()
        self._is_running = True
        self._receive_task = get_or_reuse_loop().create_task(self._receive())

    async def _receive(self):
        try:
            while self._is_running:
                message = await self.zmqlet.recv_message(callback=lambda x: x.response)
                if message.request_id in self._message_buffer:
                    self._message_buffer[message.request_id].set_result(message)
                    del self._message_buffer[message.request_id]
                else:
                    self.logger.warning(
                        f'Discarding unexpected message with request id {message.request_id}'
                    )
        except asyncio.CancelledError:
            pass

    async def close(self):
        """
        Stop receiving messages
        """
        self._is_running = False
        self._receive().cancel()

    async def send(self, request_iterator, *args) -> AsyncGenerator[None, Message]:
        """
        Async call to receive Requests and build them into Messages.

        :param request_iterator: iterator of requests.
        :param args: additional arguments
        :yield: message
        """
        self.args: argparse.Namespace
        self.zmqlet: 'AsyncZmqlet'
        self.logger: JinaLogger

        async def prefetch_req(num_req, fetch_to):
            """
            Fetch and send request.

            :param num_req: number of requests
            :param fetch_to: the task list storing requests
            :return: False if append task to :param:`fetch_to` else False
            """
            for _ in range(num_req):
                try:
                    if hasattr(request_iterator, '__anext__'):
                        next_request = await request_iterator.__anext__()
                    elif hasattr(request_iterator, '__next__'):
                        next_request = next(request_iterator)
                    else:
                        raise TypeError(
                            f'{typename(request_iterator)} does not have `__anext__` or `__next__`'
                        )

                    future = Future()
                    self._message_buffer[next_request.request_id] = future
                    asyncio.create_task(
                        self.zmqlet.send_message(
                            Message(None, next_request, 'gateway', **vars(self.args))
                        )
                    )

                    fetch_to.append(future)
                except (StopIteration, StopAsyncIteration):
                    return True
            return False

        prefetch_task = []
        is_req_empty = await prefetch_req(self.args.prefetch, prefetch_task)
        if is_req_empty and not prefetch_task:
            self.logger.error(
                'receive an empty stream from the client! '
                'please check your client\'s inputs, '
                'you can use "Client.check_input(inputs)"'
            )
            return

        # the total num requests < self.args.prefetch
        if is_req_empty:
            for r in asyncio.as_completed(prefetch_task):
                yield await r
        else:
            # if there are left over (`else` clause above is unnecessary for code but for better readability)
            onrecv_task = []
            # the following code "interleaves" prefetch_task and onrecv_task, when one dries, it switches to the other
            while prefetch_task:
                self.logger.debug(
                    f'send: {self.zmqlet.msg_sent} '
                    f'recv: {self.zmqlet.msg_recv} '
                    f'pending: {self.zmqlet.msg_sent - self.zmqlet.msg_recv}'
                )
                onrecv_task.clear()
                for r in asyncio.as_completed(prefetch_task):
                    yield await r
                    if not is_req_empty:
                        is_req_empty = await prefetch_req(
                            self.args.prefetch_on_recv, onrecv_task
                        )

                # this list dries, clear it and feed it with on_recv_task
                prefetch_task.clear()
                prefetch_task = [j for j in onrecv_task]
