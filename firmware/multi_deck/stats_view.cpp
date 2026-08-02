#include "stats_view.h"

#include <Arduino.h>

#include "config.h"
#include "theme.h"

namespace stats_view {
namespace {

struct Gauge {
  lv_obj_t *arc = nullptr;
  lv_obj_t *value = nullptr;
  lv_obj_t *caption = nullptr;
};

Gauge g_cpu;
Gauge g_mem;
Gauge g_gpu;

lv_obj_t *g_chart = nullptr;
lv_chart_series_t *g_cpu_series = nullptr;
lv_obj_t *g_detail = nullptr;

bool g_visible = false;

using theme::PAD;

// The page is laid out from the content area rather than from hardcoded pixel positions, so
// the three gauge cards stay evenly distributed if any of these change.
constexpr int AREA_W = MD_SCREEN_W;
constexpr int AREA_H = MD_SCREEN_H - theme::NAV_H;

constexpr int GAUGE_W = (AREA_W - 4 * PAD) / 3;
constexpr int GAUGE_H = 214;

// 150 rather than 160 to leave the caption a line of FONT_TILE. "CPU" and "GPU" differ by one
// letter, and at FONT_BASE that is not a difference you can read at arm's length.
constexpr int ARC_SIZE = 150;

constexpr int GAUGE_INNER_H = GAUGE_H - 2 * theme::CARD_PAD;

// The arc sits at the top of the card, so its centre is above the card's. This is the offset
// that puts the reading in the middle of the ring rather than the middle of the card.
constexpr int VALUE_DY = (ARC_SIZE - GAUGE_INNER_H) / 2;

constexpr int CHART_Y = PAD + GAUGE_H + PAD;
constexpr int CHART_H = AREA_H - CHART_Y - PAD;

// A card. Not a tile: nothing on this page is a press target, so it carries no pressed state
// and no click handling — but the same fill, so the page belongs to the same deck.
lv_obj_t *makeCard(lv_obj_t *parent, int x, int y, int w, int h) {
  lv_obj_t *card = lv_obj_create(parent);
  lv_obj_remove_style_all(card);
  lv_obj_add_style(card, &theme::panel, 0);
  lv_obj_set_pos(card, x, y);
  lv_obj_set_size(card, w, h);
  lv_obj_remove_flag(card, LV_OBJ_FLAG_SCROLLABLE);
  return card;
}

void buildGauge(Gauge &gauge, lv_obj_t *parent, int index, const char *caption) {
  const Theme &t = theme::current();

  lv_obj_t *card = makeCard(parent, PAD + index * (GAUGE_W + PAD), PAD, GAUGE_W, GAUGE_H);

  gauge.arc = lv_arc_create(card);
  lv_obj_set_size(gauge.arc, ARC_SIZE, ARC_SIZE);
  lv_obj_align(gauge.arc, LV_ALIGN_TOP_MID, 0, 0);
  lv_arc_set_rotation(gauge.arc, 135);
  lv_arc_set_bg_angles(gauge.arc, 0, 270);
  lv_arc_set_range(gauge.arc, 0, 100);
  lv_arc_set_value(gauge.arc, 0);
  lv_obj_remove_style(gauge.arc, nullptr, LV_PART_KNOB);
  lv_obj_remove_flag(gauge.arc, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_set_style_arc_color(gauge.arc, lv_color_hex(t.accent), LV_PART_INDICATOR);
  // The track behind the indicator used to come from LVGL's default theme, so it ignored
  // deck.json entirely — one of the reasons this page looked unresponsive to theme edits.
  lv_obj_set_style_arc_color(gauge.arc, lv_color_hex(t.text_muted), LV_PART_MAIN);
  lv_obj_set_style_arc_opa(gauge.arc, LV_OPA_30, LV_PART_MAIN);
  lv_obj_set_style_bg_opa(gauge.arc, LV_OPA_TRANSP, LV_PART_MAIN);

  // Thicker than LVGL's default. At arm's length a thin ring reads as a hairline rather than
  // as a quantity, which is the whole job of a gauge.
  lv_obj_set_style_arc_width(gauge.arc, 14, LV_PART_MAIN);
  lv_obj_set_style_arc_width(gauge.arc, 14, LV_PART_INDICATOR);

  gauge.value = lv_label_create(card);
  lv_label_set_text(gauge.value, "--");
  lv_obj_add_style(gauge.value, &theme::label, 0);
  // The number is the point of the gauge — read at a glance, from across the desk.
  lv_obj_set_style_text_font(gauge.value, theme::FONT_STAT_VALUE, 0);

  // Fixed width and centred text, rather than centring the label itself.
  //
  // lv_obj_align_to() resolves to a one-shot set_pos, so a label positioned while it read "--"
  // keeps that left edge and grows rightwards as the reading goes 7% -> 45% -> 100%. The number
  // visibly drifted off centre as load changed. Giving the label the ring's full width and
  // centring the text inside it makes growth symmetric and needs no relayout at all.
  lv_obj_set_width(gauge.value, ARC_SIZE);
  lv_obj_set_style_text_align(gauge.value, LV_TEXT_ALIGN_CENTER, 0);
  lv_obj_align(gauge.value, LV_ALIGN_CENTER, 0, VALUE_DY);

  gauge.caption = lv_label_create(card);
  lv_label_set_text(gauge.caption, caption);
  lv_obj_add_style(gauge.caption, &theme::label_muted, 0);
  lv_obj_set_style_text_font(gauge.caption, theme::FONT_STAT_LABEL, 0);
  lv_obj_set_width(gauge.caption, ARC_SIZE);
  lv_obj_set_style_text_align(gauge.caption, LV_TEXT_ALIGN_CENTER, 0);
  lv_obj_align(gauge.caption, LV_ALIGN_BOTTOM_MID, 0, 0);
}

// Writes a gauge from an optional field. Missing key -> "--" and the arc drops to zero, so
// an absent provider is visibly absent rather than frozen at its last value.
void setGauge(Gauge &gauge, JsonVariantConst value, const char *suffix) {
  if (gauge.arc == nullptr) return;

  if (value.isNull()) {
    lv_label_set_text(gauge.value, "--");
    lv_arc_set_value(gauge.arc, 0);
    return;
  }

  const float number = value.as<float>();
  lv_arc_set_value(gauge.arc, static_cast<int32_t>(number));
  lv_label_set_text_fmt(gauge.value, "%d%s", static_cast<int>(number), suffix);
}

void appendTemp(String &out, const char *label, JsonVariantConst value) {
  out += label;
  if (value.isNull()) {
    out += " --";
  } else {
    out += " ";
    out += String(value.as<float>(), 0);
    out += "C";
  }
  out += "   ";
}

}  // namespace

void build(lv_obj_t *parent) {
  const Theme &t = theme::current();

  buildGauge(g_cpu, parent, 0, "CPU");
  buildGauge(g_mem, parent, 1, "MEM");
  buildGauge(g_gpu, parent, 2, "GPU");

  lv_obj_t *card = makeCard(parent, PAD, CHART_Y, AREA_W - 2 * PAD, CHART_H);

  g_chart = lv_chart_create(card);
  lv_obj_set_size(g_chart, lv_pct(100), CHART_H - 2 * theme::CARD_PAD - 30);
  lv_obj_align(g_chart, LV_ALIGN_TOP_MID, 0, 0);
  lv_chart_set_type(g_chart, LV_CHART_TYPE_LINE);
  lv_chart_set_point_count(g_chart, 60);
  lv_chart_set_range(g_chart, LV_CHART_AXIS_PRIMARY_Y, 0, 100);
  lv_chart_set_update_mode(g_chart, LV_CHART_UPDATE_MODE_SHIFT);
  lv_obj_set_style_bg_opa(g_chart, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(g_chart, 0, 0);
  lv_obj_set_style_pad_all(g_chart, 0, 0);
  lv_obj_set_style_size(g_chart, 0, 0, LV_PART_INDICATOR);

  // Horizontal guides only. Vertical ones divide a time series into nothing meaningful — the
  // x axis is just "the last 60 seconds" — so they were clutter competing with the trace.
  lv_chart_set_div_line_count(g_chart, 3, 0);
  // Grid lines were another default-theme leftover.
  lv_obj_set_style_line_color(g_chart, lv_color_hex(t.text_muted), LV_PART_MAIN);
  lv_obj_set_style_line_opa(g_chart, LV_OPA_20, LV_PART_MAIN);

  g_cpu_series = lv_chart_add_series(g_chart, lv_color_hex(t.accent), LV_CHART_AXIS_PRIMARY_Y);

  // The one place on this page where a wallpaper genuinely hurts legibility: a hairline trace
  // over a busy photo. Fixed here, on the trace, rather than by making the whole page opaque.
  lv_obj_set_style_line_width(g_chart, 3, LV_PART_ITEMS);

  g_detail = lv_label_create(card);
  lv_label_set_text(g_detail, "waiting for agent");
  lv_obj_add_style(g_detail, &theme::label_muted, 0);
  // Century carries no LV_SYMBOL glyphs, and this line embeds the up/down arrows in its text.
  // They resolve through the font's Montserrat fallback — see fonts.h.
  lv_obj_set_style_text_font(g_detail, theme::FONT_STAT_TEXT, 0);
  // Full width and centred text, for the same reason as the gauge readings: this line is
  // rewritten every second and its length changes as fields come and go.
  lv_obj_set_width(g_detail, lv_pct(100));
  lv_obj_set_style_text_align(g_detail, LV_TEXT_ALIGN_CENTER, 0);
  lv_obj_align(g_detail, LV_ALIGN_BOTTOM_MID, 0, 0);

  g_visible = true;
}

void detach() {
  g_cpu = Gauge{};
  g_mem = Gauge{};
  g_gpu = Gauge{};
  g_chart = nullptr;
  g_cpu_series = nullptr;
  g_detail = nullptr;
  g_visible = false;
}

void update(JsonObjectConst frame) {
  if (!g_visible) return;

  setGauge(g_cpu, frame["cpu"], "%");
  setGauge(g_mem, frame["mem"], "%");
  setGauge(g_gpu, frame["gpu"], "%");

  if (g_chart != nullptr && g_cpu_series != nullptr && !frame["cpu"].isNull()) {
    lv_chart_set_next_value(g_chart, g_cpu_series,
                            static_cast<int32_t>(frame["cpu"].as<float>()));
  }

  if (g_detail != nullptr) {
    String detail;
    appendTemp(detail, "CPU", frame["cpu_temp"]);
    appendTemp(detail, "GPU", frame["gpu_temp"]);

    if (!frame["mem_used_gb"].isNull() && !frame["mem_total_gb"].isNull()) {
      detail += String(frame["mem_used_gb"].as<float>(), 1) + "/" +
                String(frame["mem_total_gb"].as<float>(), 0) + " GB   ";
    }

    if (!frame["net_down_mbps"].isNull()) {
      detail += LV_SYMBOL_DOWN " " + String(frame["net_down_mbps"].as<float>(), 1) + "  ";
    }
    if (!frame["net_up_mbps"].isNull()) {
      detail += LV_SYMBOL_UP " " + String(frame["net_up_mbps"].as<float>(), 1);
    }

    lv_label_set_text(g_detail, detail.c_str());
  }
}

bool isVisible() { return g_visible; }

}  // namespace stats_view
