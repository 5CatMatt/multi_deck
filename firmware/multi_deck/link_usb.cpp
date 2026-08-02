#include <USB.h>
#include <USBCDC.h>

#include "config.h"
#include "link.h"

namespace {

// Our own CDC instance rather than the global `USBSerial`, whose existence depends on the
// "USB CDC On Boot" build flag. Owning it here means the agent link is the same object
// regardless of that setting. Debug logging is likewise pinned to UART0 via MD_LOG, so the
// two never share a destination.
USBCDC g_cdc;

}  // namespace

bool UsbLink::begin() {
  g_cdc.begin();

  // Bound how long a write may block. The UI, the link and HID all share one thread, so an
  // unbounded write to a host that has stopped reading would freeze the display.
  g_cdc.setTxTimeoutMs(MD_CDC_TX_TIMEOUT_MS);

  MD_LOG.println("[link] USB CDC started on the native USB port");
  return true;
}

bool UsbLink::isAttached() const {
  // Reflects the CDC DTR line state. Known to miss reconnects — see the comment in
  // Link::poll(); callers must not treat a false here as proof no host is listening.
  return static_cast<bool>(g_cdc);
}

int UsbLink::rawRead(uint8_t *buf, size_t len) {
  const int available = g_cdc.available();
  if (available <= 0) return 0;

  const size_t want = (static_cast<size_t>(available) < len) ? available : len;
  return g_cdc.read(buf, want);
}

bool UsbLink::rawWrite(const uint8_t *buf, size_t len) {
  // Deliberately NOT gated on isAttached().
  //
  // That gate was the bug behind "every other launch does nothing": the DTR flag misses
  // reconnects, so the device would happily receive `identify` and then refuse to send the
  // `hello` answering it. Reads were never gated, which is why the failure was one-directional
  // and looked like the host was at fault.
  //
  // Nor is there a room check any more, which was the same mistake one layer down.
  //
  // It used to refuse the write unless the whole frame fitted in availableForWrite(). That is
  // the TinyUSB CDC TX FIFO, CFG_TUD_CDC_TX_BUFSIZE — **64 bytes** — so it was a hard 64-byte
  // ceiling on every frame the device can ever send, expressed as a transient-looking check.
  //
  // `hello` sat at exactly 64 bytes, so it went out only when the FIFO happened to be empty.
  // That is what made reconnects look intermittent for so long. Adding one field to `hello` in
  // 0.4.5 took it to 76 and the deck could not handshake at all: it received `identify`, tried
  // to answer, and silently refused, every time.
  //
  // USBCDC::write() already does the right thing — writes what fits, flushes, repeats until
  // done or until the TX timeout set in begin() expires. Blocking stays bounded without
  // capping frame size, which is what the check was actually for.
  const size_t sent = g_cdc.write(buf, len);
  if (sent == len) return true;

  // Zero and partial are different events, and only one of them is a fault.
  //
  // Zero means USBCDC::write() found the port closed and never started — normal for the couple
  // of seconds after a reset, while the device announces itself and the host has not reopened
  // COM yet. poll() simply tries again. Logging that as a problem would put a scary line in
  // every boot log, and a diagnostic that cries wolf is one nobody reads.
  //
  // A partial write is a real fault: the frame went out without its terminating newline, so the
  // host joins it to the next one and drops the pair before resynchronising. Silence here is
  // how the 64-byte ceiling hid for weeks, so this one is always said out loud.
  if (sent > 0) {
    MD_LOG.printf("[link] partial write: %u of %u bytes — host will drop the next frame too\n",
                  static_cast<unsigned>(sent), static_cast<unsigned>(len));
  }
  return false;
}
