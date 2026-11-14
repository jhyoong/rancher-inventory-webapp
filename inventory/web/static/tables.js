(function () {
  // Sizes as shown by the app (KiB, MiB) and as Kubernetes quantities (Ki, Mi, k, M), all in bytes.
  var UNITS = { "": 1, Ki: Math.pow(1024, 1), Mi: Math.pow(1024, 2), Gi: Math.pow(1024, 3), Ti: Math.pow(1024, 4),
                Pi: Math.pow(1024, 5), Ei: Math.pow(1024, 6), k: 1e3, M: 1e6, G: 1e9, T: 1e12, P: 1e15, E: 1e18 };
  var LAST = ["", "-", "not available", "not collected"];

  function key(text) {
    var t = text.trim();
    if (LAST.indexOf(t.toLowerCase()) !== -1) return { last: true };
    var m = t.match(/^(-?\d+(?:\.\d+)?)(?![\d.])\s*(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E|)B?(?![A-Za-z])/);
    if (m) return { num: parseFloat(m[1]) * UNITS[m[2]] };
    m = t.match(/^[-+]?\d+(?:\.\d+)?/);
    if (m) return { num: parseFloat(m[0]) };
    return { text: t.toLowerCase() };
  }

  function compare(a, b) {
    if (a.last || b.last) return (a.last ? 1 : 0) - (b.last ? 1 : 0);
    if (a.num !== undefined && b.num !== undefined) return a.num - b.num;
    if (a.num !== undefined) return -1;
    if (b.num !== undefined) return 1;
    return a.text < b.text ? -1 : a.text > b.text ? 1 : 0;
  }

  function isData(r) {
    return !(r.cells.length === 1 && r.cells[0].colSpan > 1);
  }

  // Data rows with the single-cell colspan rows (detail rows) that follow them.
  // The colspan rows are not sorted or filtered; they stay with their data row.
  function groups(table) {
    var out = [];
    Array.prototype.slice.call(table.rows, 1).forEach(function (r) {
      if (isData(r)) out.push({ row: r, extras: [] });
      else if (out.length) out[out.length - 1].extras.push(r);
    });
    return out;
  }

  function sortBy(table, headers, col, th) {
    var asc = th.getAttribute("aria-sort") !== "ascending";
    var body = table.tBodies[0];
    var keyed = groups(table).map(function (g, i) {
      var cell = g.row.cells[col];
      return { g: g, i: i, k: cell ? key(cell.textContent) : { last: true } };
    });
    keyed.sort(function (x, y) {
      // missing values stay last in both directions
      if (x.k.last || y.k.last) return compare(x.k, y.k) || x.i - y.i;
      return (asc ? 1 : -1) * compare(x.k, y.k) || x.i - y.i;
    });
    keyed.forEach(function (o) {
      body.appendChild(o.g.row);
      o.g.extras.forEach(function (e) { body.appendChild(e); });
    });
    headers.forEach(function (h) { h.removeAttribute("aria-sort"); });
    th.setAttribute("aria-sort", asc ? "ascending" : "descending");
  }

  function addFilter(table) {
    var input = document.createElement("input");
    input.type = "search";
    input.className = "table-filter";
    input.placeholder = "Filter rows";
    input.setAttribute("aria-label", "Filter rows");
    input.addEventListener("input", function () {
      var q = input.value.toLowerCase();
      groups(table).forEach(function (g) {
        g.row.hidden = q !== "" && g.row.textContent.toLowerCase().indexOf(q) === -1;
        g.extras.forEach(function (e) { e.hidden = g.row.hidden; });
      });
    });
    table.parentNode.insertBefore(input, table);
  }

  function setup(table) {
    if (table.dataset.sortable || table.classList.contains("facts")) return;
    var first = table.rows[0];
    if (!first || !first.cells.length) return;
    var headers = Array.prototype.slice.call(first.cells);
    if (!headers.every(function (c) { return c.tagName === "TH"; })) return;
    table.dataset.sortable = "1";
    headers.forEach(function (th, col) {
      if (th.classList.contains("nosort")) return;
      th.classList.add("sortable");
      th.tabIndex = 0;
      var go = function () { sortBy(table, headers, col, th); };
      th.addEventListener("click", go);
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
      });
    });
    if (groups(table).length > 10) addFilter(table);
  }

  // "Select all shown": ticks or unticks the row checkboxes of its table that the row filter does not hide.
  document.addEventListener("change", function (e) {
    var box = e.target;
    if (!box.matches || !box.matches("input[data-select-all]")) return;
    var table = box.closest("table");
    if (!table) return;
    groups(table).forEach(function (g) {
      if (g.row.hidden) return;
      Array.prototype.forEach.call(g.row.querySelectorAll("input[name=cluster]"), function (c) {
        c.checked = box.checked;
      });
    });
  });

  function init(root) {
    Array.prototype.forEach.call((root || document).querySelectorAll("table"), setup);
  }

  document.addEventListener("DOMContentLoaded", function () { init(document); });
  document.addEventListener("htmx:afterSwap", function (e) { init(e.target); });
})();
