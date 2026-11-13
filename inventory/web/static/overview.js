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

  // Bars are thin with rounded ends and a surface-coloured gap between fills.
  function bars(id, name, labels, sets, opts) {
    opts = opts || {};
    var any = sets.some(function (s) {
      return s.data.some(function (v) { return v !== null && v > 0; });
    });
    if (!labels.length || !any) return none(id, name);
    new Chart(document.getElementById(id), {
      type: "bar",
      data: {
        labels: labels,
        datasets: sets.map(function (s, i) {
          return {
            label: s.label, data: s.data, backgroundColor: P.series[i],
            borderColor: P.surface, borderWidth: 2, borderRadius: 4, borderSkipped: "bottom",
            maxBarThickness: 28
          };
        })
      },
      options: {
        plugins: { title: title(name), legend: { display: sets.length > 1, labels: { color: P.text } } },
        scales: {
          x: { stacked: !!opts.stacked, grid: { display: false } },
          y: { stacked: !!opts.stacked, beginAtZero: true, grid: { color: P.grid },
               title: { display: !!opts.unit, text: opts.unit || "" } }
        }
      }
    });
  }

  function col(rows, key, div) {
    return rows.map(function (r) { return r[key] === null ? null : r[key] / div; });
  }

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

  var cap = data.capacity, names = cap.map(function (r) { return r.cluster; });
  bars("cpu-chart", "vCPU per cluster", names, [
    { label: "Allocatable", data: col(cap, "cpu_alloc_m", 1000) },
    { label: "Requested", data: col(cap, "cpu_req_m", 1000) },
    { label: "Used", data: col(cap, "cpu_used_m", 1000) }
  ], { unit: "vCPU" });
  bars("mem-chart", "Memory per cluster", names, [
    { label: "Allocatable", data: col(cap, "mem_alloc_b", GIB) },
    { label: "Requested", data: col(cap, "mem_req_b", GIB) },
    { label: "Used", data: col(cap, "mem_used_b", GIB) }
  ], { unit: "GiB" });

  var pv = data.pv_by_class;
  bars("pv-chart", "Persistent volumes by storage class",
       pv.map(function (c) { return c.storage_class; }),
       [{ label: "Volumes", data: pv.map(function (c) { return c.pv_count; }) }]);

  var fl = data.floating;
  bars("floating-chart", "Floating IP needs by source", fl.map(function (r) { return r.cluster; }), [
    { label: "Load balancer", data: col(fl, "lb", 1) },
    { label: "External IP", data: col(fl, "external_ip", 1) },
    { label: "Ingress LB", data: col(fl, "ingress_lb", 1) },
    { label: "Node external", data: col(fl, "node_external", 1) },
    { label: "Control plane", data: col(fl, "control_plane", 1) }
  ], { stacked: true });
})();
