// Light/dark theme. Loaded in <head> so the theme is set before the page paints.
// The choice is kept in localStorage; with no choice the OS setting is used.
// Charts read window.VizPalette and redraw on the "themechange" event.
(function () {
  // Chart palette (dataviz reference palette, categorical slots in fixed order; dark steps chosen for the dark surface).
  var PALETTES = {
    light: {
      series: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
      text: "#52514e", grid: "#e4e3df", surface: "#ffffff"
    },
    dark: {
      series: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
      text: "#c3c2b7", grid: "#33383f", surface: "#1b2027"
    }
  };
  var root = document.documentElement;
  var media = window.matchMedia("(prefers-color-scheme: dark)");

  var chosen = null;
  try { chosen = localStorage.getItem("theme"); } catch (e) { /* storage blocked: follow the OS */ }
  function current() {
    return chosen === "light" || chosen === "dark" ? chosen : (media.matches ? "dark" : "light");
  }
  // The button names the mode it switches to.
  function label(theme) {
    var btn = document.getElementById("theme-toggle");
    if (btn) btn.textContent = theme === "dark" ? "Light mode" : "Dark mode";
  }
  function apply(theme) {
    root.setAttribute("data-theme", theme);
    window.VizPalette = PALETTES[theme];
    label(theme);
    document.dispatchEvent(new CustomEvent("themechange", { detail: theme }));
  }

  apply(current());
  media.addEventListener("change", function () { if (!chosen) apply(current()); });
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    label(current());
    btn.addEventListener("click", function () {
      chosen = current() === "dark" ? "light" : "dark";
      try { localStorage.setItem("theme", chosen); } catch (e) { /* storage blocked: the choice lasts for this page only */ }
      apply(chosen);
    });
  });
})();
