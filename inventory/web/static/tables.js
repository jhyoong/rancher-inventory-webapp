(function () {
  var UNITS = { B: 1, KiB: 1024, MiB: 1048576, GiB: 1073741824, TiB: 1099511627776 };
  var LAST = ["", "-", "not available", "not collected"];

  function key(text) {
    var t = text.trim();
    if (LAST.indexOf(t.toLowerCase()) !== -1) return { last: true };
    var m = t.match(/^(-?\d+(?:\.\d+)?)\s*(B|KiB|MiB|GiB|TiB)\b/);
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

  function init(root) {
    Array.prototype.forEach.call((root || document).querySelectorAll("table"), setup);
  }

  document.addEventListener("DOMContentLoaded", function () { init(document); });
  document.addEventListener("htmx:afterSwap", function (e) { init(e.target); });
})();
