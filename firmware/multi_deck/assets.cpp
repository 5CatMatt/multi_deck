#include "assets.h"

#include <SD.h>
#include <esp_heap_caps.h>

#include <vector>

#include "board_port.h"
#include "config.h"

namespace assets {
namespace {

// MDI1 container, written by tools/make_assets.py:
//   0  4  magic "MDI1"
//   4  2  width  (uint16 LE)
//   6  2  height (uint16 LE)
//   8  .. RGB565 pixels, row-major
constexpr size_t HEADER_BYTES = 8;
const char MAGIC[4] = {'M', 'D', 'I', '1'};

struct Entry {
  String path;
  uint8_t *pixels;
  lv_image_dsc_t dsc;
};

std::vector<Entry *> g_cache;
size_t g_bytes = 0;
String g_error;

bool fail(const String &reason) {
  g_error = reason;
  MD_LOG.printf("[assets] %s\n", reason.c_str());
  return false;
}

Entry *find(const String &path) {
  for (auto *entry : g_cache) {
    if (entry->path == path) return entry;
  }
  return nullptr;
}

void destroy(Entry *entry) {
  if (entry->pixels != nullptr) heap_caps_free(entry->pixels);
  delete entry;
}

}  // namespace

const lv_image_dsc_t *load(const String &path) {
  if (path.isEmpty()) return nullptr;

  if (Entry *cached = find(path)) return &cached->dsc;

  const uint32_t started = millis();
  g_error = "";

  // Checked first because it is the failure that looks least like itself: the layout arrives
  // over USB, so with no card the deck runs, navigates and switches themes perfectly, and only
  // the images are missing.
  if (!board_port::sdMounted()) {
    fail("No SD card — images live on the card, not in the layout");
    return nullptr;
  }

  File file = SD.open(path.c_str(), FILE_READ);
  if (!file) {
    fail("Not on the card: " + path);
    return nullptr;
  }

  uint8_t header[HEADER_BYTES];
  if (file.read(header, HEADER_BYTES) != HEADER_BYTES ||
      memcmp(header, MAGIC, sizeof(MAGIC)) != 0) {
    file.close();
    fail("Not an MDI1 image: " + path + " — run tools/make_assets.py");
    return nullptr;
  }

  const uint16_t width = static_cast<uint16_t>(header[4] | (header[5] << 8));
  const uint16_t height = static_cast<uint16_t>(header[6] | (header[7] << 8));
  const size_t expected = static_cast<size_t>(width) * height * 2;
  const size_t body = file.size() - HEADER_BYTES;

  if (width == 0 || height == 0 || body != expected) {
    file.close();
    fail(path + " claims " + width + "x" + height + " but carries " + static_cast<uint32_t>(body) +
         " bytes");
    return nullptr;
  }

  // PSRAM, not internal RAM: a full-screen wallpaper is 750KB against 8MB of PSRAM and only
  // ~220KB of free internal heap. The RGB framebuffer has first claim on what is left.
  uint8_t *pixels = static_cast<uint8_t *>(heap_caps_malloc(expected, MALLOC_CAP_SPIRAM));
  if (pixels == nullptr) {
    file.close();
    fail("Out of PSRAM for " + path);
    return nullptr;
  }

  // Chunked so a short read is caught rather than leaving a partly-filled buffer to be
  // rendered as garbage — but in large chunks, because the per-call FatFS overhead is what
  // decides the transfer rate here, not the SPI clock. At 8KB a 750KB wallpaper took 583ms on
  // a bus whose floor is nearer 300ms.
  constexpr size_t CHUNK = 64 * 1024;

  size_t done = 0;
  while (done < expected) {
    const size_t want = min(CHUNK, expected - done);
    const int got = file.read(pixels + done, want);
    if (got <= 0) break;
    done += static_cast<size_t>(got);
  }
  file.close();

  if (done != expected) {
    heap_caps_free(pixels);
    fail("Short read on " + path);
    return nullptr;
  }

  auto *entry = new Entry{path, pixels, {}};

  // There is no decode step — MDI1 is already the format LVGL renders — so the descriptor
  // simply points at the bytes we just read.
  entry->dsc.header.magic = LV_IMAGE_HEADER_MAGIC;
  entry->dsc.header.cf = LV_COLOR_FORMAT_RGB565;
  entry->dsc.header.flags = 0;
  entry->dsc.header.w = width;
  entry->dsc.header.h = height;
  entry->dsc.header.stride = static_cast<uint32_t>(width) * 2;
  entry->dsc.data = pixels;
  entry->dsc.data_size = expected;

  g_cache.push_back(entry);
  g_bytes += expected;

  MD_LOG.printf("[assets] %s %ux%u, %u KB in %lu ms\n", path.c_str(), width, height,
                static_cast<unsigned>(expected / 1024),
                static_cast<unsigned long>(millis() - started));
  return &entry->dsc;
}

void clear() {
  if (g_cache.empty()) return;

  for (auto *entry : g_cache) destroy(entry);
  g_cache.clear();
  g_bytes = 0;
}

size_t bytesHeld() { return g_bytes; }

const String &lastError() { return g_error; }

}  // namespace assets
