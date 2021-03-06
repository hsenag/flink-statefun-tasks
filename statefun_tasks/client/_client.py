from statefun_tasks import TaskRequest, TaskResult, TaskException, DefaultSerialiser, PipelineBuilder
from statefun_tasks.client import TaskError

from google.protobuf.any_pb2 import Any
from kafka import KafkaProducer, KafkaConsumer, TopicPartition

import logging
import socket
from uuid import uuid4
from threading import Thread
import asyncio
from concurrent.futures import Future

_log = logging.getLogger('FlinkTasks')


class FlinkTasksClient(object):
    
    def __init__(self, kafka_broker_url, request_topic, reply_topic, group_id=None, serialiser=None):
        self._kafka_broker_url = kafka_broker_url
        self._requests = {}

        self._request_topic = request_topic
        self._reply_topic = reply_topic
        self._group_id = group_id
        self._serialiser = serialiser if serialiser is not None else DefaultSerialiser()

        self._producer = KafkaProducer(bootstrap_servers=[kafka_broker_url])

        self._consumer = KafkaConsumer(
            self._reply_topic,
            bootstrap_servers=[self._kafka_broker_url],
            auto_offset_reset='earliest',
            group_id=self._group_id)

        self._consumer_thread = Thread(target=self._consume, args=())
        self._consumer_thread.daemon = True
        self._consumer_thread.start()

    def submit(self, pipeline: PipelineBuilder, topic=None):
        task_request = pipeline.to_task_request(self._serialiser)
        return self._submit_request(task_request, topic=topic)

    async def submit_async(self, pipeline: PipelineBuilder, topic=None):
        future, _ = self.submit(pipeline, topic=topic)
        return await asyncio.wrap_future(future)

    def _submit_request(self, task_request: TaskRequest, topic=None):
        if task_request.id is None or task_request.id == "":
            raise ValueError('Task request is missing an id')

        if task_request.type is None or task_request.type == "":
            raise ValueError('Task request is missing a type')

        future = Future()
        self._requests[task_request.id] = future

        task_request.reply_topic = self._reply_topic

        key = task_request.id.encode('utf-8')
        val = task_request.SerializeToString()

        topic = self._request_topic if topic is None else topic
        self._producer.send(topic=topic, key=key, value=val)
        self._producer.flush()

        return future, task_request.id

    def _consume(self):
        while True:
            try:
                for message in self._consumer:

                    _log.info(f'Message received - {message}')

                    any = Any()
                    any.ParseFromString(message.value)

                    if any.Is(TaskException.DESCRIPTOR):
                        self._raise_exception(any)
                    elif any.Is(TaskResult.DESCRIPTOR):
                        self._return_result(any)

            except Exception as ex:
                _log.warning(f'Exception in consumer thread - {ex}', exc_info=ex)

    def _return_result(self, any: Any):
        task_result = TaskResult()
        any.Unpack(task_result)

        correlation_id = task_result.correlation_id

        future = self._requests.get(correlation_id, None)

        if future is not None:
            del self._requests[correlation_id]

            try:
                result, _ = self._serialiser.deserialise_result(task_result)
                future.set_result(result)
            except Exception as ex:
                future.set_exception(ex)

    def _raise_exception(self, any: Any):
        task_exception = TaskException()
        any.Unpack(task_exception)

        correlation_id = task_exception.correlation_id

        future = self._requests.get(correlation_id, None)

        if future is not None:
            del self._requests[correlation_id]

            try:
                future.set_exception(TaskError(task_exception))
            except Exception as ex:
                future.set_exception(ex)


class FlinkTasksClientFactory():
    __clients = {}

    @staticmethod
    def get_client(kafka_broker_url, request_topic, reply_topic_prefix, serialiser=None) -> FlinkTasksClient:
        key = f'{kafka_broker_url}.{request_topic}.{reply_topic_prefix}'

        if key not in FlinkTasksClientFactory.__clients:

            reply_topic = f'{reply_topic_prefix}.{socket.gethostname()}.{str(uuid4())}'
            client = FlinkTasksClient(kafka_broker_url, request_topic, reply_topic, serialiser=serialiser)
            FlinkTasksClientFactory.__clients[key] = client

        return FlinkTasksClientFactory.__clients[key]
