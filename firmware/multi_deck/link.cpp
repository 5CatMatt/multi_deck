#include "link.h"

#include "config.h"

void Link::poll() {
  uint8_t buf[256];
  int n;

  while ((n = rawRead(buf, sizeof(buf))) > 0) {
    for (int i = 0; i < n; i++) {
      const char c = static_cast<char>(buf[i]);

      if (c == '\n') {
        if (rx_.length() > 0) {
          handleLine(rx_.c_str(), rx_.length());
          rx_ = "";
        }
        continue;
      }

      if (c == '\r') continue;

      // A line this long means the stream has desynchronised. Drop it rather than growing
      // the buffer until the heap gives out.
      if (rx_.length() >= MD_LINK_RX_MAX) {
        MD_LOG.println("[link] oversized line — resynchronising");
        rx_ = "";
        continue;
      }

      rx_ += c;
    }
  }

  const uint32_t now = millis();

  // Drop any half-received line when the port goes away.
  //
  // A host that dies mid-write leaves a fragment in rx_ with no terminating newline. That
  // fragment survives the disconnect, so the *next* session's first frame gets appended to
  // it and the pair is rejected as one malformed line ("bad frame: InvalidInput"). The link
  // recovers on the next retry, which is exactly why this hid for so long.
  const bool attached_now = isAttached();
  if (was_attached_ && !attached_now && rx_.length() > 0) {
    MD_LOG.printf("[link] discarding %u byte partial frame from the closed session\n",
                  static_cast<unsigned>(rx_.length()));
    rx_ = "";
  }
  was_attached_ = attached_now;

  // Inbound traffic is the authoritative sign a host is there.
  //
  // isAttached() reflects the CDC DTR line state, and that flag demonstrably misses
  // reconnects: on alternate agent restarts it stayed false while the host was quite
  // happily sending us frames. Treating it as the sole truth meant we received `identify`
  // and then refused to answer it, which looked like the deck ignoring every other launch.
  // So the flag is now only ever corroborating evidence, never the deciding vote.
  const bool recently_heard = (now - last_inbound_ms_) < MD_LINK_HEARD_RECENTLY_MS;
  const bool host_present = isAttached() || recently_heard;

  // Fast path for a clean disconnect: the port dropped and nothing has arrived since.
  if (session_up_ && !isAttached() && !recently_heard) {
    session_up_ = false;
    host_rev_ = -1;
    MD_LOG.println("[link] host closed the port — session down");
  }

  // Backstop for a host that stops answering without closing the port.
  if (session_up_ && (now - last_inbound_ms_) > MD_LINK_TIMEOUT_MS) {
    session_up_ = false;
    host_rev_ = -1;
    MD_LOG.println("[link] host went quiet — session down");
  }

  if (!session_up_ && host_present && (now - last_hello_ms_) > MD_LINK_HELLO_INTERVAL_MS) {
    sendHello();
    last_hello_ms_ = now;
  }
}

void Link::handleLine(const char *line, size_t len) {
  JsonDocument doc;
  DeserializationError error = deserializeJson(doc, line, len);

  if (error) {
    MD_LOG.printf("[link] bad frame: %s\n", error.c_str());
    return;
  }

  JsonObjectConst frame = doc.as<JsonObjectConst>();
  const char *type = frame["t"] | "";

  last_inbound_ms_ = millis();

  if (strcmp(type, "welcome") == 0) {
    const int proto = frame["proto"] | -1;
    if (proto != MD_PROTO_VERSION) {
      // Refusing to limp along on a mismatched protocol: a loud failure here is far cheaper
      // to diagnose than fields silently going missing later.
      MD_LOG.printf("[link] protocol mismatch: host %d, device %d — refusing session\n",
                    proto, MD_PROTO_VERSION);
      session_up_ = false;
      return;
    }

    session_up_ = true;
    host_rev_ = frame["rev"] | -1;
    MD_LOG.printf("[link] session up with %s (layout rev %d)\n",
                  frame["host"] | "?", host_rev_);
    return;
  }

  if (strcmp(type, "ping") == 0) {
    sendPong(frame["seq"] | 0);
    return;
  }

  // The host asking who we are. It sends this on connecting, because otherwise it would have
  // to wait for our next unsolicited hello — and we only send those while we believe no
  // session exists, so an agent reconnecting inside the timeout window would hear nothing.
  if (strcmp(type, "identify") == 0) {
    // Skip if a scheduled hello just went out. The host's identify and our own 2s timer
    // routinely coincide on connect, and answering both makes the host handshake twice.
    if ((millis() - last_hello_ms_) < MD_LINK_HELLO_DEDUPE_MS) return;

    const bool sent = sendHello();
    last_hello_ms_ = millis();
    if (!sent) MD_LOG.println("[link] identify received but hello could not be sent");
    return;
  }

  if (frame_handler_) frame_handler_(frame);
}

bool Link::sendFrame(const JsonDocument &doc) {
  String out;
  serializeJson(doc, out);
  out += '\n';
  return rawWrite(reinterpret_cast<const uint8_t *>(out.c_str()), out.length());
}

bool Link::sendHello() {
  JsonDocument doc;
  doc["t"] = "hello";
  doc["proto"] = MD_PROTO_VERSION;
  doc["fw"] = MD_FW_VERSION;
  doc["dev"] = MD_DEVICE_NAME;
  doc["rev"] = device_rev_;
  if (have_asset_stamp_) doc["assets"] = asset_stamp_;
  return sendFrame(doc);
}

void Link::sendPong(uint32_t seq) {
  JsonDocument doc;
  doc["t"] = "pong";
  doc["seq"] = seq;
  sendFrame(doc);
}

void Link::sendPress(const String &id, const String &page) {
  JsonDocument doc;
  doc["t"] = "press";
  doc["id"] = id;
  doc["page"] = page;
  sendFrame(doc);
}

void Link::sendRelease(const String &id, const String &page, uint32_t held_ms) {
  JsonDocument doc;
  doc["t"] = "release";
  doc["id"] = id;
  doc["page"] = page;
  doc["held_ms"] = held_ms;
  sendFrame(doc);
}

void Link::sendLog(const char *level, const String &message) {
  JsonDocument doc;
  doc["t"] = "log";
  doc["lvl"] = level;
  doc["msg"] = message;
  sendFrame(doc);
}

void Link::sendLayoutRequest() {
  JsonDocument doc;
  doc["t"] = "layout_req";
  sendFrame(doc);
}
