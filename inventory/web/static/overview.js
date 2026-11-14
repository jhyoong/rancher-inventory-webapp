// Chart palette for all charts (dataviz reference palette, light mode, categorical slots in fixed order).
// Other pages can read window.VizPalette; drawing only happens when the overview data is on the page.
var VizPalette = {
  series: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
  text: "#52514e",
  grid: "#e4e3df",
  surface: "#fcfcfb"
};

(function () {
  var el = document.getElementById("overview-data");
  if (!el) return;
  var data = JSON.parse(el.textContent);
  var P = VizPalette;
  var GIB = 1073741824;

  Chart.defaults.color = P.text;
  Chart.defaults.animation = false;

  function none(id, title) {
    var canvas = document.getElementById(id);
    var p = document.createElement("p");
    p.className = "muted";
    p.textContent = title + ": not available";
    canvas.parentNode.replaceChild(p, canvas);
  }

  function title(text) {
    return { display: true, text: text, color: P.text };
  }

  // Horizontal bars, thin with rounded ends and a surface-coloured gap between fills.
  // opts.hrefs (one per label, may be null) makes a bar a link.
  function hbars(id, name, labels, sets, opts) {
    opts = opts || {};
    var any = sets.some(function (s) {
      return s.data.some(function (v) { return v !== null && v > 0; });
    });
    if (!labels.length || !any) return none(id, name);
    var hrefs = opts.hrefs || [];
    var canvas = document.getElementById(id);
    var chart = new Chart(canvas, {
      type: "bar",
      data: {
        labels: labels,
        datasets: sets.map(function (s, i) {
          return {
            label: s.label, data: s.data, backgroundColor: s.color || P.series[i],
            borderColor: P.surface, borderWidth: 2, borderRadius: 4, borderSkipped: "start",
            maxBarThickness: 24
          };
        })
      },
      options: {
        indexAxis: "y",
        aspectRatio: 520 / (70 + 30 * labels.length),
        onClick: function (evt, els) {
          if (els.length && hrefs[els[0].index]) window.location = hrefs[els[0].index];
        },
        onHover: function (evt, els) {
          canvas.style.cursor = els.length && hrefs[els[0].index] ? "pointer" : "default";
        },
        plugins: { title: title(name), legend: { display: !!opts.legend, labels: { color: P.text } } },
        scales: {
          x: { stacked: !!opts.stacked, beginAtZero: true, grid: { color: P.grid },
               title: { display: !!opts.unit, text: opts.unit || "" } },
          y: { stacked: !!opts.stacked, grid: { display: false } }
        }
      }
    });
    return chart;
  }

  function num(v, div) { return v === null ? null : v / div; }

  // OS donut: slots beyond the eighth fold into "Other"; never a generated colour.
  var os = data.os_images.slice(0, 7);
  var rest = data.os_images.slice(7).reduce(function (n, o) { return n + o.nodes; }, 0);
  if (rest) os.push({ os: "Other", nodes: rest });
  if (!os.length) {
    none("os-chart", "Nodes by OS image");
  } else {
    new Chart(document.getElementById("os-chart"), {
      type: "doughnut",
      data: {
        labels: os.map(function (o) { return o.os; }),
        datasets: [{
          data: os.map(function (o) { return o.nodes; }),
          backgroundColor: P.series, borderColor: P.surface, borderWidth: 2
        }]
      },
      options: { plugins: { title: title("Nodes by OS image"), legend: { position: "bottom", labels: { color: P.text } } } }
    });
  }

  // Capacity: sums over clusters that report each figure; the label says how many did.
  var cap = data.capacity;
  function capacity(id, name, block, div, unit) {
    var rows = [["Used", "used"], ["Requested", "req"], ["Allocatable", "alloc"]];
    var labels = rows.map(function (r) {
      return r[0] + " (" + block[r[1] + "_n"] + " of " + cap.clusters + " clusters)";
    });
    hbars(id, name, labels,
          [{ label: unit, data: rows.map(function (r) { return num(block[r[1]], div); }), color: P.series[0] }],
          { unit: unit });
  }
  // The memory canvas shares the card of the vCPU canvas.
  capacity("capacity-chart", "vCPU: used, requested, allocatable", cap.cpu, 1000, "vCPU");
  capacity("capacity-mem-chart", "Memory: used, requested, allocatable", cap.mem, GIB, "GiB");

  function counts(id, name, items) {
    hbars(id, name, items.map(function (i) { return i.label; }),
          [{ label: "Clusters", data: items.map(function (i) { return i.count; }), color: P.series[0] }],
          { hrefs: items.map(function (i) { return i.href; }), unit: "clusters" });
  }
  counts("class-chart", "Clusters by class", data.by_class);
  counts("flag-chart", "Clusters by flag", data.by_flag);

  var topSets = {
    cpu: { items: data.top_cpu, div: 1000, unit: "vCPU", share: data.top_cpu_share, what: "vCPU" },
    mem: { items: data.top_mem, div: GIB, unit: "GiB", share: data.top_mem_share, what: "memory" }
  };
  var topChart = null;
  function drawTop(key) {
    var t = topSets[key], canvas = document.getElementById("top-chart");
    if (!canvas) return;  // replaced by "not available"
    if (topChart) topChart.destroy();
    topChart = hbars("top-chart", "Top 10 clusters by requested " + t.what,
                     t.items.map(function (i) { return i.label; }),
                     [{ label: "Requested " + t.unit, data: t.items.map(function (i) { return num(i.value, t.div); }), color: P.series[0] }],
                     { hrefs: t.items.map(function (i) { return i.href; }), unit: t.unit });
    document.getElementById("top-share").textContent = t.share === null
      ? "Share of all requested " + t.what + ": not available"
      : "Top 10 = " + t.share + "% of all requested " + t.what;
  }
  document.getElementById("top-cpu-btn").addEventListener("click", function () { drawTop("cpu"); });
  document.getElementById("top-mem-btn").addEventListener("click", function () { drawTop("mem"); });
  drawTop("cpu");

  // Stacked bars: one bar per site, one series per class in a fixed order.
  var classes = data.by_class.map(function (c) { return c.label; });
  hbars("site-class-chart", "Clusters by site and class", data.site_class.map(function (s) { return s.site; }),
        classes.map(function (c, i) {
          return { label: c, color: P.series[i], data: data.site_class.map(function (s) { return s.counts[c] || 0; }) };
        }), { stacked: true, legend: true });

  var fl = data.floating;
  var sources = [["Load balancer", "lb"], ["External IP", "external_ip"], ["Ingress LB", "ingress_lb"],
                 ["Node external", "node_external"], ["Control plane", "control_plane"]];
  hbars("floating-chart", "Floating IP needs by source", ["All clusters"],
        sources.map(function (s, i) { return { label: s[0], color: P.series[i], data: [fl[s[1]]] }; }),
        { stacked: true, legend: true });

  var pv = data.pv_by_class;
  hbars("pv-chart", "Persistent volumes by storage class",
        pv.map(function (c) { return c.storage_class; }),
        [{ label: "Volumes", data: pv.map(function (c) { return c.pv_count; }), color: P.series[0] }]);
})();
