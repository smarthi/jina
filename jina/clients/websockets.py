import asyncio
from typing import Callable, List

from .base import BaseClient
from .helper import callback_exec
from ..importer import ImportExtensions
from ..logging.profile import TimeContext, ProgressBar
from ..types.request import Request

if False:
    from ..types.request import Response


class WebSocketBaseClient(BaseClient):
    async def _get_results(self,
                           input_fn: Callable,
                           on_done: Callable,
                           on_error: Callable = None,
                           on_always: Callable = None, **kwargs):
        """
        :meth:`send_requests`
            traverses through the request iterator
            sends each request & awaits :meth:` websocket.send`
            sends & awaits `byte(True)` to acknowledge request iteraor is empty
        traversal logic:
            starts an independent task :meth:`send_requests`
            awaits on each response from :meth:` websocket.recv` (done in an async loop)
            this makes sure client makes concurrent invocations
        await exit strategy:
            :meth:`send_requests` keeps track of num_requests sent
            async recv loop keeps track of num_responses received
            client exits out of await when num_requests == num_responses
        """
        with ImportExtensions(required=True):
            import websockets

        result = []  # type: List['Response']
        self.input_fn = input_fn
        req_iter, tname = self._get_requests(**kwargs)
        try:
            client_info = f'{self.args.host}:{self.args.port_expose}'
            # setting `max_size` as None to avoid connection closure due to size of message
            # https://websockets.readthedocs.io/en/stable/api.html?highlight=1009#module-websockets.protocol

            async with websockets.connect(f'ws://{client_info}/stream', max_size=None) as websocket:
                # To enable websockets debug logs
                # https://websockets.readthedocs.io/en/stable/cheatsheet.html#debugging
                self.logger.success(f'Connected to the gateway at {client_info}')
                self.num_requests = 0
                self.num_responses = 0

                async def send_requests(request_iterator):
                    while True:
                        try:
                            next_request = next(request_iterator)
                            await websocket.send(next_request.SerializeToString())
                            self.num_requests += 1
                        except StopIteration:
                            # Server has no way of knowing when to stop the await on sending response back to the client
                            # We send one last message to say `request_iterator` is completed.
                            # On the client side, this :meth:`send` doesn't need to be awaited with a :meth:`recv`
                            await websocket.send(bytes(True))
                            return

                with ProgressBar(task_name=tname) as p_bar, TimeContext(tname):
                    # Unlike gRPC, any arbitrary function (generator) cannot be passed via websockets.
                    # Simply iterating through the `req_iter` makes the request-response sequential.
                    # To make client unblocking, :func:`send_requests` and `recv_responses` are separate tasks

                    asyncio.create_task(send_requests(request_iterator=req_iter))
                    async for response_bytes in websocket:
                        # When we have a stream of responses, instead of doing `await websocket.recv()`,
                        # we need to traverse through the websocket to recv messages.
                        # https://websockets.readthedocs.io/en/stable/faq.html#why-does-the-server-close-the-connection-after-processing-one-message

                        response = Request(response_bytes).to_response()
                        callback_exec(response=response,
                                      on_error=on_error,
                                      on_done=on_done,
                                      on_always=on_always,
                                      continue_on_error=self.args.continue_on_error,
                                      logger=self.logger)
                        p_bar.update(self.args.batch_size)
                        if self.args.return_results:
                            result.append(response)
                        self.num_responses += 1
                        if self.num_requests == self.num_responses:
                            break

        except websockets.exceptions.ConnectionClosedOK:
            self.logger.warning(f'Client got disconnected from the websocket server')
        except websockets.exceptions.WebSocketException as e:
            self.logger.error(f'Got following error while streaming requests via websocket: {repr(e)}')
        finally:
            if self.args.return_results:
                return result