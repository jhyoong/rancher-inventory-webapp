(function () {
  var UNITS = { B: 1, KiB: 1024, MiB: 1048576, GiB: 1073741824, TiB: 1099511627776 };
  var LAST = ["", "-", "not available", "not collected"];

  function key(text) {
    var t = text.trim();
    if (LAST.indexOf(t.toLowerCase()) !== -1) return { last: true };
    var m = t.match(/^(-?\d+(?:\.\d+)?)\s*(B|KiB|MiB|GiB|TiB)\b/);
    if (m) return { num: parseFloat(m[1]) * UNITS[m[2]] };
    m = t.match(/-?\d+(?:\.\d+)?/);
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

  function sortBy(table, headers, col, th) {
    var asc = th.getAttribute("aria-sort") !== "ascending";
    var body = table.tBodies[0];
    var rows = Array.prototype.slice.call(body.rows).filter(function (r) {
      return r !== table.rows[0];
    });
    var keyed = rows.map(function (r, i) {
      var cell = r.cells[col];
      return { row: r, i: i, k: cell ? key(cell.textContent) : { last: true } };
    });
    keyed.sort(function (x, y) {
      // missing values stay last in both directions
      if (x.k.last || y.k.last) return compare(x.k, y.k) || x.i - y.i;
      return (asc ? 1 : -1) * compare(x.k, y.k) || x.i - y.i;
    });
    keyed.forEach(function (o) { body.appendChild(o.row); });
    headers.forEach(function (h) { h.removeAttribute("aria-sort"); });
    th.setAttribute("aria-sort", asc ? "ascending" : "descending");
  }

  function addFilter(table, dataRows) {
    var input = document.createElement("input");
    input.type = "search";
    input.className = "table-filter";
    input.placeholder = "Filter rows";
    input.setAttribute("aria-label", "Filter rows");
    input.addEventListener("input", function () {
      var q = input.value.toLowerCase();
      dataRows.forEach(function (r) {
        r.hidden = q !== "" && r.textContent.toLowerCase().indexOf(q) === -1;
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
    var dataRows = Array.prototype.slice.call(table.rows, 1);
    if (dataRows.length > 10) addFilter(table, dataRows);
  }

  function init(root) {
    Array.prototype.forEach.call((root || document).querySelectorAll("table"), setup);
  }

  document.addEventListener("DOMContentLoaded", function () { init(document); });
  document.addEventListener("htmx:afterSwap", function (e) { init(e.target); });
})();
