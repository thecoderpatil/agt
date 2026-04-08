# F23 — Survey Addendum: errorEvent Threading Model

**Date:** 2026-04-08
**Gating question:** Does `errorEvent` fire on the asyncio main loop or a background thread?

---

## Answer: asyncio main loop. `asyncio.create_task` is safe.

## Source chain (ib_async 2.1.0, all verified from installed package source)

### Step 1: Socket reader — `asyncio.Protocol` on the event loop

`ib_async/connection.py` — `Connection` is a subclass of `asyncio.Protocol`:

```python
class Connection(asyncio.Protocol):
    async def connectAsync(self, host, port):
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_connection(lambda: self, host, port)

    def data_received(self, data):
        self.hasData.emit(data)
```

`data_received` is an `asyncio.Protocol` callback — called by the asyncio event loop when socket data arrives. **No threading involved.**

### Step 2: hasData → _onSocketHasData

`ib_async/client.py` — `Client.__init__`:

```python
self.conn.hasData += self._onSocketHasData
```

`_onSocketHasData` parses incoming IBKR wire messages and dispatches to `Wrapper` methods via the decoder. Runs on the event loop (called from `data_received`).

### Step 3: Wrapper.error → errorEvent.emit

`ib_async/wrapper.py` — `Wrapper.error()` (final line):

```python
self.ib.errorEvent.emit(reqId, errorCode, errorString, contract)
```

This is the last line of `Wrapper.error()`, which is called from the decoder in `_onSocketHasData`. The entire chain runs on the asyncio event loop.

### Step 4: IB._onError — internal handler

`ib_async/ib.py` — `IB._onError` is registered on `errorEvent` and runs before our handler:

```python
def _onError(self, reqId, errorCode, errorString, contract):
    if errorCode == 1102:
        asyncio.ensure_future(self.reqAccountSummaryAsync())
```

Note: ib_async's own 1102 handler uses `asyncio.ensure_future()` — confirming the authors know this runs on the event loop.

## Conclusion

The full chain is:

```
asyncio event loop
  → Protocol.data_received()
    → Connection.hasData.emit()
      → Client._onSocketHasData()
        → decoder → Wrapper.error()
          → IB._onError()           [internal, runs first]
          → _on_ib_error()          [our handler, runs second]
```

**All on the asyncio main loop. `asyncio.create_task()` is the correct dispatch pattern for our handler.** No `run_coroutine_threadsafe` needed.

---

F23 addendum done | threading model confirmed: asyncio main loop | `asyncio.create_task` safe | STOP
