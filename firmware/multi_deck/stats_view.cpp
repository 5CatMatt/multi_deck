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

void buildGauge(Gauge &gauge, lv_obj_t *parent, int x, int y, const char *caption) {
  const Theme &t = theme::current();

  gauge.arc = lv_arc_create(parent);
  lv_obj_set_size(gauge.arc, 150, 150);
  lv_obj_set_pos(gauge.arc, x, y);
  lv_arc_set_rotation(gauge.arc, 135);
  lv_arc_set_bg_angles(gauge.arc, 0, 270);
  lv_arc_set_range(gauge.arc, 0, 100);
  lv_arc_set_value(gauge.arc, 0);
  lv_obj_remove_style(gauge.arc, nullptr, LV_PART_KNOB);
  lv_obj_remove_flag(gauge.arc, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_set_style_arc_color(gauge.arc, lv_color_hex(t.accent), LV_PART_INDICATOR);
  // The track behind the indicator used to come from LVGL's default theme, so it ignored
  // deck.json entirely — one of the reasons this page looked unresponsive to theme edits.
  lv_obj_set_style_arc_color(gauge.arc, lv_color_hex(t.tile), LV_PART_MAIN);
  lv_obj_set_style_bg_opa(gauge.arc, LV_OPA_TRANSP, LV_PART_MAIN);

  gauge.value = lv_label_create(parent);
  lv_label_set_text(gauge.value, "--");
  lv_obj_add_style(gauge.value, &theme::label, 0);
  // The number is the point of the gauge — read at a glance, from across the desk.
  lv_obj_set_style_text_font(gauge.value, &lv_font_montserrat_40, 0);
  lv_obj_align_to(gauge.value, gauge.arc, LV_ALIGN_CENTER, 0, -10);

  gauge.caption = lv_label_create(parent);
  lv_label_set_text(gauge.caption, caption);
  lv_obj_add_style(gauge.caption, &theme::label_muted, 0);
  lv_obj_align_to(gauge.caption, gauge.arc, LV_ALIGN_CENTER, 0, 18);
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

  buildGauge(g_cpu, parent, 60, 20, "CPU");
  buildGauge(g_mem, parent, 320, 20, "MEM");
  buildGauge(g_gpu, parent, 580, 20, "GPU");

  g_chart = lv_chart_create(parent);
  lv_obj_set_size(g_chart, 700, 140);
  lv_obj_set_pos(g_chart, 50, 200);
  lv_chart_set_type(g_chart, LV_CHART_TYPE_LINE);
  lv_chart_set_point_count(g_chart, 60);
  lv_chart_set_range(g_chart, LV_CHART_AXIS_PRIMARY_Y, 0, 100);
  lv_chart_set_update_mode(g_chart, LV_CHART_UPDATE_MODE_SHIFT);
  lv_obj_set_style_bg_opa(g_chart, LV_OPA_TRANSP, 0);
  lv_obj_set_style_border_width(g_chart, 0, 0);
  lv_obj_set_style_size(g_chart, 0, 0, LV_PART_INDICATOR);
  // Grid lines were another default-theme leftover.
  lv_obj_set_style_line_color(g_chart, lv_color_hex(t.tile), LV_PART_MAIN);
  lv_obj_set_style_line_opa(g_chart, LV_OPA_60, LV_PART_MAIN);

  g_cpu_series = lv_chart_add_series(g_chart, lv_color_hex(t.accent), LV_CHART_AXIS_PRIMARY_Y);

  g_detail = lv_label_create(parent);
  lv_label_set_text(g_detail, "waiting for agent");
  lv_obj_add_style(g_detail, &theme::label_muted, 0);
  lv_obj_align(g_detail, LV_ALIGN_BOTTOM_MID, 0, -10);

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
