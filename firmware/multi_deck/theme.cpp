#include "theme.h"

#include "assets.h"
#include "config.h"

namespace theme {

lv_style_t screen;
lv_style_t surface;
lv_style_t nav_scrim;
lv_style_t tile;
lv_style_t tile_press;
lv_style_t tile_off;
lv_style_t tab;
lv_style_t tab_active;
lv_style_t label;
lv_style_t label_muted;
lv_style_t toast;

const lv_font_t *const FONT_BASE = &lv_font_montserrat_20;
const lv_font_t *const FONT_TILE = &lv_font_montserrat_28;
const lv_font_t *const FONT_PAD = &lv_font_montserrat_40;

namespace {

Theme g_current;
bool g_initialised = false;
String g_error;

// Presses fade rather than snap. Kept short: over a wallpaper each frame recomposites the
// tile against the image, so a long transition buys polish at a cost that shows.
lv_style_transition_dsc_t g_press_anim;
const lv_style_prop_t kAnimatedProps[] = {
    LV_STYLE_BG_COLOR, LV_STYLE_BG_OPA, LV_STYLE_BORDER_OPA, LV_STYLE_TRANSLATE_Y,
    LV_STYLE_PROP_INV,
};

// LVGL stores opacity 0-255; the theme talks in percent because that is what a human editing
// JSON wants to type.
lv_opa_t opaOf(uint8_t percent) {
  return static_cast<lv_opa_t>((static_cast<uint32_t>(percent) * 255u) / 100u);
}

lv_color_t colorOf(uint32_t rgb) { return lv_color_hex(rgb); }

void initOnce() {
  if (g_initialised) return;
  lv_style_init(&screen);
  lv_style_init(&surface);
  lv_style_init(&nav_scrim);
  lv_style_init(&tile);
  lv_style_init(&tile_press);
  lv_style_init(&tile_off);
  lv_style_init(&tab);
  lv_style_init(&tab_active);
  lv_style_init(&label);
  lv_style_init(&label_muted);
  lv_style_init(&toast);
  g_initialised = true;
}

void resetAll() {
  lv_style_reset(&screen);
  lv_style_reset(&surface);
  lv_style_reset(&nav_scrim);
  lv_style_reset(&tile);
  lv_style_reset(&tile_press);
  lv_style_reset(&tile_off);
  lv_style_reset(&tab);
  lv_style_reset(&tab_active);
  lv_style_reset(&label);
  lv_style_reset(&label_muted);
  lv_style_reset(&toast);
}

// Shared by tiles and nav tabs: same fill, border and corner treatment, different sizes.
void styleAsCard(lv_style_t *style, const Theme &t, lv_opa_t fill_opa) {
  lv_style_set_bg_color(style, colorOf(t.tile));
  lv_style_set_bg_opa(style, fill_opa);
  lv_style_set_radius(style, t.radius);

  // A second stop equal to the first is a wasted per-pixel blend, so only ask for a gradient
  // when the theme actually describes one.
  if (t.tile_grad != t.tile) {
    lv_style_set_bg_grad_color(style, colorOf(t.tile_grad));
    lv_style_set_bg_grad_dir(style, LV_GRAD_DIR_VER);
  } else {
    lv_style_set_bg_grad_dir(style, LV_GRAD_DIR_NONE);
  }

  if (t.border_opa > 0) {
    lv_style_set_border_color(style, colorOf(t.border));
    lv_style_set_border_opa(style, opaOf(t.border_opa));
    lv_style_set_border_width(style, 1);
  } else {
    lv_style_set_border_width(style, 0);
  }

  // Shadows are off deliberately. Over a wallpaper they read as mud, and LVGL's shadow draw
  // is one of the more expensive things available on a CPU-only blitter.
  lv_style_set_shadow_width(style, 0);
}

}  // namespace

void apply(const Theme &t) {
  // ui::rebuild() calls this on every page change, not just on a theme change, so that there
  // is one ordering to get right rather than two. Bailing out here keeps an ordinary page tap
  // from resetting and repopulating ten styles inside LVGL's fixed 48 KB pool.
  if (g_initialised && t == g_current) return;

  initOnce();
  // resetAll() must come before assets::clear(): it drops the styles' references to the old
  // wallpaper, so freeing the pixels underneath cannot leave a style pointing at dead PSRAM.
  resetAll();
  assets::clear();
  g_current = t;
  g_error = "";

  lv_style_transition_dsc_init(&g_press_anim, kAnimatedProps, lv_anim_path_ease_out, 120, 0,
                               nullptr);

  // --- screen ------------------------------------------------------------------------------
  // The single place the backdrop is painted. Nav and content used to paint `bg` themselves,
  // which meant three opaque layers stacked on top of each other and the screen never visible
  // at all — and no way for a wallpaper to show through.
  lv_style_set_bg_color(&screen, colorOf(t.bg));
  lv_style_set_bg_opa(&screen, LV_OPA_COVER);
  lv_style_set_text_color(&screen, colorOf(t.text));
  lv_style_set_text_font(&screen, FONT_BASE);

  // A wallpaper sits on top of `bg`, which still shows if the image is missing or smaller than
  // the screen — so a bad path degrades to the flat colour instead of to a blank panel.
  if (!t.wallpaper.isEmpty()) {
    if (const lv_image_dsc_t *image = assets::load(t.wallpaper)) {
      lv_style_set_bg_image_src(&screen, image);
      lv_style_set_bg_image_opa(&screen, LV_OPA_COVER);
    } else {
      // Surfaced on the panel, not just on the log. Images are the one part of a theme that
      // does not arrive over USB — they have to be written to the card by hand — so "nothing
      // happened" almost always means the file is not where the theme says it is, and the
      // reason needs to reach the person looking at the deck.
      g_error = assets::lastError();
    }
  }

  // --- surface -----------------------------------------------------------------------------
  lv_style_set_bg_opa(&surface, LV_OPA_TRANSP);
  lv_style_set_border_width(&surface, 0);
  lv_style_set_pad_all(&surface, 0);
  lv_style_set_radius(&surface, 0);

  // --- tiles -------------------------------------------------------------------------------
  styleAsCard(&tile, t, opaOf(t.tile_opa));
  lv_style_set_transition(&tile, &g_press_anim);

  styleAsCard(&tile_press, t, opaOf(t.tile_opa));
  lv_style_set_bg_color(&tile_press, colorOf(t.accent));
  lv_style_set_bg_grad_dir(&tile_press, LV_GRAD_DIR_NONE);
  lv_style_set_bg_opa(&tile_press, opaOf(t.tile_opa > 75 ? 100 : t.tile_opa + 25));
  if (t.border_opa > 0) lv_style_set_border_opa(&tile_press, LV_OPA_60);
  // Presses read as the tile sinking. Deliberately a translate rather than a scale: LVGL's
  // heap is a fixed 48 KB pool (LV_MEM_SIZE) and transforming a 190x130 tile needs a ~49 KB
  // layer, so a scale animation would fail to allocate at runtime. A translate needs none.
  lv_style_set_translate_y(&tile_press, 2);

  // Agent-dependent tile with no agent. The old treatment dropped the whole object to 40%
  // opacity, which is fine on a flat background and turns to mud over a photo — so the fill
  // stays put and the signal moves to the label and border instead.
  styleAsCard(&tile_off, t, opaOf(t.tile_opa > 30 ? t.tile_opa - 20 : t.tile_opa));
  if (t.border_opa > 0) lv_style_set_border_opa(&tile_off, LV_OPA_10);

  // --- nav ---------------------------------------------------------------------------------
  // Over a wallpaper the tabs need something behind them or the labels sit on open photo, so
  // the bar carries a scrim of its own rather than inheriting the screen's transparency.
  if (!t.wallpaper.isEmpty()) {
    lv_style_set_bg_color(&nav_scrim, colorOf(t.bg));
    lv_style_set_bg_opa(&nav_scrim, LV_OPA_40);
  } else {
    lv_style_set_bg_opa(&nav_scrim, LV_OPA_TRANSP);
  }
  lv_style_set_border_width(&nav_scrim, 0);
  lv_style_set_pad_all(&nav_scrim, 0);
  lv_style_set_radius(&nav_scrim, 0);

  styleAsCard(&tab, t, opaOf(t.tile_opa));
  lv_style_set_radius(&tab, t.radius > 8 ? 8 : t.radius);

  styleAsCard(&tab_active, t, LV_OPA_COVER);
  lv_style_set_bg_color(&tab_active, colorOf(t.accent));
  lv_style_set_bg_grad_dir(&tab_active, LV_GRAD_DIR_NONE);
  lv_style_set_radius(&tab_active, t.radius > 8 ? 8 : t.radius);

  // --- text --------------------------------------------------------------------------------
  lv_style_set_text_color(&label, colorOf(t.text));
  lv_style_set_text_color(&label_muted, colorOf(t.text_muted));

  // --- toast -------------------------------------------------------------------------------
  lv_style_set_bg_color(&toast, colorOf(t.tile));
  lv_style_set_bg_opa(&toast, LV_OPA_90);
  lv_style_set_text_color(&toast, colorOf(t.text));
  lv_style_set_pad_all(&toast, 10);
  lv_style_set_radius(&toast, t.radius);
}

const Theme &current() { return g_current; }

String takeError() {
  String error = g_error;
  g_error = "";
  return error;
}

lv_color_t statusColor(bool link_up) {
  return colorOf(link_up ? g_current.ok : g_current.idle);
}

}  // namespace theme
