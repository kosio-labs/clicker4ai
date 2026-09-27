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
    this._pingTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === 1) this.ws.send('{"type":"ping"}');
    }, 25000);
  }

  connect() {
    if (this.ws && (this.ws.readyState === 0 || this.ws.readyState === 1)) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    this.ws = ws;

    ws.onopen = () => {
      this.up = true;
      this._retry = 0;
      const q = this._sendQueue;
      this._sendQueue = [];
      for (const frame of q) ws.send(frame);
      this._emitStatus({ up: true });
    };
    ws.onmessage = (e) => {
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
        this._emitStatus({ up: false, authRequired: true });
        return;
      }
      if (e.code === 4423) {          // cookie fine, session locked
        this._emitStatus({ up: false, locked: true });
        return;
      }
      if (e.code === 4428) {          // device must register a passkey first
        this._emitStatus({ up: false, passkeyRequired: true });
        return;
      }
      if (e.code === 4403) {          // Origin refused: retrying cannot help
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

  send(obj) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify(obj));
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
