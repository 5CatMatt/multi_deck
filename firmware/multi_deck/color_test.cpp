#include "color_test.h"

#include <Arduino.h>

#include "config.h"
#include "theme.h"

namespace color_test {
namespace {

constexpr int MARGIN = 8;
constexpr int GAP = 4;
constexpr int USABLE = MD_SCREEN_W - MARGIN * 2;

// A patch with every decoration deliberately switched off — no radius, no border, no
// gradient, full opacity. Anything else would mean the page is testing the style system
// rather than the panel.
void swatch(lv_obj_t *parent, int x, int y, int w, int h, uint32_t rgb) {
  lv_obj_t *box = lv_obj_create(parent);
  lv_obj_remove_style_all(box);
  lv_obj_set_pos(box, x, y);
  lv_obj_set_size(box, w, h);
  lv_obj_set_style_bg_color(box, lv_color_hex(rgb), 0);
  lv_obj_set_style_bg_opa(box, LV_OPA_COVER, 0);
}

void caption(lv_obj_t *parent, int x, int y, int w, const char *text, const lv_font_t *font) {
  lv_obj_t *label = lv_label_create(parent);
  lv_label_set_text(label, text);
  lv_obj_add_style(label, &theme::label, 0);
  lv_obj_set_style_text_font(label, font, 0);
  lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
  lv_obj_set_pos(label, x, y);
  lv_obj_set_width(label, w);
}

void line(lv_obj_t *parent, int y, const String &text, lv_style_t *style) {
  lv_obj_t *label = lv_label_create(parent);
  lv_label_set_text(label, text.c_str());
  lv_obj_add_style(label, style, 0);
  lv_obj_set_style_text_font(label, &lv_font_montserrat_14, 0);
  lv_obj_set_pos(label, MARGIN, y);
}

String hex(uint32_t rgb) {
  char buf[8];
  snprintf(buf, sizeof(buf), "#%06lX", static_cast<unsigned long>(rgb));
  return String(buf);
}

struct Patch {
  uint32_t rgb;
  const char *name;
};

void patchRow(lv_obj_t *parent, int y, int h, const Patch *patches, int count) {
  const int w = (USABLE - GAP * (count - 1)) / count;
  for (int i = 0; i < count; i++) {
    const int x = MARGIN + i * (w + GAP);
    swatch(parent, x, y, w, h, patches[i].rgb);
    caption(parent, x, y + h + 2, w, patches[i].name, &lv_font_montserrat_14);
  }
}

// Row 1 — full saturation, where no camera and no pair of eyes can mistake one hue for
// another. Every patch is named, so the test is a direct yes/no: does the one labelled GREEN
// look green? An earlier version of this page asked the same question at level 0x30, which was
// useless — too dark to judge, so it produced opinions instead of answers.
void buildPrimaries(lv_obj_t *parent, int y, int h) {
  static const Patch kFull[] = {
      {0xFF0000, "RED"},     {0x00FF00, "GREEN"}, {0x0000FF, "BLUE"},  {0xFFFF00, "YELLOW"},
      {0x00FFFF, "CYAN"},    {0xFF00FF, "MAGNTA"}, {0xFFFFFF, "WHITE"}, {0x000000, "BLACK"},
  };
  patchRow(parent, y, h, kFull, sizeof(kFull) / sizeof(kFull[0]));
}

// Row 2 — the same hues at half level. Hue should survive; if RED and YELLOW become
// indistinguishable here, the panel is crushing midtones, not just shadows.
void buildHalfPrimaries(lv_obj_t *parent, int y, int h) {
  static const Patch kHalf[] = {
      {0x800000, "red"},   {0x008000, "green"},   {0x000080, "blue"},  {0x808000, "yellow"},
      {0x008080, "cyan"},  {0x800080, "magenta"}, {0x808080, "grey"},  {0x404040, "dk grey"},
  };
  patchRow(parent, y, h, kHalf, sizeof(kHalf) / sizeof(kHalf[0]));
}

// Row 3 — greyscale, weighted at the bottom. Finds the panel's black floor: the level below
// which nothing can separate itself, which is what decides whether dark themes are viable.
void buildGreyRamp(lv_obj_t *parent, int y, int h) {
  static const Patch kGreys[] = {
      {0x000000, "00"}, {0x080808, "08"}, {0x101010, "10"}, {0x181818, "18"},
      {0x202020, "20"}, {0x282828, "28"}, {0x303030, "30"}, {0x404040, "40"},
      {0x606060, "60"}, {0x808080, "80"},
  };
  patchRow(parent, y, h, kGreys, sizeof(kGreys) / sizeof(kGreys[0]));
}

// Row 4 — the three theme accents side by side. Simultaneous comparison, which is what human
// vision is actually good at, unlike remembering a colour across a theme switch.
void buildAccents(lv_obj_t *parent, int y, int h, const DeckConfig &config) {
  const int count = static_cast<int>(config.themes.size());
  if (count == 0) return;

  const int w = (USABLE - GAP * (count - 1)) / count;
  for (int i = 0; i < count; i++) {
    const Theme &t = config.themes[i];
    const int x = MARGIN + i * (w + GAP);
    swatch(parent, x, y, w, h, t.accent);
    caption(parent, x, y + h + 2, w, (t.name + " " + hex(t.accent)).c_str(),
            &lv_font_montserrat_14);
  }
}

}  // namespace

void build(lv_obj_t *parent, const DeckConfig &config) {
  buildPrimaries(parent, 2, 58);
  buildHalfPrimaries(parent, 82, 52);
  buildGreyRamp(parent, 156, 44);
  buildAccents(parent, 222, 44, config);

  const Theme &t = theme::current();
  line(parent, 292,
       String("active \"") + t.name + "\"  bg " + hex(t.bg) + "  tile " + hex(t.tile) +
           "  accent " + hex(t.accent) + "  text " + hex(t.text),
       &theme::label_muted);

  // The instruction is on the page because this is a bench tool used with a camera in one
  // hand: the answer that matters should not require remembering what was being asked.
  line(parent, 314,
       String("Top row must read as its labels. If GREEN is not green, the pipeline is at "
              "fault, not the panel."),
       &theme::label);
  line(parent, 336,
       String("Bottom grey ramp: the first patch that separates from BLACK is this panel's "
              "usable floor for dark themes."),
       &theme::label_muted);
}

}  // namespace color_test
