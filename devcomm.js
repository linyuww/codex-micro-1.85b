oaderDevices;
    } catch (error) {
      this.log.error(error);
      return [];
    }
  }
};
var wl_device_discovery_default = WLDeviceDiscovery;

// src/wl_device_comm/wl_device_comm_impl.ts
var import_node_hid = require("node-hid");
var import_serialport2 = require("serialport");

// src/wl_device_comm/types/connection_event_type.ts
var ConnectionEventType = /* @__PURE__ */ ((ConnectionEventType2) => {
  ConnectionEventType2[ConnectionEventType2["CONNECTED"] = 0] = "CONNECTED";
  ConnectionEventType2[ConnectionEventType2["DISCONNECTED"] = 1] = "DISCONNECTED";
  ConnectionEventType2[ConnectionEventType2["ERROR"] = 2] = "ERROR";
  return ConnectionEventType2;
})(ConnectionEventType || {});

// src/wl_device_comm/wl_device_comm_impl.ts
var WLDeviceCommImpl = class {
  /**
   * @param logger - Optional {@link Logger} instance. If omitted, logging is disabled.
   */
  constructor(logger) {
    this.connectionListener = new event_emitter_default();
    this.CHANNEL_DEBUG = 1;
    this.CHANNEL_RPC = 2;
    this.connectedDevice = void 0;
    this.port = void 0;
    this._responseResolvers = /* @__PURE__ */ new Map();
    this._rpcResolver = /* @__PURE__ */ new Map();
    this._notifyResolvers = /* @__PURE__ */ new Map();
    this.rpcResponse = "";
    this.connectionType = void 0;
    this.queue = [];
    this.pending = false;
    this.buffers = {
      [this.CHANNEL_DEBUG]: "",
      [this.CHANNEL_RPC]: ""
    };
    this.log = prefixLogger(logger ?? noopLogger, "wl_device_comm");
  }
  /** @inheritDoc */
  onConnectionEvent(callback) {
    return this.connectionListener.on(callback);
  }
  /** @inheritDoc */
  async connect(device) {
    if (device.connectionType === 0 /* serial */) {
      this.log.info("Connecting with serial");
      return this.connectWithSerial(device.portPath);
    } else {
      this.log.info("Connecting with HID");
      return this.connectWithHID(device.portPath);
    }
  }
  /**
   * Opens a serial port at the given path (115200 baud) and wires up
   * data/error/close handlers.
   */
  async connectWithSerial(portPath) {
    if (this.port) {
      this.log.info("A device is already connected");
      return Promise.reject(new WLDeviceError("ALREADY_CONNECTED" /* AlreadyConnected */, "A device is already connected"));
    }
    let resolveCb = void 0;
    let promise = new Promise((resolve, _reject) => {
      resolveCb = resolve;
    });
    let baudRate = 115200;
    const port = new import_serialport2.SerialPort({ path: portPath, baudRate, autoOpen: false });
    port.on("open", () => {
      this.log.info("Connection opened");
    });
    port.on("close", () => {
      this.queue = [];
      this.connectionType = void 0;
      this.port = void 0;
      this.log.info("Connection closed");
      this.connectionListener.emit({ type: 1 /* DISCONNECTED */ });
    });
    port.on("error", (err2) => {
      this.log.error(err2);
      this.queue = [];
      this.connectionType = void 0;
      this.connectionListener.emit({ type: 2 /* ERROR */, error: err2 });
    });
    port.open((err2) => {
      if (err2) {
        this.queue = [];
        this.connectionType = void 0;
        this.port = void 0;
        this.log.error(err2);
        this.connectionListener.emit({ type: 2 /* ERROR */, error: err2 });
        resolveCb(false);
      } else {
        this.connectionListener.emit({ type: 0 /* CONNECTED */ });
        resolveCb(true);
      }
    });
    port.on("data", this.parseSerialRpcData.bind(this));
    const parser = port.pipe(new import_serialport2.DelimiterParser({ delimiter: "\n" }));
    parser.on("data", this.parseSerialData.bind(this));
    this.connectionType = 0 /* serial */;
    this.port = port;
    return promise;
  }
  /**
   * Opens a HID device at the given path and wires up data/error/close
   * handlers. On macOS the device is opened in non-exclusive mode.
   */
  async connectWithHID(portPath) {
    if (this.connectedDevice) {
      this.log.info("A device is already connected");
      return Promise.reject(new WLDeviceError("ALREADY_CONNECTED" /* AlreadyConnected */, "A device is already connected"));
    }
    try {
      let connectingDevice = process.platform === "darwin" ? await import_node_hid.HIDAsync.open(portPath, { nonExclusive: true }) : await import_node_hid.HIDAsync.open(portPath);
      connectingDevice.on("close", () => {
        this.cleanOnDisconnect();
        this.log.info("Connection closed");
        this.connectionListener.emit({ type: 1 /* DISCONNECTED */ });
      });
      connectingDevice.on("error", (err2) => {
        this.log.error(err2);
        this.cleanOnDisconnect();
        this.connectionListener.emit({ type: 2 /* ERROR */, error: err2 });
      });
      connectingDevice.on("data", this.parseHIDdata.bind(this));
      this.connectionType = 1 /* hid */;
      this.connectedDevice = connectingDevice;
      this.connectionListener.emit({ type: 0 /* CONNECTED */ });
      return true;
    } catch (error) {
      this.cleanOnDisconnect();
      this.log.error(error);
      this.connectionListener.emit({ type: 2 /* ERROR */, error });
      throw error;
    }
  }
  /**
   * Creates a {@link ResolverEntry} that automatically clears `timer` and
   * deletes the entry from `map` when either callback fires. Centralises the cleanup
   * logic shared by every in-flight request.
   */
  makeResolver(map, key, resolve, reject, timer) {
    const cleanup = () => {
      clearTimeout(timer);
      map.delete(key);
    };
    return {
      resolve: (value) => {
        cleanup();
        resolve(value);
      },
      reject: (err2) => {
        cleanup();
        reject(err2);
      }
    };
  }
  /**
   * Rejects all queued and in-flight RPC and legacy request promises with a
   * {@link WLDeviceErrorCode.DeviceDisconnected} error so callers don't hang
   * indefinitely after the device goes away.
   */
  rejectPendingRequests() {
    const disconnectError = new WLDeviceError("DEVICE_DISCONNECTED" /* DeviceDisconnected */, "Device disconnected");
    for (const task of this.queue) {
      try {
        task.reject(disconnectError);
      } catch (error) {
        this.log.error("Error while rejecting queued task", error);
      }
    }
    for (const value of this._rpcResolver.values()) {
      try {
        value.reject(disconnectError);
      } catch (error) {
        this.log.error("Error while rejecting resolver", error);
      }
    }
    for (const value of this._responseResolvers.values()) {
      try {
        value.reject(disconnectError);
      } catch (error) {
        this.log.error("Error while rejecting resolver", error);
      }
    }
  }
  /**
   * Resets all transport state after a disconnection (intentional or unexpected).
   * Clears the send queue, pending resolvers, accumulated RPC buffer, and both
   * device handles so the instance is safe to reuse with a new {@link connect} call.
   */
  cleanOnDisconnect() {
    this.rejectPendingRequests();
    this.queue = [];
    this.connectionType = void 0;
    this.connectedDevice = void 0;
    this.port = void 0;
    this._responseResolvers.clear();
    this._rpcResolver.clear();
    this.rpcResponse = "";
    this.pending = false;
  }
  /** @inheritDoc */
  cleanCommQueue() {
    this.rejectPendingRequests();
    this._responseResolvers.clear();
    this._rpcResolver.clear();
    this.rpcResponse = "";
    this.queue = [];
    this.pending = false;
  }
  /** @inheritDoc */
  async disconnect() {
    if (this.connectionType === 1 /* hid */) {
      this.log.info("Disconnecting HID device");
      try {
        await this.connectedDevice?.close();
        this.connectedDevice?.removeAllListeners();
      } catch (error) {
        this.log.error("Failed to disconnect", error);
      }
    } else {
      this.log.info("Disconnecting serial device");
      this.port?.close();
    }
    this.cleanOnDisconnect();
  }
  /** @inheritDoc */
  isConnected() {
    return this.connectionType === 1 /* hid */ && !!this.connectedDevice || this.connectionType === 0 /* serial */ && !!this.port;
  }
  /** @inheritDoc */
  async sendLegacyRpcRequest(rpc, args = null) {
    return this.enqueue(() => this._sendLegacyRpcRequest(rpc, args));
  }
  _sendLegacyRpcRequest(rpc, args = null) {
    const message = !args ? `#${rpc}#\r
` : `#${rpc}#${args}#\r
`;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._responseResolvers.get(rpc)?.reject(new WLDeviceError("TIMEOUT" /* Timeout */, "Request timed out"));
      }, 1e4);
      this._responseResolvers.set(rpc, this.makeResolver(this._responseResolvers, rpc, resolve, reject, timer));
      this.sendData(message).catch((err2) => {
        this._responseResolvers.get(rpc)?.reject(err2);
      });
    });
  }
  /** @inheritDoc */
  sendJsonRpcRequest(request, id) {
    return this.enqueue(() => this._sendJsonRpcRequest(request, id), id);
  }
  _sendJsonRpcRequest(request, id) {
    this.log.debug("Started sending RPC call, id:", id);
    this.rpcResponse = "";
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._rpcResolver.get(id)?.reject(new WLDeviceError("TIMEOUT" /* Timeout */, "Request timed out"));
      }, 1e4);
      this._rpcResolver.set(id, this.makeResolver(this._rpcResolver, id, resolve, reject, timer));
      this.sendData(request).catch((err2) => {
        this._rpcResolver.get(id)?.reject(err2);
      });
    });
  }
  /**
   * Dispatches raw data to the device over the active transport.
   * @param message - The string payload to send.
   * @throws {WLDeviceError} with code `DeviceDisconnected` if no device is connected,
   *         or `WriteFailed` if the transport write failed.
   */
  async sendData(message) {
    if (!this.isConnected()) {
      throw new WLDeviceError("DEVICE_DISCONNECTED" /* DeviceDisconnected */, "cannot send, no device connected");
    }
    if (this.connectionType === 0 /* serial */) {
      return this.sendDataSerial(message);
    }
    return this.sendDataHID(message);
  }
  /**
   * Writes a string directly to the serial port and drains the buffer.
   * @throws {WLDeviceError} with code `WriteFailed` if the write or drain fails.
   */
  sendDataSerial(data) {
    return new Promise((resolve, reject) => {
      const port = this.port;
      port.write(data, (writeError) => {
        if (writeError) {
          this.log.error("Error sending serial data:", writeError);
          reject(new WLDeviceError("WRITE_FAILED" /* WriteFailed */, writeError.message));
          return;
        }
        port.drain((drainError) => {
          if (drainError) {
            this.log.error("Error draining serial data:", drainError);
            reject(new WLDeviceError("WRITE_FAILED" /* WriteFailed */, drainError.message));
            return;
          }
          resolve();
        });
      });
    });
  }
  /**
   * Frames and sends a string payload over HID. Messages longer than 61 bytes
   * are automatically split into multiple 64-byte HID reports.
   *
   * @throws {WLDeviceError} with code `HidUnavailable` for node-hid's native
   *         unavailable-device error, or `WriteFailed` for other write failures.
   */
  async sendDataHID(message) {
    const MAX_CHUNK_SIZE = 61;
    const startTime = Date.now();
    try {
      const connectedDevice = this.connectedDevice;
      const messageBuffer = Buffer.from(message);
      const messageByteLength = messageBuffer.length;
      let offset = 0;
      let packetNum = 1;
      while (offset < messageByteLength) {
        const chunkSize = Math.min(MAX_CHUNK_SIZE, messageByteLength - offset);
        this.log.debug("Sending packet:", packetNum, "size:", chunkSize, "offset:", offset);
        const data = Buffer.alloc(64);
        data[0] = 6;
        data[1] = this.CHANNEL_RPC;
        data[2] = chunkSize;
        messageBuffer.copy(data, 3, offset, offset + chunkSize);
        await connectedDevice.write(data);
        offset += chunkSize;
        packetNum++;
      }
      const elapsedTime = (Date.now() - startTime) / 1e3;
      this.log.debug("Send complete, packets:", packetNum - 1, "bytes:", messageByteLength, "seconds:", elapsedTime.toFixed(3));
    } catch (error) {
      this.log.error("Error sending message:", error);
      const message2 = error instanceof Error ? error.message : String(error);
      const code = message2.includes("0xE00002C5") ? "HID_UNAVAILABLE" /* HidUnavailable */ : "WRITE_FAILED" /* WriteFailed */;
      throw new WLDeviceError(code, message2);
    }
  }
  /**
   * Processes an incoming HID report, demultiplexes by channel (debug vs RPC),
   * and forwards complete newline-terminated lines to the appropriate parser.
   */
  parseHIDdata(data) {
    try {
      const packet = this.parseHIDReport(data);
      const channel = packet.channel;
      const textData = packet.payload;
      if (this.buffers[channel] === void 0) {
        this.buffers[channel] = "";
      }
      this.buffers[channel] += textData;
      const lines = this.buffers[channel].split(/\r?\n/);
      if (lines.length > 1 || textData.endsWith("\n") || textData.endsWith("\r")) {
        for (let i = 0; i < lines.length - 1; i++) {
          const line = lines[i].trim();
          if (line) {
            const prefix = channel === this.CHANNEL_DEBUG ? "[LOG]" : "[RPC]";
            if (channel === this.CHANNEL_RPC) {
              this.log.debug(prefix, line);
              try {
                this.parseRpcData(line);
              } catch (error) {
                this.log.error("Failed to parse RPC data:", error.message);
              }
            } else {
              this.log.info(prefix, line);
            }
          }
        }
        this.buffers[channel] = lines[lines.length - 1];
      }
    } catch (error) {
      this.log.error("Failed to parse packet:", error.message);
      this.buffers[this.CHANNEL_DEBUG] = "";
      this.buffers[this.CHANNEL_RPC] = "";
    }
  }
  /**
   * Extracts channel, length, and UTF-8 payload from a raw 64-byte HID report.
   */
  parseHIDReport(data) {
    const channel = data[1];
    const length = data[2];
    const payload = data.slice(3, 3 + length);
    return {
      channel,
      length,
      payload: Buffer.from(payload).toString("utf8")
    };
  }
  /** Decodes a serial data buffer and routes it to the legacy response parser. */
  parseSerialData(data) {
    const decoded = new TextDecoder().decode(data);
    this.parseData(decoded);
  }
  /**
   * Parses legacy `#rpc#response#` formatted data received over serial and
   * resolves the matching pending request promise.
   */
  parseData(data) {
    let str = data.replace(/[\r\n]+$/, "");
    try {
      if (this.checkMessage(str, "version")) {
        let version = this.extractResponse(str);
        this._responseResolvers.get("version")?.resolve(version);
      } else if (this.checkMessage(str, "dfu")) {
        let response = this.extractResponse(str);
        this._responseResolvers.get("dfu")?.resolve(response === "ok");
      } else if (this.checkMessage(str, "selftest")) {
        let response = this.extractResponse(str);
        this._responseResolvers.get("selftest")?.resolve(response === "ok");
      } else if (this.checkMessage(str, "bootloader")) {
        let response = this.extractResponse(str);
        this._responseResolvers.get("bootloader")?.resolve(response === "ok");
      }
    } catch (error) {
      this.log.error(error);
    }
  }
  /** Decodes a serial data buffer and routes it to the JSON-RPC response parser. */
  parseSerialRpcData(data) {
    const string = new TextDecoder().decode(data);
    this.parseRpcData(string);
  }
  /**
   * Accumulates incoming JSON fragments and, once a complete JSON object is
   * received, either resolves a pending RPC promise or dispatches a device
   * notification via {@link handleNotifyMessage}.
   *
   * @returns `true` if the data was consumed or ignored; `false` if more data is needed.
   */
  parseRpcData(data) {
    if (this.rpcResponse.length === 0) {
      const jsonStart = data.indexOf("{");
      if (jsonStart === -1) {
        return true;
      }
      this.rpcResponse = data.slice(jsonStart);
    } else {
      this.rpcResponse += data;
    }
    try {
      const parsedData = JSON.parse(this.rpcResponse);
      let id = parsedData.id ?? parsedData.i;
      if (id !== void 0 && typeof id === "number") {
        id = String(id);
      }
      const method = parsedData.method ?? parsedData.m;
      if (!id && !method) {
        this.log.warn("Received RPC call without id and method");
        this.rpcResponse = "";
        return false;
      }
      if (!id && method) {
        this.handleNotifyMessage(parsedData);
        this.rpcResponse = "";
      } else {
        const resolverEntry = this._rpcResolver.get(id);
        if (resolverEntry) {
          resolverEntry.resolve(this.rpcResponse);
          this.rpcResponse = "";
        } else {
          this.log.warn("No