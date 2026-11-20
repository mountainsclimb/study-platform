"""P1 真跨进程流式 SSE：Redis Pub/Sub 双写、回退轮询、keepalive、订阅清理与配额环境变量。

不依赖真实 Redis：发布端用 sys.modules 假 `redis` 模块，订阅端用 fake aioredis/pubsub。
"""
import asyncio
import json
import sys
import time
import types

import pytest

from app import db
from app.ai import jobs


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _user(client):
    return client.get("/api/auth/me").json()


def _insert_job(user_id, *, status="running", partial="", task="explain", token="stream-token"):
    with db.connection() as con:
        now = db.utcnow()
        return con.execute(
            "INSERT INTO ai_jobs(user_id,task,status,stage,request_json,partial_content,cancelled,"
            "created_at,updated_at,dispatch_token) VALUES (?,?,?,?,?,?,0,?,?,?)",
            (
                user_id, task, status, status,
                json.dumps({"task": task, "provider_id": 1, "model": "m"}),
                partial, now, now, token,
            ),
        ).lastrowid


def _set_job(job_id, **fields):
    with db.connection() as con:
        assignments = ",".join(f"{key}=?" for key in fields)
        con.execute(
            f"UPDATE ai_jobs SET {assignments},updated_at=? WHERE id=?",
            (*fields.values(), db.utcnow(), job_id),
        )


async def _collect(response):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def _parse_sse(text):
    """返回 [(event, data), ...]；忽略 keepalive 注释行。"""
    events = []
    event = None
    for line in text.splitlines():
        if line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: "):
            events.append((event, json.loads(line[len("data: "):])))
    return events


def _payload(raw):
    return json.loads(raw)


# ---------------------------------------------------------------------------
# 假 Redis 发布端（同步 redis.Redis）
# ---------------------------------------------------------------------------

class _FakeSyncRedis:
    instances = []
    fail = False
    last_url = None

    def __init__(self):
        self.published = []
        self.closed = False
        type(self).instances.append(self)

    @classmethod
    def from_url(cls, url, **kwargs):
        cls.last_url = (url, kwargs)
        return cls()

    def publish(self, channel, payload):
        if type(self).fail:
            raise ConnectionError("redis down")
        self.published.append((channel, payload))
        return 1

    def close(self):
        self.closed = True


def _fake_redis_module(monkeypatch):
    _FakeSyncRedis.instances = []
    _FakeSyncRedis.fail = False
    module = types.ModuleType("redis")
    module.Redis = _FakeSyncRedis
    monkeypatch.setitem(sys.modules, "redis", module)
    # 发布客户端/熔断状态按测试隔离
    monkeypatch.setattr(jobs, "_sync_redis_client", None)
    monkeypatch.setattr(jobs, "_sync_redis_disabled_until", 0.0)
    return _FakeSyncRedis


# ---------------------------------------------------------------------------
# 假 Redis 订阅端（redis.asyncio 模块形象）
# ---------------------------------------------------------------------------

class FakePubSub:
    """pending 用于订阅后排队等待 drain 的消息；script 是随后的逐个动作。"""

    def __init__(self, script=None, pending=None, *, subscribe_error=None):
        self.script = list(script or [])
        self.pending = list(pending or [])
        self.subscribe_error = subscribe_error
        self.subscribed = []
        self.unsubscribed = []
        self.closed = False

    async def subscribe(self, channel):
        if self.subscribe_error:
            raise self.subscribe_error
        self.subscribed.append(channel)

    async def get_message(self, ignore_subscribe_messages=True, timeout=0):
        await asyncio.sleep(0)
        if timeout and timeout > 0:
            if self.script:
                item = self.script.pop(0)
            elif self.pending:
                item = self.pending.pop(0)
            else:
                return None
            if item == "idle":
                return None
            if isinstance(item, Exception):
                raise item
            return {"type": "message", "data": json.dumps(item, ensure_ascii=False)}
        return {"type": "message", "data": json.dumps(self.pending.pop(0), ensure_ascii=False)} if self.pending else None

    async def unsubscribe(self, channel):
        self.unsubscribed.append(channel)

    async def aclose(self):
        self.closed = True


class FakeAioredisModule:
    def __init__(self, pubsub, *, from_url_error=None):
        self.pubsub = pubsub
        self.from_url_error = from_url_error
        self.urls = []
        self.clients = []
        outer = self

        class _Client:
            def __init__(self):
                self.closed = False
                self.pubsub_calls = 0

            def pubsub(self):
                self.pubsub_calls += 1
                return outer.pubsub

            async def aclose(self):
                self.closed = True

        class _Redis:
            @classmethod
            def from_url(cls, url, **kwargs):
                if outer.from_url_error:
                    raise outer.from_url_error
                outer.urls.append((url, kwargs))
                client = _Client()
                outer.clients.append(client)
                return client

        self.Redis = _Redis


def _msg(event, data):
    return {"event": event, "data": data, "ts": time.time()}


@pytest.fixture(autouse=True)
def _isolated_stream_state():
    jobs._stream_buffers.clear()
    jobs._stream_queues.clear()
    yield
    jobs._stream_buffers.clear()
    jobs._stream_queues.clear()


# ---------------------------------------------------------------------------
# 1. publish_event：本地队列 + Redis Pub/Sub 双写
# ---------------------------------------------------------------------------

def test_publish_event_dual_writes_local_queue_and_redis(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://pubsub-test:6379/0")
    fake = _fake_redis_module(monkeypatch)
    job_id = _insert_job(_user(client)["id"])

    async def scenario():
        queue = asyncio.Queue(maxsize=256)
        jobs._stream_queues[job_id] = [queue]
        jobs.publish_event(job_id, "delta", {"text": "你好"})
        jobs.publish_event(job_id, "done", {"status": "completed"})
        return queue.get_nowait(), queue.get_nowait()

    first, second = asyncio.run(scenario())
    assert first == {"event": "delta", "data": {"text": "你好"}}
    assert second == {"event": "done", "data": {"status": "completed"}}
    # 本地增量缓冲也照旧累积（断线重连兜底）
    assert "".join(jobs._stream_buffers.get(job_id, [])) == "你好"

    instance = fake.instances[-1]
    assert fake.last_url[0] == "redis://pubsub-test:6379/0"
    assert len(instance.published) == 2
    channel, raw = instance.published[0]
    assert channel == f"zhixu:job:{job_id}"
    payload = _payload(raw)
    assert payload["event"] == "delta"
    assert payload["data"] == {"text": "你好"}
    assert isinstance(payload["ts"], float)
    assert _payload(instance.published[1][1])["event"] == "done"


def test_publish_event_never_raises_when_redis_down(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://unreachable:6379/0")
    fake = _fake_redis_module(monkeypatch)
    fake.fail = True
    job_id = _insert_job(_user(client)["id"])

    async def scenario():
        queue = asyncio.Queue(maxsize=256)
        jobs._stream_queues[job_id] = [queue]
        jobs.publish_event(job_id, "stage", {"status": "running", "stage": "ocr-1/3"})
        jobs.publish_event(job_id, "delta", {"text": "x"})
        return [queue.get_nowait()["event"] for _ in range(2)]

    assert asyncio.run(scenario()) == ["stage", "delta"]
    # 熔断期内不再重复建连（发布失败只告警，不影响任务）
    attempts = len(fake.instances)
    jobs.publish_event(job_id, "stage", {"status": "running", "stage": "ocr-2/3"})
    assert len(fake.instances) == attempts


def test_publish_event_without_redis_only_writes_local_queue(client, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    fake = _fake_redis_module(monkeypatch)
    job_id = _insert_job(_user(client)["id"])

    async def scenario():
        queue = asyncio.Queue(maxsize=256)
        jobs._stream_queues[job_id] = [queue]
        jobs.publish_event(job_id, "delta", {"text": "本地"})
        return queue.get_nowait()

    assert asyncio.run(scenario())["data"] == {"text": "本地"}
    assert fake.instances == []


def test_control_events_survive_queue_full_while_deltas_may_drop(client, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    job_id = _insert_job(_user(client)["id"])

    async def scenario():
        queue = asyncio.Queue(maxsize=1)
        jobs._stream_queues[job_id] = [queue]
        queue.put_nowait({"event": "delta", "data": {"text": "old"}})
        jobs.publish_event(job_id, "delta", {"text": "dropped"})
        jobs.publish_event(job_id, "done", {"status": "completed"})
        events = []
        while not queue.empty():
            events.append(queue.get_nowait()["event"])
        return events

    # done/stage 控制事件永不被 QueueFull 丢弃；普通 delta 可丢弃。
    assert "done" in asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 2. stream_job：Redis Pub/Sub 增量流
# ---------------------------------------------------------------------------

def test_stream_job_streams_pubsub_increments_and_cleans_subscription(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://pubsub-test:6379/0")
    user = _user(client)
    job_id = _insert_job(user["id"], status="running")
    pubsub = FakePubSub(script=[
        _msg("delta", {"text": "第一段"}),
        _msg("thinking", {"text": "推理中", "chars": 3}),
        _msg("stage", {"status": "running", "stage": "verify"}),
        _msg("done", {"status": "completed"}),
    ])
    fake = FakeAioredisModule(pubsub)
    monkeypatch.setattr(jobs, "_load_aioredis", lambda: fake)

    async def scenario():
        response = await jobs.stream_job(job_id, user)
        return await _collect(response)

    text = asyncio.run(scenario())
    events = _parse_sse(text)
    # 连接建立后先发 snapshot，再按增量下发，delta 不再携带全量 replace
    assert events[0][0] == "snapshot"
    assert sum(1 for name, _ in events if name == "snapshot") >= 2
    assert ("delta", {"text": "第一段"}) in events
    assert ("thinking", {"text": "推理中", "chars": 3}) in events
    assert any(name == "stage" and data["stage"] == "verify" for name, data in events)
    assert events[-1] == ("done", {"status": "completed"})
    assert not any(name == "delta" and data.get("replace") for name, data in events)
    # 取消/结束时退订并关闭连接
    channel = f"zhixu:job:{job_id}"
    assert pubsub.subscribed == [channel]
    assert pubsub.unsubscribed == [channel]
    assert pubsub.closed is True
    assert fake.clients and fake.clients[0].closed is True


def test_stream_job_terminal_job_does_not_subscribe(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://pubsub-test:6379/0")
    user = _user(client)
    job_id = _insert_job(user["id"], status="completed")
    pubsub = FakePubSub()
    fake = FakeAioredisModule(pubsub)
    monkeypatch.setattr(jobs, "_load_aioredis", lambda: fake)

    async def scenario():
        response = await jobs.stream_job(job_id, user)
        return await _collect(response)

    events = _parse_sse(asyncio.run(scenario()))
    assert events[-1] == ("done", {"status": "completed"})
    assert fake.urls == []  # 已完成任务无需订阅 Redis


def test_stream_job_redis_keepalive_and_db_terminal_done(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://pubsub-test:6379/0")
    monkeypatch.setattr(jobs, "STREAM_KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(jobs, "STREAM_PUBSUB_POLL_SECONDS", 0.01)
    user = _user(client)
    job_id = _insert_job(user["id"], status="running")
    pubsub = FakePubSub(script=[])  # 永远没有 Pub/Sub 事件
    monkeypatch.setattr(jobs, "_load_aioredis", lambda: FakeAioredisModule(pubsub))

    async def scenario():
        response = await jobs.stream_job(job_id, user)
        consumer = asyncio.create_task(_collect(response))

        async def producer():
            await asyncio.sleep(0.10)
            # Worker 进程失联/尚未发 done：keepalive 时以数据库终态兜底结束
            _set_job(job_id, status="completed", stage="completed")

        await producer()
        return await consumer

    text = asyncio.run(scenario())
    assert ": keepalive" in text
    events = _parse_sse(text)
    assert events[-1] == ("done", {"status": "completed"})
    assert pubsub.unsubscribed == [f"zhixu:job:{job_id}"]
    assert pubsub.closed is True


def test_stream_job_partial_content_catchup_is_replace_delta_no_duplicate(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://pubsub-test:6379/0")
    user = _user(client)
    job_id = _insert_job(user["id"], status="running", partial="已生成的部分")
    published_delta = _msg("delta", {"text": "已生成的部分"})
    pubsub = FakePubSub(
        pending=[published_delta, _msg("stage", {"status": "running", "stage": "verify"})],
        script=[_msg("done", {"status": "completed"})],
    )
    monkeypatch.setattr(jobs, "_load_aioredis", lambda: FakeAioredisModule(pubsub))

    async def scenario():
        response = await jobs.stream_job(job_id, user)
        return await _collect(response)

    events = _parse_sse(asyncio.run(scenario()))
    deltas = [data for name, data in events if name == "delta"]
    assert deltas and all(data.get("replace") is True for data in deltas)
    assert {data["text"] for data in deltas} == {"已生成的部分"}
    assert any(name == "stage" and data["stage"] == "verify" for name, data in events)
    assert events[-1] == ("done", {"status": "completed"})


def test_stream_job_appends_uncovered_buffered_deltas_after_catchup(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://pubsub-test:6379/0")
    user = _user(client)
    job_id = _insert_job(user["id"], status="running", partial="AB")
    pubsub = FakePubSub(
        pending=[_msg("delta", {"text": "CD"})],
        script=[_msg("done", {"status": "completed"})],
    )
    monkeypatch.setattr(jobs, "_load_aioredis", lambda: FakeAioredisModule(pubsub))

    async def scenario():
        response = await jobs.stream_job(job_id, user)
        return await _collect(response)

    events = _parse_sse(asyncio.run(scenario()))
    deltas = [data for name, data in events if name == "delta"]
    assert {"text": "AB", "replace": True} in deltas
    # partial_content 未覆盖的缓冲增量按增量追加，不重复整段
    assert {"text": "CD"} in deltas


def test_stream_job_redis_disconnect_releases_subscription(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://pubsub-test:6379/0")
    user = _user(client)
    job_id = _insert_job(user["id"], status="running")
    pubsub = FakePubSub(script=[_msg("delta", {"text": "半"}), ConnectionError("pubsub lost")])
    monkeypatch.setattr(jobs, "_load_aioredis", lambda: FakeAioredisModule(pubsub))

    async def scenario():
        response = await jobs.stream_job(job_id, user)
        return await _collect(response)

    text = asyncio.run(scenario())
    assert "event: delta" in text
    assert pubsub.unsubscribed == [f"zhixu:job:{job_id}"]
    assert pubsub.closed is True


# ---------------------------------------------------------------------------
# 3. Redis 不可用 → 回退 0.75s 数据库轮询（本地模式/断线兜底）
# ---------------------------------------------------------------------------

def _polling_scenario(job_id, user, partial_text):
    async def scenario():
        response = await jobs.stream_job(job_id, user)
        consumer = asyncio.create_task(_collect(response))
        await asyncio.sleep(0.03)
        _set_job(job_id, partial_content=partial_text)
        await asyncio.sleep(0.05)
        _set_job(job_id, status="completed", stage="completed")
        return await consumer

    return asyncio.run(scenario())


def test_stream_job_falls_back_to_polling_when_redis_package_missing(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://pubsub-test:6379/0")
    monkeypatch.setitem(sys.modules, "redis.asyncio", None)  # 模拟未安装 redis 依赖
    monkeypatch.setattr(jobs, "STREAM_POLL_SECONDS", 0.01)
    user = _user(client)
    job_id = _insert_job(user["id"], status="running")

    text = _polling_scenario(job_id, user, "轮询增量")
    events = _parse_sse(text)
    assert events[0][0] == "snapshot"
    assert any(name == "delta" and data.get("replace") for name, data in events)
    assert any(name == "delta" and data["text"] == "轮询增量" for name, data in events)
    assert events[-1] == ("done", {"status": "completed"})


def test_stream_job_falls_back_to_polling_when_subscribe_fails(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://unreachable:6379/0")
    monkeypatch.setattr(jobs, "STREAM_POLL_SECONDS", 0.01)
    pubsub = FakePubSub()
    fake = FakeAioredisModule(pubsub, from_url_error=ConnectionError("cannot connect"))
    monkeypatch.setattr(jobs, "_load_aioredis", lambda: fake)
    user = _user(client)
    job_id = _insert_job(user["id"], status="running")

    text = _polling_scenario(job_id, user, "降级增量")
    events = _parse_sse(text)
    assert any(name == "delta" and data["text"] == "降级增量" for name, data in events)
    assert events[-1] == ("done", {"status": "completed"})
    assert pubsub.subscribed == []


# ---------------------------------------------------------------------------
# 4. 本地内存队列模式：done 结束 + 空队列清理 + keepalive
# ---------------------------------------------------------------------------

def test_local_stream_done_ends_and_cleans_up_empty_queue(client, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    user = _user(client)
    job_id = _insert_job(user["id"], status="running")

    async def scenario():
        response = await jobs.stream_job(job_id, user)
        consumer = asyncio.create_task(_collect(response))
        await asyncio.sleep(0.02)
        assert jobs._stream_queues.get(job_id)  # 消费者已注册
        jobs.publish_event(job_id, "delta", {"text": "增量"})
        jobs.publish_event(job_id, "done", {"status": "completed"})
        text = await consumer
        return text

    text = asyncio.run(scenario())
    events = _parse_sse(text)
    assert ("delta", {"text": "增量"}) in events
    assert events[-1] == ("done", {"status": "completed"})
    # 消费者断开后删除空列表，避免内存泄漏
    assert job_id not in jobs._stream_queues


def test_local_stream_keepalive_then_db_terminal_done(client, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(jobs, "STREAM_KEEPALIVE_SECONDS", 0.05)
    user = _user(client)
    job_id = _insert_job(user["id"], status="running")

    async def scenario():
        response = await jobs.stream_job(job_id, user)
        consumer = asyncio.create_task(_collect(response))
        await asyncio.sleep(0.14)
        _set_job(job_id, status="completed", stage="completed")
        return await consumer

    text = asyncio.run(scenario())
    assert ": keepalive" in text
    assert _parse_sse(text)[-1] == ("done", {"status": "completed"})
    assert job_id not in jobs._stream_queues


# ---------------------------------------------------------------------------
# 5. 配额环境变量与 arq 连接池复用
# ---------------------------------------------------------------------------

def test_quota_environment_variables(monkeypatch):
    monkeypatch.setenv("STUDY_AI_MAX_ACTIVE_PER_USER", "3")
    monkeypatch.setenv("STUDY_AI_GLOBAL_CONCURRENCY", "5")
    monkeypatch.setattr(jobs, "_global_sem", None)
    monkeypatch.setattr(jobs, "_global_sem_limit", 0)
    assert jobs.max_active_per_user() == 3
    assert jobs.global_concurrency() == 5
    assert jobs._global_semaphore()._value == 5
    # 运行时改环境变量 → 信号量按新上限重建
    monkeypatch.setenv("STUDY_AI_GLOBAL_CONCURRENCY", "2")
    assert jobs._global_semaphore()._value == 2
    # 非法值回退默认值，不抛错
    monkeypatch.setenv("STUDY_AI_MAX_ACTIVE_PER_USER", "not-a-number")
    assert jobs.max_active_per_user() == jobs.DEFAULT_MAX_ACTIVE_PER_USER
    monkeypatch.delenv("STUDY_AI_MAX_ACTIVE_PER_USER")
    monkeypatch.delenv("STUDY_AI_GLOBAL_CONCURRENCY")
    assert jobs.max_active_per_user() == 2
    assert jobs.global_concurrency() == 8


def test_enqueue_jobs_reuses_single_arq_pool(monkeypatch):
    created = []
    enqueued = []
    closed = []

    class _FakePool:
        async def enqueue_job(self, name, job_id, token, **kwargs):
            enqueued.append((name, job_id, token, kwargs))

        async def aclose(self):
            closed.append(True)

    async def fake_create_pool(settings):
        created.append(settings)
        return _FakePool()

    fake_arq = types.ModuleType("arq")
    fake_arq.create_pool = fake_create_pool
    fake_connections = types.ModuleType("arq.connections")

    class _RedisSettings:
        @classmethod
        def from_dsn(cls, dsn):
            instance = cls()
            instance.dsn = dsn
            return instance

        conn_timeout = None
        conn_retries = None

    fake_connections.RedisSettings = _RedisSettings
    monkeypatch.setitem(sys.modules, "arq", fake_arq)
    monkeypatch.setitem(sys.modules, "arq.connections", fake_connections)
    monkeypatch.setenv("REDIS_URL", "redis://queue-test:6379/0")
    monkeypatch.setattr(jobs, "_arq_pool", None)
    monkeypatch.setattr(jobs, "_arq_pool_loop", None)

    async def scenario():
        await jobs._enqueue_jobs([(1, "token-a")])
        await jobs._enqueue_jobs([(2, "token-b")])
        await jobs.close_arq_pool()

    asyncio.run(scenario())
    assert len(created) == 1  # 同一事件循环内复用同一个连接池
    assert [item[1] for item in enqueued] == [1, 2]
    assert enqueued[0][3]["_queue_name"] == jobs.QUEUE_NAME
    assert closed == [True]
