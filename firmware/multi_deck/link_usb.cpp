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
  // Blocking is prevented by the TX timeout set in begin() plus this room check, rather than
  // by guessing whether anyone is listening.
  if (g_cdc.availableForWrite() < static_cast<int>(len)) return false;

  return g_cdc.write(buf, len) == len;
}
