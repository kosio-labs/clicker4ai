/* Transport abstraction — the app talks to exactly this interface:
     send(obj) -> bool          fire-and-forget control message
     rpc(method, params) -> Promise<result>
     openTerm(sid) -> WebSocket (binary out, JSON control in)
     onMessage(cb), onStatus(cb)
     connect(), close()
   LocalTransport wraps /ws + /ws/term (cookie auth, same origin).
   Classic script, no bundler — loaded before app.js. */
"use strict";

const RPC_TIMEOUT_MS = 30000;
// Pings go out every 25 s and each gets a pong: a minute with nothing from
// the server means the socket died without telling us (a computer that
// slept with the app on screen wakes up to one that still reads as open).
const STALE_MS = 60000;

class LocalTransport {
  constructor() {
    this.up = false;
    this.ws = null;
    this._retry = 0;
    this._msgCbs = [];
    this._statusCbs = [];
    this._pending = new Map(); // rpc id -> {resolve, reject, timer}
    this._sendQueue = [];      // frames waiting for the socket to open
    this._nextId = 1;
    this._retryTimer = null;
    this._lastRx = 0;          // when the server last said anything
    this._halted = false;      // closed on purpose (logged out, locked …): no retry
    this._openedAt = 0;        // when the current socket started connecting
    this._pingTimer = null;
  }

  // Started by connect() and stopped by close(): a transport closed for the
  // lock screen gets its pings back once unlocking connects it again.
  _startPing() {
    if (this._pingTimer) return;
    this._pingTimer = setInterval(() => {
      if (!this.ws) return;
      // iOS can close a frozen page's socket without ever firing onclose:
      // nothing would retry until the next send, so the timer does
      if (!document.hidden && !this._halted) {
        if (this.ws.readyState > 1) { this.connect(); return; }
        if (this.ws.readyState === 0 && Date.now() - this._openedAt > 30000) {
          this.reconnect(); return;
        }
      }
      if (this.ws.readyState !== 1) return;
      // a hidden page's timers are throttled, so silence proves nothing there
      if (!document.hidden && this._stale()) { this.reconnect(); return; }
      this.ws.send('{"type":"ping"}');
    }, 25000);
  }

  connect() {
    if (this.ws && (this.ws.readyState === 0 || this.ws.readyState === 1)) return;
    this._startPing();
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws = ws;
    this._halted = false;
    this._openedAt = Date.now();

    ws.onopen = () => {
      this.up = true;
      this._lastRx = Date.now();
      this._retry = 0;
      const q = this._sendQueue;
      this._sendQueue = [];
      for (const frame of q) ws.send(frame);
      this._emitStatus({ up: true });
    };
    ws.onmessage = (e) => {
      this._lastRx = Date.now();
      let msg; try { msg = JSON.parse(e.data); } catch { return; }
      if (msg.type === "rpc_result") { this._settle(msg); return; }
      for (const cb of this._msgCbs) cb(msg);
    };
    ws.onclose = (e) => {
      if (this.ws !== ws) return;
      this.up = false;
      this._failPending(new Error(e.code === 4401 ? "unauthorized"
        : e.code === 4503 ? "server error" : "connection lost"));
      if (e.code === 4401) {
        this._halted = true;
        this._emitStatus({ up: false, authRequired: true });
        return;
      }
      if (e.code === 4423) {          // cookie fine, session locked
        this._halted = true;
        this._emitStatus({ up: false, locked: true });
        return;
      }
      if (e.code === 4428) {          // device must register a passkey first
        this._halted = true;
        this._emitStatus({ up: false, passkeyRequired: true });
        return;
      }
      if (e.code === 4403) {          // Origin refused: retrying cannot help
        this._halted = true;
        this._emitStatus({ up: false, badOrigin: true });
        return;
      }
      // 4503: the server cannot read its device list — the cookie may be
      // fine, so keep retrying like a server that is down
      this._emitStatus({ up: false, serverError: e.code === 4503 });
      clearTimeout(this._retryTimer);
      this._retryTimer = setTimeout(() => this.connect(),
        Math.min(8000, 500 * 2 ** this._retry++));
    };
    ws.onerror = () => ws.close();
  }

  // A page the OS froze (iOS, screen off) can wake up with a socket that
  // still reads as open but is dead — connect() would keep it. Drop it
  // and open a fresh one; a locked session then closes with 4423 at once.
  reconnect() {
    const ws = this.ws;
    this.ws = null;
    this.up = false;
    if (ws) {
      ws.onopen = null; ws.onclose = null; ws.onmessage = null; ws.onerror = null;
      try { ws.close(); } catch {}
    }
    this._failPending(new Error("connection lost"));
    clearTimeout(this._retryTimer);
    this.connect();
  }

  _stale() { return Date.now() - this._lastRx > STALE_MS; }

  send(obj) {
    const frame = JSON.stringify(obj);
    if (this.ws && this.ws.readyState === 1) {
      if (this._stale()) {
        // a fresh socket first; the frame waits for it (dropped, and
        // reported down, if that one does not open)
        this.reconnect();
        this._sendQueue.push(frame);
      } else this.ws.send(frame);
      return true;
    }
    // a socket on its way up (reconnect() on the way back to the app) takes
    // the frame when it opens, as rpc() does; dropped if it never opens
    if (this.ws && this.ws.readyState === 0) {
      this._sendQueue.push(frame);
      return true;
    }
    this.connect();
    return false;
  }

  rpc(method, params) {
    return new Promise((resolve, reject) => {
      const id = this._nextId++;
      const frame = JSON.stringify({ type: "rpc", id, method, params: params || {} });
      const timer = setTimeout(() => {
        this._pending.delete(id);
        // a call reported as failed must not go out once the socket opens
        const i = this._sendQueue.indexOf(frame);
        if (i >= 0) this._sendQueue.splice(i, 1);
        reject(new Error("rpc timeout: " + method));
      }, RPC_TIMEOUT_MS);
      if (this.ws && this.ws.readyState === 1 && this._stale()) this.reconnect();
      this._pending.set(id, { resolve, reject, timer });
      if (this.ws && this.ws.readyState === 1) this.ws.send(frame);
      else { this._sendQueue.push(frame); this.connect(); }
    });
  }

  _settle(msg) {
    const p = this._pending.get(msg.id);
    if (!p) return;
    this._pending.delete(msg.id);
    clearTimeout(p.timer);
    if (msg.ok) { p.resolve(msg.result); return; }
    const err = new Error((msg.error && msg.error.message) || "rpc failed");
    if (msg.error) { err.code = msg.error.code; err.data = msg.error.data; }
    p.reject(err);
  }

  _failPending(err) {
    for (const p of this._pending.values()) { clearTimeout(p.timer); p.reject(err); }
    this._pending.clear();
    this._sendQueue = [];
  }

  openTerm(sid) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    return new WebSocket(`${proto}://${location.host}/ws/term/${encodeURIComponent(sid)}`);
  }

  onMessage(cb) { this._msgCbs.push(cb); }
  onStatus(cb) { this._statusCbs.push(cb); }
  _emitStatus(st) { for (const cb of this._statusCbs) cb(st); }

  close() {
    clearTimeout(this._retryTimer);
    clearInterval(this._pingTimer);
    this._pingTimer = null;
    const ws = this.ws;
    this.ws = null;
    this.up = false;
    if (ws) {
      ws.onclose = null; ws.onmessage = null; ws.onerror = null;
      try { ws.close(); } catch {}
    }
    this._failPending(new Error("transport closed"));
  }
}
