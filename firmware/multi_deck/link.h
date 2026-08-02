// Transport-agnostic link to the PC agent. See docs/protocol.md.
//
// All the protocol lives in this base class: line framing, the hello/welcome handshake,
// heartbeat and link-down detection. Subclasses supply raw byte IO only. Today that means
// USB CDC; when WiFi arrives it is a second subclass, not a second protocol.
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

#include <functional>

class Link {
 public:
  using FrameHandler = std::function<void(JsonObjectConst)>;

  virtual ~Link() = default;

  virtual bool begin() = 0;

  // Transport-level attachment: a cable is plugged in and the host has opened the port.
  // Distinct from isUp(), which additionally requires a completed handshake.
  virtual bool isAttached() const = 0;

  void setFrameHandler(FrameHandler handler) { frame_handler_ = std::move(handler); }

  // Pumps received bytes, dispatches frames, sends hello until welcomed, and expires the
  // session when the host goes quiet. Call from loop().
  void poll();

  bool sendFrame(const JsonDocument &doc);

  // A session exists and the host has been heard from within MD_LINK_TIMEOUT_MS. When this
  // is false the UI greys out agent-dependent tiles but keeps HID tiles live.
  bool isUp() const { return session_up_; }

  int hostRev() const { return host_rev_; }

  // Convenience senders for the frames the UI raises.
  void sendPress(const String &id, const String &page);
  void sendRelease(const String &id, const String &page, uint32_t held_ms);
  void sendLog(const char *level, const String &message);
  void sendLayoutRequest();

 protected:
  virtual int rawRead(uint8_t *buf, size_t len) = 0;
  virtual bool rawWrite(const uint8_t *buf, size_t len) = 0;

 private:
  void handleLine(const char *line, size_t len);
  bool sendHello();
  void sendPong(uint32_t seq);

  FrameHandler frame_handler_;
  String rx_;
  bool session_up_ = false;
  bool was_attached_ = false;
  int host_rev_ = -1;
  int device_rev_ = 0;
  String asset_stamp_;
  bool have_asset_stamp_ = false;
  uint32_t last_inbound_ms_ = 0;
  uint32_t last_hello_ms_ = 0;

 public:
  // The layout revision reported in hello. Set before begin().
  void setDeviceRev(int rev) { device_rev_ = rev; }

  // The card's asset generation, reported in hello. Call this only when an SD card is actually
  // mounted, including when the stamp itself is empty: the two cases mean different things.
  //
  // No card at all means the agent has nothing to compare and must stay quiet — the missing
  // images already announce themselves loudly elsewhere. A mounted card with no stamp file is
  // a real answer: this card was written before stamps existed. Never called, and the field is
  // omitted from the frame entirely, which is also what firmware predating this reports.
  void setAssetStamp(const String &stamp) {
    asset_stamp_ = stamp;
    have_asset_stamp_ = true;
  }
};

// USB CDC transport over the native USB port (port B), alongside the HID interfaces.
class UsbLink : public Link {
 public:
  bool begin() override;
  bool isAttached() const override;

 protected:
  int rawRead(uint8_t *buf, size_t len) override;
  bool rawWrite(const uint8_t *buf, size_t len) override;
};
